"""Ownerless replication of one shared memory across machines.

Machines exchange data through a synced folder (for example an encrypted
rclone remote). Each machine writes only under machines/<its id>/, so the
sync can never overwrite another machine's data:

  machines/<id>/messages/<first>-<last>.jsonl.gz   own messages, by own sequence
  machines/<id>/summaries/<first>-<last>.jsonl.gz  summaries its workers wrote
  machines/<id>/heartbeat.json                     "everything up to T is uploaded"

Every machine orders messages by (date, id) once the heartbeats of all active
machines have passed them, so machines that are online together compute the
same order without an owner. A machine whose heartbeat is older than
`offline_after` is skipped; what it wrote while away is appended when it
arrives. Summaries are shared by the exact message sequence they cover (a
content key), so any machine's compaction is reused by the others wherever
their orders agree. The service never starts a model.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path

from .memory import Part
from .storage import KINDS, normalize_origin
from .util import atomic_json, json_text, valid_unicode

BATCH_RECORDS = 500
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
MACHINE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}-[0-9a-f]{6}$")
RANGE_FILE = re.compile(r"^(\d{12})-(\d{12})\.jsonl\.gz$")


def stamp(date: str) -> float:
    return datetime.fromisoformat(date).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def machine_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:31] or "machine"
    if not SAFE_NAME.match(name):
        raise ValueError(f"Unusable machine name {raw!r}")
    return name


def encode_batch(records) -> bytes:
    lines = [json_text(r) for r in records] + [json_text({"end": True, "count": len(records)})]
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), mtime=0)


def decode_batch(data: bytes) -> list:
    lines = gzip.decompress(data).decode("utf-8").splitlines()
    if not lines or json.loads(lines[-1]) != {"end": True, "count": len(lines) - 1}:
        raise ValueError("Incomplete batch file")
    return [json.loads(line) for line in lines[:-1]]


def range_files(names):
    """(first, last, name) for well-formed range files, sorted by first."""
    found = []
    for name in names:
        m = RANGE_FILE.match(name)
        if m and int(m.group(1)) <= int(m.group(2)):
            found.append((int(m.group(1)), int(m.group(2)), name))
    return sorted(found)


class DirExchange:
    """A plain directory, e.g. a local mount or a test folder."""

    def __init__(self, root):
        self.root = Path(root).expanduser()

    def describe(self):
        return f"dir:{self.root}"

    def files(self, rel):
        path = self.root / rel
        return sorted(p.name for p in path.iterdir() if p.is_file()) if path.is_dir() else []

    def dirs(self, rel):
        path = self.root / rel
        return sorted(p.name for p in path.iterdir() if p.is_dir()) if path.is_dir() else []

    def read(self, rel):
        try:
            return (self.root / rel).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, rel, data: bytes):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


class RcloneExchange:
    """An rclone remote, read directly so mount caches cannot delay the sync."""

    def __init__(self, remote, *, rclone="rclone", config=None, timeout=120):
        self.remote = remote.rstrip("/")
        self.base = [rclone] + (["--config", str(Path(config).expanduser())] if config else [])
        self.timeout = timeout

    def describe(self):
        return f"rclone:{self.remote}"

    def _run(self, *args, data=None, missing_ok=False):
        proc = subprocess.run(self.base + list(args), input=data, capture_output=True, timeout=self.timeout)
        if proc.returncode:
            err = proc.stderr.decode("utf-8", "replace").strip()
            if missing_ok and ("not found" in err.lower() or proc.returncode == 3):
                return None
            raise OSError(f"rclone {args[0]} failed ({proc.returncode}): {err[-300:]}")
        return proc.stdout

    def _path(self, rel):
        return f"{self.remote}/{rel}" if rel else self.remote

    def files(self, rel):
        out = self._run("lsf", "--files-only", self._path(rel), missing_ok=True)
        return sorted(out.decode().split()) if out else []

    def dirs(self, rel):
        out = self._run("lsf", "--dirs-only", self._path(rel), missing_ok=True)
        return sorted(name.rstrip("/") for name in out.decode().split()) if out else []

    def read(self, rel):
        return self._run("cat", self._path(rel), missing_ok=True)

    def write(self, rel, data: bytes):
        tmp = f"{rel}.{secrets.token_hex(4)}.tmp"
        self._run("rcat", self._path(tmp), data=data)
        # --ignore-times: without modtimes or common hashes rclone compares by size
        # only, and would silently keep an old same-size heartbeat.
        self._run("moveto", "--ignore-times", self._path(tmp), self._path(rel))


def make_exchange(config):
    spec = (config or {}).get("exchange")
    if not spec:
        return None
    if spec.get("type") == "dir":
        return DirExchange(spec["path"])
    if spec.get("type") == "rclone":
        return RcloneExchange(spec["remote"], rclone=spec.get("rclone", "rclone"), config=spec.get("config"))
    raise ValueError("exchange.type must be 'dir' or 'rclone'")


class Replica:
    def __init__(self, store, *, name="local", exchange=None, offline_after=600, join_grace=60, clock=time.time, report=print):
        self.store, self.exchange, self.offline_after, self.join_grace = store, exchange, offline_after, join_grace
        self.clock, self.report = clock, report
        ident_path = store.path / "identity.json"
        if ident_path.exists():
            ident = json.loads(ident_path.read_text())
        else:
            # A fresh incarnation per local store: a rebuilt machine never
            # reuses the sequence numbers its earlier incarnation published.
            ident = {"name": machine_name(name), "incarnation": secrets.token_hex(3)}
            atomic_json(ident_path, ident)
        self.name = ident["name"]
        self.machine = f"{self.name}-{ident['incarnation']}"
        for stream in ("own", "remote", "pool", "outsum"):
            (store.path / stream).mkdir(exist_ok=True, mode=0o700)
        self.own = [r for r in store._records("own")]
        if [r["seq"] for r in self.own] != list(range(len(self.own))):
            raise RuntimeError("Own message journal is not contiguous; restore it from backup")
        self.own_by_gid = {r["gid"]: r for r in self.own}
        self.remote = {}
        for r in store._records("remote"):
            self.remote.setdefault(r["gid"], r)
        self.pool = {}
        for r in store._records("pool"):
            self.pool.setdefault(r["key"], r["text"])
        self.outsum = [r for r in store._records("outsum")]
        for r in self.outsum:
            self.pool.setdefault(r["key"], r["text"])
        self.state_path = store.path / "sync.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.state.setdefault("uploaded_seq", 0)
        self.state.setdefault("uploaded_sum", 0)
        self.state.setdefault("machines", {})
        self.state.setdefault("odates", {})  # own seq -> published order date, never changed once set
        self.positions = {m.gid: m.i for m in store.root if m.gid}  # gid -> local position
        self.sealed = set(self.positions)
        self.started = clock()
        self.synced = False  # no exchange round has completed since start
        # (sort time, gid) of the latest placed message; anything sorting before it is late.
        self.last_key = max(((self.order_stamp(self.record_for(m)), m.gid) for m in store.root), default=(float("-inf"), ""))
        self.keys = {}
        self.board = None
        self.last_sync = None
        self.last_error = None

    # Recording -----------------------------------------------------------

    def next_gid(self):
        return f"{self.machine}/{len(self.own)}"

    def record(self, kind, text, date, origin, gid=None):
        gid = gid or self.next_gid()
        if gid != self.next_gid():
            raise RuntimeError(f"Own message {gid} is out of sequence")
        rec = {"gid": gid, "seq": len(self.own), "kind": kind, "text": text, "date": date, "origin": origin}
        self.store._append("own", rec)
        self.own.append(rec)
        self.own_by_gid[gid] = rec
        return gid

    # Ordering ------------------------------------------------------------

    def attach(self, board):
        self.board = board
        self.seal()

    def watermarks(self, now):
        """Watermark per active remote machine; None blocks (data not downloaded yet)."""
        marks = {}
        for mid, info in self.state["machines"].items():
            hb = info.get("heartbeat")
            if not hb:
                continue
            try:
                fresh = now - stamp(hb["updated"]) <= self.offline_after
            except (KeyError, TypeError, ValueError):
                continue
            if not fresh:
                continue
            complete = info.get("have_seq", 0) >= hb.get("seq", 0)
            stuck = info.get("blocked_since") and now - info["blocked_since"] > self.offline_after
            if complete:
                marks[mid] = stamp(hb["watermark"])
            elif not stuck:
                marks[mid] = None
        return marks

    def record_for(self, message):
        return self.own_by_gid.get(message.gid) or self.remote.get(message.gid) or {"date": message.date}

    def order_stamp(self, r):
        """Sort time: the message date, except for messages a machine recorded
        while the others could not see it (first start, or back from offline).
        Those sort when the others are sure to have noticed it again, so they
        never jump ahead of what the others already placed in the meantime."""
        if "odate" in r:
            return stamp(r["odate"])
        if r.get("gid") in self.own_by_gid:
            fixed = self.state["odates"].get(str(r["seq"]))
            if fixed:
                return stamp(fixed)
            visible = self.state.get("visible_from")
            if r["seq"] >= self.state["uploaded_seq"] and visible:
                return max(stamp(r["date"]), stamp(visible))
        return stamp(r["date"])

    def unordered(self):
        return [r for r in chain(self.own, self.remote.values()) if r["gid"] not in self.sealed]

    def seal(self):
        """Append every message whose position is now agreed (or late) to the local order."""
        if self.board is None:
            return 0
        pending = sorted(self.unordered(), key=lambda r: (self.order_stamp(r), r["gid"]))
        if not pending:
            return 0
        now = self.clock()
        if self.exchange and not self.synced and now - self.started < self.offline_after:
            return 0  # Learn which machines exist before placing anything.
        marks = self.watermarks(now) if self.exchange else {}
        sealed = 0
        for r in pending:
            t = self.order_stamp(r)
            late = (t, r["gid"]) < self.last_key
            if not late and any(w is None or w < t for w in marks.values()):
                break
            message = self.board.append(r["kind"], r["text"], r["date"], r["origin"], gid=r["gid"])
            self.positions[r["gid"]] = message.i
            self.sealed.add(r["gid"])
            self.last_key = max(self.last_key, (t, r["gid"]))
            sealed += 1
        return sealed

    # Shared summaries ----------------------------------------------------

    def key(self, p):
        found = self.keys.get(p.key)
        if found is None:
            if p.l == 0:
                found = self.store.root[p.i].gid or f"legacy/{p.i}"
            else:
                a, b = (self.key(Part(p.l - 1, 2 * p.i + j)) for j in (0, 1))
                found = hashlib.sha256(f"{a}|{b}".encode()).hexdigest()[:32]
            self.keys[p.key] = found
        return found

    def lookup(self, p):
        return self.pool.get(self.key(p)) if self.pool else None

    def on_summary(self, p, text):
        key = self.key(p)
        rec = {"n": len(self.outsum), "key": key, "text": text}
        self.store._append("outsum", rec)
        self.outsum.append(rec)
        self.pool.setdefault(key, text)

    # Exchange ------------------------------------------------------------

    def outgoing(self):
        """Snapshot for one exchange round (taken on the service thread)."""
        now = self.clock()
        last = self.state.get("last_heartbeat")
        visible = self.state.get("visible_from")
        if (last is None or now - stamp(last) > self.offline_after) and not (visible and stamp(visible) > now):
            # The others treat this machine as new or offline until they see a
            # fresh heartbeat; give them join_grace seconds to notice it.
            self.state["visible_from"] = visible = iso(now + self.join_grace)
        odates = self.state["odates"]
        for r in self.own[self.state["uploaded_seq"]:]:
            if str(r["seq"]) not in odates and visible and stamp(r["date"]) < stamp(visible):
                odates[str(r["seq"])] = visible  # Fixed before upload: every copy agrees.
        atomic_json(self.state_path, self.state)
        own = [dict(r, odate=odates[str(r["seq"])]) if str(r["seq"]) in odates else r for r in self.own[self.state["uploaded_seq"]:]]
        sums = self.outsum[self.state["uploaded_sum"]:]
        have = {mid: (info.get("have_seq", 0), info.get("have_sum", 0)) for mid, info in self.state["machines"].items()}
        return {"own": own, "sums": sums, "have": have, "uploaded_seq": self.state["uploaded_seq"], "uploaded_sum": self.state["uploaded_sum"]}

    def exchange_round(self, out):
        """Network I/O only; safe to run on a worker thread. Returns what to apply."""
        ex, me = self.exchange, f"machines/{self.machine}"
        result = {"uploaded_seq": out["uploaded_seq"], "uploaded_sum": out["uploaded_sum"], "machines": {}, "errors": [], "heartbeat": None}
        for kind, records, start in (("messages", out["own"], out["uploaded_seq"]), ("summaries", out["sums"], out["uploaded_sum"])):
            for at in range(0, len(records), BATCH_RECORDS):
                chunk = records[at:at + BATCH_RECORDS]
                first, last = start + at, start + at + len(chunk) - 1
                body = [{k: v for k, v in r.items()} for r in chunk]
                ex.write(f"{me}/{kind}/{first:012d}-{last:012d}.jsonl.gz", encode_batch(body))
                result["uploaded_seq" if kind == "messages" else "uploaded_sum"] = last + 1
        now = self.clock()
        pending_own = out["own"][result["uploaded_seq"] - out["uploaded_seq"]:]
        watermark = stamp(pending_own[0].get("odate", pending_own[0]["date"])) - 1e-6 if pending_own else now
        heartbeat = {"machine": self.machine, "name": self.name, "updated": iso(now), "watermark": iso(watermark),
                     "seq": result["uploaded_seq"], "sum": result["uploaded_sum"]}
        ex.write(f"{me}/heartbeat.json", json_text(heartbeat).encode())
        result["heartbeat"] = heartbeat["updated"]
        for mid in ex.dirs("machines"):
            if mid == self.machine or not MACHINE_ID.match(mid):
                continue
            have_seq, have_sum = out["have"].get(mid, (0, 0))
            got = {"messages": [], "summaries": [], "have_seq": have_seq, "have_sum": have_sum}
            try:
                raw = ex.read(f"machines/{mid}/heartbeat.json")
                got["heartbeat"] = json.loads(raw) if raw else None
                for kind, field in (("messages", "have_seq"), ("summaries", "have_sum")):
                    for first, last, name in range_files(ex.files(f"machines/{mid}/{kind}")):
                        if last < got[field]:
                            continue
                        if first > got[field]:
                            break  # A gap: the missing file is not visible yet.
                        data = ex.read(f"machines/{mid}/{kind}/{name}")
                        if data is None:
                            break
                        records = decode_batch(data)
                        if len(records) != last - first + 1:
                            raise ValueError(f"{mid}/{kind}/{name} has the wrong record count")
                        check = self._valid_remote if kind == "messages" else self._valid_summary
                        got[kind].extend(check(mid, r) for r in records[got[field] - first:])
                        got[field] = last + 1
            except (OSError, ValueError, EOFError, gzip.BadGzipFile) as exc:
                result["errors"].append(f"{mid}: {exc}")
                got["error"] = str(exc)
            result["machines"][mid] = got
        return result

    def apply(self, result):
        """Persist an exchange round's results and extend the local order."""
        self.state["uploaded_seq"] = max(self.state["uploaded_seq"], result["uploaded_seq"])
        self.state["uploaded_sum"] = max(self.state["uploaded_sum"], result["uploaded_sum"])
        if result.get("heartbeat"):
            self.state["last_heartbeat"] = result["heartbeat"]
        new_pool = False
        now = self.clock()
        for mid, got in result["machines"].items():
            info = self.state["machines"].setdefault(mid, {"have_seq": 0, "have_sum": 0})
            if got.get("heartbeat"):
                info["heartbeat"] = got["heartbeat"]
            fresh = [r for r in got["messages"] if r["gid"] not in self.remote and r["gid"] not in self.sealed]
            self.store._append_many("remote", fresh)
            for rec in fresh:
                self.remote[rec["gid"]] = rec
            summaries = {}
            for rec in got["summaries"]:
                if rec["key"] not in self.pool and rec["key"] not in summaries:
                    summaries[rec["key"]] = rec
            self.store._append_many("pool", list(summaries.values()))
            for key, rec in summaries.items():
                self.pool[key] = rec["text"]
            new_pool = new_pool or bool(summaries)
            info["have_seq"] = max(info.get("have_seq", 0), got["have_seq"])
            info["have_sum"] = max(info.get("have_sum", 0), got["have_sum"])
            if got.get("error"):
                info["blocked_since"] = info.get("blocked_since") or now
                info["error"] = got["error"]
            else:
                info.pop("blocked_since", None)
                info.pop("error", None)
        atomic_json(self.state_path, self.state)
        self.last_sync = now
        self.synced = True
        self.last_error = "; ".join(result["errors"]) or None
        sealed = self.seal()
        if self.board is not None:
            if new_pool:
                self.board.pool_arrived()
            self.board.advance()
        return sealed

    def _valid_remote(self, mid, r):
        gid, seq = r.get("gid"), r.get("seq")
        if not isinstance(gid, str) or gid != f"{mid}/{seq}":
            raise ValueError(f"Invalid message id from {mid}")
        if r.get("kind") not in KINDS or not isinstance(r.get("text"), str):
            raise ValueError(f"Invalid message {gid}")
        if datetime.fromisoformat(r["date"]).tzinfo is None:
            raise ValueError(f"Message {gid} has no timezone")
        rec = {"gid": gid, "seq": seq, "kind": r["kind"], "text": valid_unicode(r["text"]), "date": r["date"], "origin": normalize_origin(r.get("origin"))}
        if "odate" in r:
            if datetime.fromisoformat(r["odate"]).tzinfo is None:
                raise ValueError(f"Message {gid} has an invalid order date")
            rec["odate"] = r["odate"]
        return rec

    def _valid_summary(self, mid, r):
        if not isinstance(r.get("key"), str) or not isinstance(r.get("text"), str) or not r["text"].strip():
            raise ValueError(f"Invalid summary from {mid}")
        return {"key": r["key"], "text": valid_unicode(r["text"]), "from": mid}

    def sync_once(self):
        """One blocking exchange round; the daemon runs exchange_round on a worker thread instead."""
        return self.apply(self.exchange_round(self.outgoing()))

    def status(self):
        now = self.clock()
        marks = self.watermarks(now) if self.exchange else {}
        machines = {}
        for mid, info in self.state["machines"].items():
            hb = info.get("heartbeat") or {}
            machines[mid] = {"active": mid in marks, "last_heartbeat": hb.get("updated"), "messages_received": info.get("have_seq", 0),
                             "summaries_received": info.get("have_sum", 0), **({"error": info["error"]} if info.get("error") else {})}
        return {"machine": self.machine, "exchange": self.exchange.describe() if self.exchange else None,
                "own_messages": len(self.own), "uploaded": self.state["uploaded_seq"], "unordered": len(self.unordered()),
                "shared_summaries": len(self.pool), "last_sync": iso(self.last_sync) if self.last_sync else None,
                "last_error": self.last_error, "machines": machines}
