"""The memory service: the only writer of a chat directory. It never starts a model and never reads model credentials."""
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
            self.replica.attach(self.board)
        except BaseException:
            self.store.close()
            raise
        self.interval = self.config.get("interval", 15)
        self.sync_task = None
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
                "replication": self.replica.status()}

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
            return {"id": self.replica.positions.get(gid), "gid": gid, "status": "saved", "ordered": gid in self.replica.sealed,
                    "compaction_needed": self.board.compaction_needed()}
        if method == "view":
            return self.view(args.get("snapshot"), args.get("offset", 0))
        if method == "search":
            return {"text": self.memory.search(args["query"], args.get("limit", 20))}
        if method == "status_line":
            waiting = len(self.board.backlog())
            return {"text": f"OptChat: {waiting} to summarize" if waiting >= STATUS_AT else ""}
        if method == "zoom":
            return {"text": self.memory.zoom(args["id"], args["n"])}
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
        try:
            await self.stopping.wait()
        finally:
            if self.sync_task:
                self.sync_task.cancel()
                await asyncio.gather(self.sync_task, return_exceptions=True)
            self.server.close()
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)
            self.close()

    async def replicate(self):
        """Exchange data with the shared folder every `interval` seconds. Network I/O runs on a worker thread."""
        loop = asyncio.get_running_loop()
        while not self.stopping.is_set():
            try:
                result = await loop.run_in_executor(None, self.replica.exchange_round, self.replica.outgoing())
                self.replica.apply(result)
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
