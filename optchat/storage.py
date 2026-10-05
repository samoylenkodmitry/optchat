from __future__ import annotations

import fcntl
import json
import hashlib
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .util import json_text, now, size, sync_dir, valid_unicode

KINDS = {"user", "talk", "tool", "echo", "note"}


def normalize_origin(origin):
    if origin is None:
        return {}
    if not isinstance(origin, dict) or set(origin) - {"project", "session", "agent", "machine"}:
        raise ValueError("origin accepts project, session, agent and machine")
    if any(not isinstance(v, str) or not v or len(v) > 500 for v in origin.values()):
        raise ValueError("Origin values must contain 1–500 characters")
    return {k: valid_unicode(v) for k, v in sorted(origin.items())}


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Message:
    i: int
    kind: str
    text: str
    size: int
    date: str
    origin: dict = field(default_factory=dict)
    gid: str = ""  # Global id "<machine>/<seq>", identical on every machine.

    @property
    def source(self) -> str:
        provenance = " [origin=" + json_text(self.origin) + "]" if self.origin else ""
        return f"{self.kind}{provenance}: {self.text}"

    @property
    def compact_source(self) -> str:
        if not self.origin:
            return self.source
        project = self.origin.get("project")
        label = (Path(project).name[:40] or "root") + "~" + hashlib.sha256(project.encode()).hexdigest()[:8] if project else "shared"
        agent = self.origin.get("agent", "agent")[:24]
        machine = "@" + self.origin["machine"][:24] if self.origin.get("machine") else ""
        session = self.origin.get("session")
        suffix = "#" + hashlib.sha256(session.encode()).hexdigest()[:6] if session else ""
        return f"{self.kind}@{label}/{agent}{machine}{suffix}: {self.text}"


@dataclass(frozen=True)
class Node:
    l: int
    i: int
    text: str
    size: int


class Store:
    """Single-process owner. One write + fsync per immutable JSONL record.

    flock is atomic, survives no owner crash, and needs no stale-socket unlink
    race. Never unlink the lock file: all owners must lock the same inode.
    """

    def __init__(self, path: Path, report: Callable[[str], None] = print):
        self.path = path.expanduser().resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.report = report
        self._lock = os.open(self.path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock)
            self._lock = -1
            raise StorageError(f"Another OptChat process owns {self.path}") from None
        self.root: list[Message] = []
        self.tree: dict[tuple[int, int], Node] = {}
        self._poisoned = False
        try:
            for stream in ("main", "tree"):
                (self.path / stream).mkdir(exist_ok=True, mode=0o700)
            sync_dir(self.path)
            self._load()
        except BaseException:
            self.close()
            raise

    def _records(self, stream: str):
        for file in sorted((self.path / stream).glob("*.jsonl")):
            with file.open("rb") as f:
                ended = True
                for number, raw in enumerate(f, 1):
                    ended = raw.endswith(b"\n")
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        self.report(f"Skipped torn/invalid JSON: {file.name}:{number} ({stream})")
                        continue
                    yield value
            if not ended:
                fd = os.open(file, os.O_WRONLY | os.O_APPEND)
                try:
                    if os.write(fd, b"\n") != 1:
                        raise StorageError("Failed to terminate torn line")
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def _load(self):
        messages = {}
        for value in self._records("main"):
            try:
                m = Message(**value)
                assert type(m.i) is int and m.i >= 0 and m.kind in KINDS
                assert isinstance(m.text, str) and m.size == size(m.source)
                assert normalize_origin(m.origin) == m.origin and isinstance(m.gid, str)
                assert datetime.fromisoformat(m.date).tzinfo is not None
            except (TypeError, ValueError, AssertionError):
                raise StorageError("Invalid message record; restore the damaged file from backup") from None
            if m.i in messages:
                raise StorageError(f"Duplicate permanent message id {m.i}")
            messages[m.i] = m
        for i in sorted(messages):
            if i != len(self.root):
                raise StorageError(f"Missing message {len(self.root)} before {i}; restore from backup. IDs will not be renumbered.")
            self.root.append(messages[i])
        for value in self._records("tree"):
            try:
                n = Node(**value)
                assert type(n.l) is int and 0 <= n.l <= 52
                assert type(n.i) is int and n.i >= 0
                assert isinstance(n.text, str) and n.text.strip() and n.size == size(n.text)
                assert (n.i + 1) * 2**n.l <= len(self.root)
            except (TypeError, ValueError, AssertionError):
                raise StorageError("Invalid tree record; restore the damaged file from backup") from None
            key = n.l, n.i
            if key in self.tree:
                raise StorageError(f"Duplicate tree node {key}")
            self.tree[key] = n
        # A missing cached child can be rebuilt, but cannot be silently exposed
        # under a stored ancestor until reconstruction completes.

    def _append(self, stream: str, value: object):
        if self._lock < 0 or self._poisoned:
            raise StorageError("Store is closed or a previous write failed; reopen before continuing")
        file = self.path / stream / (datetime.now().astimezone().date().isoformat() + ".jsonl")
        is_new = not file.exists()
        data = (json_text(value) + "\n").encode("utf-8")
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise StorageError("Short log write; stopped to preserve permanent IDs")
            os.fsync(fd)
            if is_new:
                sync_dir(file.parent)
        except BaseException:
            self._poisoned = True
            raise
        finally:
            os.close(fd)

    def _append_many(self, stream: str, values: list):
        """Several records with one write and one fsync (bulk replication imports)."""
        if not values:
            return
        if self._lock < 0 or self._poisoned:
            raise StorageError("Store is closed or a previous write failed; reopen before continuing")
        file = self.path / stream / (datetime.now().astimezone().date().isoformat() + ".jsonl")
        is_new = not file.exists()
        data = "".join(json_text(v) + "\n" for v in values).encode("utf-8")
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = 0
            while written < len(data):
                n = os.write(fd, data[written:])
                if n <= 0:
                    raise StorageError("Short write while importing replicated records")
                written += n
            os.fsync(fd)
            if is_new:
                sync_dir(file.parent)
        except BaseException:
            self._poisoned = True
            raise
        finally:
            os.close(fd)

    def append(self, kind: str, text: str, date: str | None = None, origin=None, gid: str = "") -> Message:
        if kind not in KINDS or not isinstance(text, str):
            raise ValueError("Invalid message kind or text (reasoning is never a log kind)")
        text = valid_unicode(text)
        stamp = date or now()
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError("Message dates must include a timezone")
        origin = normalize_origin(origin)
        message = Message(len(self.root), kind, text, 0, stamp, origin, gid)
        message = Message(message.i, kind, text, size(message.source), stamp, origin, gid)
        self._append("main", asdict(message))
        self.root.append(message)
        return message

    def save_node(self, l: int, i: int, text: str) -> Node:
        key = l, i
        if key in self.tree:
            raise StorageError(f"Node {key} is immutable and already exists")
        if not text.strip() or l < 0 or i < 0 or (i + 1) * 2**l > len(self.root):
            raise ValueError("Invalid tree node")
        if l and any((l - 1, 2 * i + j) not in self.tree for j in (0, 1)):
            raise ValueError("Both children must be built before their parent")
        node = Node(l, i, text, size(text))
        self._append("tree", asdict(node))
        self.tree[key] = node
        return node

    def close(self):
        if self._lock >= 0:
            os.close(self._lock)
            self._lock = -1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
