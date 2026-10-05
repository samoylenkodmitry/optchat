"""Idempotent event ingestion, including the crash between intent and journal write.

Recorded events go to this machine's own journal with a global id
"<machine>/<seq>"; the replica then places them in the shared order.
"""
import hashlib
import sqlite3
import os
import json

from .util import json_text, now, valid_unicode
from .storage import normalize_origin


class Ledger:
    def __init__(self, store, replica):
        self.store, self.replica = store, replica
        self.db = sqlite3.connect(store.path / "delivery.sqlite3")
        os.chmod(store.path / "delivery.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, gid TEXT UNIQUE, kind TEXT, text TEXT, date TEXT, digest TEXT, done INTEGER, origin TEXT NOT NULL DEFAULT '{}')")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, role TEXT)")
        if "gid" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            raise RuntimeError("delivery.sqlite3 predates replication; move it aside before starting")
        self.db.commit()
        self.failed = False
        for key, gid, kind, text, date, origin in self.db.execute("SELECT key,gid,kind,text,date,origin FROM events WHERE done=0").fetchall():
            self._recover(gid, kind, text, date, json.loads(origin))
            self.db.execute("UPDATE events SET done=1, text=NULL WHERE key=?", (key,))
        self.db.commit()

    def _recover(self, gid, kind, text, date, origin):
        own = self.replica.own_by_gid.get(gid)
        if own is None and gid == self.replica.next_gid():
            self.replica.record(kind, text, date, origin, gid)
        elif own is None or (own["kind"], own["text"], own["date"], own["origin"]) != (kind, text, date, origin):
            raise RuntimeError(f"Delivery ledger conflicts with journal entry {gid}")

    def append(self, key, kind, text, date=None, origin=None):
        if self.failed:
            raise RuntimeError("A previous delivery failed; restart the service to recover its durable intent")
        if not isinstance(key, str) or not key or len(key) > 500:
            raise ValueError("A stable event_id of 1–500 characters is required")
        if kind not in {"user", "talk", "tool", "echo", "note"} or not isinstance(text, str):
            raise ValueError("Invalid message kind/text")
        text = valid_unicode(text)
        origin = normalize_origin(origin)
        digest = hashlib.sha256(json_text([kind, text, date] + ([origin] if origin else [])).encode()).hexdigest()
        existing = self.db.execute("SELECT gid,digest FROM events WHERE key=?", (key,)).fetchone()
        if existing:
            if existing[1] != digest:
                raise ValueError("event_id was already used for different content")
            return existing[0]
        from datetime import datetime
        stamp = date or now()
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError("Dates must include a timezone")
        gid = self.replica.next_gid()
        self.db.execute("INSERT INTO events (key,gid,kind,text,date,digest,done,origin) VALUES (?,?,?,?,?,?,0,?)", (key, gid, kind, text, stamp, digest, json_text(origin)))
        self.db.commit()
        try:
            self.replica.record(kind, text, stamp, origin, gid)
            self.db.execute("UPDATE events SET done=1,text=NULL WHERE key=?", (key,))
            self.db.commit()
        except BaseException:
            self.failed = True
            raise
        return gid

    def session(self, key, role=None):
        if role is not None:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (key, role))
            self.db.commit()
        row = self.db.execute("SELECT role FROM sessions WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def close(self):
        self.db.close()
