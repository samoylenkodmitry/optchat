from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .storage import Store
from .util import PLACEHOLDER, VIEW, flat, size


ZOOM_CHARS = 12_000  # Characters of one message that zoom returns. read_message gives the rest.
SNIPPET = 240  # Characters of a message in a line of search or thread.
THREAD_LIMIT = 30  # Most messages that one thread call returns.


@dataclass(frozen=True)
class Part:
    l: int
    i: int

    @property
    def n(self):
        return 2**self.l

    @property
    def start(self):
        return self.i * self.n

    @property
    def end(self):
        return self.start + self.n

    @property
    def key(self):
        return self.l, self.i


class Memory:
    def __init__(self, store: Store, budget: int = VIEW):
        if budget < 1:
            raise ValueError("View budget must be positive")
        self.store, self.budget = store, budget
        self._parts = {}
        self._ordered = None
        self._candidates = {}
        self._bytes = 0
        self._first = 0
        self.complete: set[tuple[int, int]] = set()
        self.changed = asyncio.Event()
        self.listeners = []
        self.total = 0
        for l, i in sorted(store.tree):
            if l == 0 or all((l - 1, i * 2 + j) in self.complete for j in (0, 1)):
                self.complete.add((l, i))
        # Fold with the historical T at each append, not the final root length.
        for m in store.root:
            self.total = m.i + 1
            self._append_part(Part(0, m.i))
            self.fit()
        while (0, self._first) in self.complete:
            self._first += 1

    @property
    def view(self):
        if self._ordered is None:
            self._ordered = sorted(self._parts.values(), key=lambda p: p.start)
        return self._ordered

    def _offer_merge(self, parent):
        if self.built(parent) and all((parent.l - 1, parent.i * 2 + j) in self._parts for j in (0, 1)):
            self._candidates[parent.key] = parent

    def _append_part(self, p):
        self._parts[p.key] = p
        self._ordered = None
        self._bytes += self.part_size(p)
        self._offer_merge(Part(p.l + 1, p.i // 2))

    def part_size(self, p):
        return self.store.tree[p.key].size if self.built(p) else size(PLACEHOLDER)

    def built(self, p: Part):
        return p.key in self.complete

    def text(self, p: Part):
        return self.store.tree[p.key].text if self.built(p) else PLACEHOLDER

    @property
    def bytes(self):
        return self._bytes

    def first(self):
        return self._first

    def append(self, kind: str, text: str, date: str | None = None, origin=None, gid: str = ""):
        m = self.store.append(kind, text, date, origin, gid)
        self.total += 1
        self._append_part(Part(0, m.i))
        self.fit()
        return m

    def node_built(self, p: Part):
        # Reconnect stored ancestors after a missing child was rebuilt.
        while p.key in self.store.tree:
            if p.l and not all((p.l - 1, p.i * 2 + j) in self.complete for j in (0, 1)):
                break
            if p.key in self._parts and p.key not in self.complete:
                self._bytes += self.store.tree[p.key].size - size(PLACEHOLDER)
            self.complete.add(p.key)
            if p.l:
                self._offer_merge(p)
            p = Part(p.l + 1, p.i // 2)
        while (0, self._first) in self.complete:
            self._first += 1
        self.fit()

    def fit(self):
        current = self.bytes
        while current > self.budget and self._candidates:
            parent = max(self._candidates.values(), key=lambda p: ((self.total - p.start) / 2**(p.l + 1), -p.start))
            del self._candidates[parent.key]
            children = [Part(parent.l - 1, parent.i * 2 + j) for j in (0, 1)]
            current += self.part_size(parent) - sum(self.part_size(p) for p in children)
            for child in children:
                del self._parts[child.key]
            self._parts[parent.key] = parent
            self._ordered = None
            self._offer_merge(Part(parent.l + 1, parent.i // 2))
        self._bytes = current
        self.changed.set()
        for callback in self.listeners:
            callback()

    def render(self, *, ids: bool = True, end: int | None = None, strict: bool = False):
        lines = []
        for p in self.view:
            if end is not None and p.end > end:
                break
            if strict and not self.built(p):
                raise RuntimeError("Attempt to send an unsummarized view to a model")
            prefix = f"{p.start}+{p.n}|" if ids else ""
            lines.append(prefix + flat(self.text(p)))
        return "<chat>\n" + "\n".join(lines) + "\n</chat>"

    def bounded_view(self, end):
        # Match fit(): the VIEW budget counts only summary text. Range labels do not count.
        lines, used, covered = [], 0, 0
        for p in self.view:
            if p.end > end or not self.built(p):
                break
            line = f"{p.start}+{p.n}|{flat(self.text(p))}"
            if used + self.part_size(p) > self.budget:
                break
            lines.append(line)
            used += self.part_size(p)
            covered = p.end
        return "<chat>\n" + "\n".join(lines) + "\n</chat>", covered

    async def settle(self):
        while self.first() < self.total:
            self.changed.clear()
            # No await between the predicate and subscribing to the event.
            await self.changed.wait()

    def zoom(self, id: int, n: int):
        missing = f"No line {id}+{n}."
        if type(id) is not int or type(n) is not int or n < 1 or id < 0:
            return missing
        if n & (n - 1) or id % n or id + n > self.total:
            return missing
        if n == 1:
            source = self.store.root[id].source
            if len(source) <= ZOOM_CHARS:
                return f"{id}+0|{source}"
            return f"{id}+0|{source[:ZOOM_CHARS]}\n[The message has {len(source)} characters. read_message({id}, {ZOOM_CHARS}) gives the rest.]"
        level = n.bit_length() - 2
        children = [Part(level, id // (n // 2) + j) for j in (0, 1)]
        if not all(self.built(p) for p in children):
            return missing
        return "\n".join(f"{p.start}+{p.n}|{flat(self.text(p))}" for p in children)

    def search(self, query: str, limit: int = 10):
        """Original messages that contain the most words of the query: the words of
        the user first, then replies, then the rest; newer before older."""
        import re
        if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Give a query and a limit from 1 to 50.")
        terms = sorted({t for t in re.findall(r"\w+", query.casefold()) if len(t) > 1})
        if not terms:
            raise ValueError("The query has no words.")
        rank = {"user": 0, "talk": 1, "note": 2, "echo": 3, "tool": 4}
        from .activity import stored_kind
        hits = []
        for m in self.store.root:
            text = m.text.casefold()
            found = [t for t in terms if t in text]
            kind = stored_kind(m.kind, m.text) if found else None
            if kind:
                hits.append((-len(found), rank.get(kind, 5), -m.i, m, found))
        hits.sort(key=lambda h: h[:3])
        lines = [self.line(m, max(0, m.text.casefold().find(found[0]) - 80)) for _, _, _, m, found in hits[:limit]]
        head = f"{len(hits)} messages match; the best {len(lines)} follow. zoom(id, 1) gives a whole message."
        return head + ("\n" + "\n".join(lines) if lines else "")

    @staticmethod
    def line(m, at: int = 0):
        """One message as a line of search or thread, from character `at`."""
        snippet = flat(m.text[at:at + SNIPPET])
        return f"{m.i}+0|{m.label} {m.date[:16]}: {'...' if at else ''}{snippet}{'...' if at + SNIPPET < len(m.text) else ''}"

    def thread(self, id: int, n: int = 5):
        """The next n messages after message id that belong to its session, oldest first."""
        from .activity import stored_kind
        if type(id) is not int or not 0 <= id < self.total or type(n) is not int or not 1 <= n <= THREAD_LIMIT:
            raise ValueError(f"Give a message id and an n from 1 to {THREAD_LIMIT}.")
        first = self.store.root[id]
        session = first.origin.get("session")
        if not session:
            return f"Message {id} has no session."
        later = [m for m in self.store.root[id + 1:] if m.origin.get("session") == session and stored_kind(m.kind, m.text)]
        lines = [self.line(m) for m in later[:n]]
        more = f" thread({later[n - 1].i}, n) continues." if len(later) > n else ""
        return f"Later messages in {first.session_tag}: {len(later)}; the next {len(lines)} follow.{more}" + ("\n" + "\n".join(lines) if lines else "")

    def date(self, id: int):
        from datetime import datetime
        if type(id) is not int or not 0 <= id < self.total:
            return f"No message {id}."
        return datetime.fromisoformat(self.store.root[id].date).astimezone().isoformat(sep=" ")

    def read_message(self, id: int, offset: int):
        from .util import json_text
        if type(id) is not int or not 0 <= id < self.total or type(offset) is not int or offset < 0:
            raise ValueError("Invalid message id or character offset")
        text = self.store.root[id].source
        if offset > len(text):
            raise ValueError("Offset is past the end of the message")
        end = min(len(text), offset + 12_000)
        # JSON escaping can expand characters; return a plain header + exact text.
        return json_text({"id": id, "offset": offset, "end": end, "total": len(text), "next_offset": end if end < len(text) else None}) + "\n" + text[offset:end]
