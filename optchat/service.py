"""The memory service: the only writer of a chat directory. It runs no model, unless the user stored an API key (see api.py)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import signal
import sqlite3
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

from . import api
from .export import backup, export_html
from .jobs import BATCH_TASKS, STATUS_AT, JobBoard
from .ledger import Ledger
from .memory import Memory, Part
from .replica import Replica, make_exchange
from .storage import Store, StorageError
from .util import CAP, VIEW, atomic_json, cap, json_text

DEFAULT_CHAT = Path.home() / ".local/share/optchat/chat"


def endpoint(chat):
    canonical = str(Path(chat).expanduser().resolve())
    # GUI apps and shells often have different TMPDIR values on macOS.
    base = Path('/tmp') / f"optchat-{os.getuid()}"
    base.mkdir(mode=0o700, exist_ok=True)
    if base.stat().st_uid != os.getuid() or base.is_symlink():
        raise RuntimeError("Unsafe local socket directory")
    os.chmod(base, 0o700)
    return base / (hashlib.sha256(canonical.encode()).hexdigest()[:24] + ".sock")


class Service:
    def __init__(self, chat, *, budget=VIEW, config=None, clock=time.time, wait=False):
        # config=None means a memory for this machine only. The CLI passes the machine
        # config only for the configured chat directory (see config.py).
        self.config = config or {}
        report = lambda s: print(s, file=sys.stderr, flush=True)
        # wait=True: a managed service (launchd, systemd) waits for the lock and
        # takes over when another copy stops, so it does not exit and restart in a loop.
        self.store = Store(Path(chat), report, wait=wait)
        try:
            self.replica = Replica(self.store, name=self.config.get("machine") or socket.gethostname().split(".")[0],
                                   exchange=make_exchange(self.config), offline_after=self.config.get("offline_after", 600),
                                   join_grace=self.config.get("join_grace", 4 * self.config.get("interval", 15)), clock=clock, report=report)
            self.ledger = Ledger(self.store, self.replica)
            self.memory = Memory(self.store, budget)
            self.board = JobBoard(self.memory, pool=self.replica, batch_tasks=BATCH_TASKS)
            self.api = api.ApiCompactor(self.store.path, clock=clock)
            self.replica.attach(self.board)
        except BaseException:
            self.store.close()
            raise
        self.interval = self.config.get("interval", 15)
        self.sync_task = None
        self.api_task = None
        # Only the configured memory of a machine uses the API, so tests and scratch memories never spend credit.
        self.api_enabled = bool(self.config)
        self.path = endpoint(chat)
        self.token = secrets.token_urlsafe(32)
        atomic_json(self.store.path / "connection.json", {"token": self.token})
        self.snapshots = {}
        self.snapshot_versions = {}
        self.draining = False
        self.server = None
        self.stopping = asyncio.Event()

    def status(self):
        b, m = self.board, self.memory
        b.advance()
        return {"messages": m.total, "nodes": len(self.store.tree), "view_bytes": m.bytes, "view_budget": m.budget,
                "view_ready": m.first() == m.total and m.bytes <= m.budget, "first_unsummarized": m.first(), "pending_nodes": len(b.offered), "active_jobs": len(b.leases),
                "failures": {f"{i*2**l}+{2**l}": {"reason": reason, "blocked": b.blocked(Part(l, i))} for (l, i), reason in b.failures.items()},
                "queued_hook_events": len(list((self.store.path / 'spool').glob('*.json'))),
                "rejected_hook_events": len(list((self.store.path / 'spool' / 'failed').glob('*.json'))), "model_processes": 0,
                "replication": self.replica.status(), "api": {**self.api.state(self.api.load_key()), "runs_here": self.api_turn()} if self.api_enabled else {"status": "off"}}

    def call(self, method, args, client="local"):
        if not self.draining and method not in ("hook", "flush_hooks", "shutdown"):
            self.flush_hooks()
        if method == "flush_hooks":
            return self.flush_hooks(args.get("current"))
        if method == "status":
            return self.status()
        if method == "append":
            text = args["text"]
            if args["kind"] == "echo":
                text = cap(text, CAP)
            origin = dict(args.get("origin") or {})
            if self.replica.exchange:
                origin.setdefault("machine", self.replica.name)
            gid = self.ledger.append(args["event_id"], args["kind"], text, args.get("date"), origin)
            self.replica.seal()
            # id: local position (usable with zoom) once ordered; gid: same on every machine.
            return {"id": self.replica.positions.get(gid), "gid": gid, "status": "saved", "ordered": gid in self.replica.sealed}
        if method == "view":
            return self.view(args.get("snapshot"), args.get("offset", 0))
        if method == "search":
            return {"text": self.memory.search(args["query"], args.get("limit", 10))}
        if method == "status_line":
            waiting = len(self.board.backlog())
            if waiting < STATUS_AT:
                return {"text": ""}
            note = self.api.note(self.api.load_key()) if self.api_enabled else ""
            return {"text": f"OptChat: {waiting} to summarize" + (f", {note}" if note else "")}
        if method == "api_key_changed":
            self.api.key_changed()
            return {"shared": bool(self.api_enabled and self.replica.exchange)}
        if method == "api_status":
            return self.api.state(self.api.load_key())
        if method == "api_compact":
            if not self.api_enabled:
                return {"status": "off", "reason": "this memory has no machine config, so it does not use the API"}
            key = self.api.load_key()
            if key is None:
                return self.api.state(None)
            if not self.api.running:
                self.api.forced = True
                self.api.wake.set()
            return {**self.api.state(key), "target": self.api.run if self.api.running else self.api.run + 1}
        if method == "zoom":
            return {"text": self.memory.zoom(args["id"], args["n"])}
        if method == "thread":
            return {"text": self.memory.thread(args["id"], args.get("n", 5))}
        if method == "date":
            return {"text": self.memory.date(args["id"])}
        if method == "read_message":
            return {"text": self.memory.read_message(args["id"], args["offset"])}
        if method == "compact_next":
            worker = args.get("worker")
            if worker is not None and (not isinstance(worker, str) or worker not in self.board.workers):
                raise ValueError("Unknown worker token. Start a new invocation with compact_next() without a token.")
            return self.board.next(worker)
        if method == "compact_read":
            return self.board.read(args["job"], args["offset"])
        if method == "compact_submit":
            # Sessions started before batching still send a single "line".
            lines = args["lines"] if "lines" in args else args["line"]
            return self.board.submit(args["job"], lines, chain=args.get("chain", True))
        if method == "compact_release":
            return self.board.release(args["job"], args["reason"], args.get("task"))
        if method == "compact_abandon":
            return self.board.abandon(args["job"])
        if method == "compact_resume":
            return self.board.resume(args["id"], args["n"])
        if method == "export":
            path = Path(args["path"]).expanduser().resolve()
            export_html(self.memory, path)
            return {"path": str(path)}
        if method == "backup":
            path = Path(args["path"]).expanduser().resolve()
            self.ledger.db.execute("PRAGMA wal_checkpoint(FULL)")
            backup(self.store, path)
            return {"path": str(path)}
        if method == "hook":
            from .hooks import ingest
            return ingest(self, args)
        if method == "shutdown":
            self.stopping.set()
            return {"status": "stopping"}
        raise ValueError(f"Unknown method: {method}")

    def view(self, snapshot=None, offset=0):
        self.board.advance()
        if snapshot is None:
            if offset != 0:
                raise ValueError("Begin at offset 0 without a snapshot")
            version = (self.memory.total, len(self.store.tree), self.board.frontier, repr(self.board.failures), len(self.replica.unordered()))
            snapshot = self.snapshot_versions.get(version)
            if snapshot not in self.snapshots:
                now = time.monotonic()
                for token, saved in list(self.snapshots.items()):
                    if now - saved['accessed'] > 300:
                        del self.snapshots[token]
                        self.snapshot_versions.pop(saved['version'], None)
                if len(self.snapshots) >= 128:
                    raise ValueError("128 view snapshots are in use. Try again later, and keep the snapshot that you are reading.")
                snapshot = secrets.token_urlsafe(16)
                text, covered = self.memory.bounded_view(self.board.frontier)
                pending = {"start": self.board.frontier, "count": self.memory.total - self.board.frontier}
                omitted = {"start": covered, "count": self.board.frontier - covered, "reason": "view_over_budget", "instruction": "These messages have summaries. The view has no room for them until larger merges exist. Read them with zoom or read_message."}
                self.snapshots[snapshot] = {"text": text, "accessed": now, "version": version,
                    "status": "ready" if covered == self.memory.total else "partial", "pending": pending, "omitted": omitted,
                    # Messages that this machine recorded or received and that have no place in
                    # the shared order yet. They wait for the check-ins of the other machines.
                    "unordered": len(self.replica.unordered()),
                    "blocked": self.status()["failures"]}
                self.snapshot_versions[version] = snapshot
        if snapshot not in self.snapshots:
            raise ValueError("The view snapshot has expired. Start again at offset 0.")
        saved = self.snapshots[snapshot]
        saved['accessed'] = time.monotonic()
        text = saved["text"]
        if type(offset) is not int or not 0 <= offset <= len(text):
            raise ValueError("Invalid view offset")
        end = min(len(text), offset + 24_000)
        return {"status": saved["status"], "pending": saved["pending"], "omitted": saved["omitted"], "unordered": saved["unordered"], "failures": saved["blocked"], "snapshot": snapshot, "offset": offset, "next_offset": end if end < len(text) else None, "total": len(text), "text": text[offset:end],
                "instruction": "Read all pages. The view holds only finished summaries. A pending range is knowledge that is missing from the view, and its content is unknown. Compact it, or read the original messages with zoom(id, 1) or read_message before you rely on them. Never guess the content of a pending range. A blocked line needs compact_resume after its cause is fixed."}

    def flush_hooks(self, current=None):
        from .hooks import ingest
        from .util import sync_dir
        self.draining = True
        result = {}
        try:
            for path in sorted((self.store.path / "spool").glob("*.json")):
                try:
                    value = ingest(self, json.loads(path.read_text()))
                    if path.name == current:
                        result = value
                except (ValueError, TypeError, KeyError) as exc:
                    failed = path.parent / 'failed'
                    failed.mkdir(exist_ok=True, mode=0o700)
                    os.replace(path, failed / path.name)
                    atomic_json(failed / (path.stem + '.error'), {'error': str(exc)})
                    sync_dir(path.parent)
                    print(f"Rejected hook retained in {failed / path.name}: {exc}", file=sys.stderr, flush=True)
                    if path.name == current:
                        result = {'rejected': True, 'error': str(exc)}
                    continue
                except (RuntimeError, OSError, sqlite3.Error) as exc:
                    # Keep the durable event for recovery; don't silently lose it.
                    print(f"Queued hook {path.name}: {exc}", file=sys.stderr, flush=True)
                    if path.name == current:
                        result = {"queued": True, "error": str(exc)}
                    self.stopping.set()
                    break
                path.unlink()
                sync_dir(path.parent)
        finally:
            self.draining = False
        return result

    async def handle(self, reader, writer):
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                    if not secrets.compare_digest(str(request.get("token", "")), self.token):
                        raise ValueError("Unauthorized local memory client")
                    result = self.call(request["method"], request.get("args", {}), request.get("client", "local"))
                    response = {"result": result}
                except Exception as exc:
                    if isinstance(exc, (StorageError, sqlite3.Error)) or self.ledger.failed or (isinstance(exc, OSError) and request.get('method') not in ('export', 'backup')):
                        self.stopping.set()
                    response = {"error": str(exc)}
                writer.write((json_text(response) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def serve(self):
        # Only the flock owner removes a stale socket. No second service can take it over.
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.handle, str(self.path), limit=32 * 1024 * 1024)
        os.chmod(self.path, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        if self.replica.exchange:
            self.sync_task = asyncio.create_task(self.replicate())
        if self.api_enabled:
            self.api_task = asyncio.create_task(self.summarize())
        try:
            await self.stopping.wait()
        finally:
            tasks = [t for t in (self.sync_task, self.api_task) if t]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.server.close()
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)
            self.close()

    async def replicate(self):
        """Exchange data with the shared folder every `interval` seconds. Network I/O runs on a worker thread."""
        while not self.stopping.is_set():
            try:
                await self.exchange()
            except StorageError:
                self.stopping.set()
                raise
            except Exception as exc:
                # Offline or the folder is unreachable: keep recording locally and
                # let the offline timeout order messages without the other machines.
                self.replica.last_error = str(exc)
                print(f"Replication: {exc}", file=sys.stderr, flush=True)
                self.replica.seal()
                self.board.advance()
            try:
                await asyncio.wait_for(self.stopping.wait(), self.interval)
            except asyncio.TimeoutError:
                pass

    def model_work(self):
        """Summaries that need a model: long messages and merges that are too long to be their own line."""
        self.board.advance()
        merges = sum(1 for key in self.board.nonfree if key[0] > 0 and key not in self.store.tree and not self.board.blocked(Part(*key)))
        return len(self.board.backlog()) + merges

    async def summarize(self):
        """Send compaction jobs to the API when this machine has a key. Without a key, or while the API is paused, agents write the summaries."""
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.api.wake.wait(), api.CHECK)
            except asyncio.TimeoutError:
                pass
            forced, self.api.forced = self.api.forced, False
            self.api.wake.clear()
            key = self.api.load_key()
            if not self.api.ready(key, forced) or not (forced or (self.api_turn() and self.api.due(self.memory.total, self.model_work()))):
                continue
            try:
                await self.api.compact(self.call, key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A fault here must not stop the memory. The subagent can still write the summaries.
                self.api.pause(api.ApiError("error", f"API worker: {exc}"), key)
                print(f"API worker: {exc}", file=sys.stderr, flush=True)

    async def exchange(self):
        """One exchange round with the shared folder, and the API key that the machines share through it."""
        loop = asyncio.get_running_loop()
        out = self.replica.outgoing()
        out["api"] = self.api.heartbeat(self.api_enabled)
        self.replica.apply(await loop.run_in_executor(None, self.replica.exchange_round, out))
        if not self.api_enabled:
            return
        ex, me = self.replica.exchange, self.replica.machine
        info = self.api.key_info
        if info.get("pending"):
            await loop.run_in_executor(None, api.publish, ex, me, self.api.load_key(), info["date"])
            if self.api.key_info is info:  # The user did not change the key again meanwhile.
                info["pending"] = False
                self.api.save()
        found = self.api.newer({mid: (beat.get("heartbeat") or {}).get("api") for mid, beat in self.replica.state["machines"].items()})
        if found:
            data = await loop.run_in_executor(None, api.fetch, ex, found[0])
            # The heartbeat can arrive before the file. Then the next round tries again.
            if data and data["date"] == found[1] and self.api.newer({found[0]: {"key": data["date"]}}) and self.api.adopt(data, found[0]):
                await loop.run_in_executor(None, ex.delete, f"machines/{me}/{api.SHARED}")

    def api_turn(self):
        """True when this machine writes the summaries with the API: it is the first, by id, of the online machines whose API is ready."""
        if not self.replica.exchange:
            return True
        online = self.replica.watermarks(self.replica.clock())
        ready = [mid for mid in online if ((self.replica.state["machines"][mid].get("heartbeat") or {}).get("api") or {}).get("ready")]
        return all(self.replica.machine < mid for mid in ready)

    def close(self):
        self.ledger.close()
        self.store.close()


class Client:
    def __init__(self, chat=DEFAULT_CHAT, *, autostart=True):
        self.chat = Path(chat).expanduser().resolve()
        self.path = endpoint(self.chat)
        self.id = secrets.token_urlsafe(12)
        self.autostart = autostart

    def call(self, method, args=None):
        for attempt in range(2):
            try:
                token = json.loads((self.chat / "connection.json").read_text())["token"]
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(30)
                    sock.connect(str(self.path))
                    sock.sendall((json_text({"token": token, "client": self.id, "method": method, "args": args or {}}) + "\n").encode())
                    with sock.makefile("r", encoding="utf-8") as f:
                        line = f.readline()
                    response = json.loads(line)
                    if "error" in response:
                        raise ValueError(response["error"])
                    return response["result"]
            except (FileNotFoundError, ConnectionRefusedError):
                if not self.autostart or attempt:
                    raise RuntimeError("OptChat memory service is not running") from None
                self.start()

    def start(self):
        self.chat.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = os.open(self.chat / "service.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            proc = subprocess.Popen([sys.executable, "-m", "optchat", "--chat", str(self.chat), "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, close_fds=True, env=env)
            threading.Thread(target=proc.wait, daemon=True).start()
        finally:
            os.close(log)
        # Wait a bounded time for the service socket. This loop never waits on a model.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(str(self.path))
                return
            except (FileNotFoundError, ConnectionRefusedError):
                if proc.poll() is not None:
                    # Another simultaneous launcher may own the lock and still
                    # be loading. Keep waiting for its endpoint too.
                    import fcntl
                    lock = os.open(self.chat / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            break
                    finally:
                        os.close(lock)
                time.sleep(.05)
        raise RuntimeError(f"Memory service did not start; inspect {self.chat / 'service.log'}")
