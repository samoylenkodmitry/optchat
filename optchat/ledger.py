"""Records each event once, also after a crash between the intent and the journal write.

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
        # Tool calls of a turn that has not ended yet; Stop turns them into one record.
        self.db.execute("CREATE TABLE IF NOT EXISTS turn (key TEXT PRIMARY KEY, session TEXT, at REAL, origin TEXT, item TEXT)")
        # Chats whose main agent has used an OptChat tool.
        self.db.execute("CREATE TABLE IF NOT EXISTS capable (session TEXT PRIMARY KEY)")
        if "gid" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            raise RuntimeError("delivery.sqlite3 is older than replication. Move it away before you start the service.")
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
            raise RuntimeError("A previous delivery failed. Restart the service, which then completes the saved intent.")
        if not isinstance(key, str) or not key or len(key) > 500:
            raise ValueError("A stable event_id of 1 to 500 characters is required.")
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

    def mark_capable(self, session):
        self.db.execute("INSERT OR IGNORE INTO capable VALUES (?)", (session,))
        self.db.commit()

    def is_capable(self, session):
        return self.db.execute("SELECT 1 FROM capable WHERE session=?", (session,)).fetchone() is not None

    def hold(self, key, session, origin, item, at):
        self.db.execute("INSERT OR IGNORE INTO turn VALUES (?,?,?,?,?)", (key, session, at, json_text(origin), json_text(item)))
        self.db.commit()

    def held(self, session):
        rows = self.db.execute("SELECT key, origin, item FROM turn WHERE session=? ORDER BY rowid", (session,)).fetchall()
        return [k for k, _, _ in rows], (json.loads(rows[-1][1]) if rows else {}), [json.loads(i) for _, _, i in rows]

    def release_turn(self, session):
        self.db.execute("DELETE FROM turn WHERE session=?", (session,))
        self.db.commit()

    def stale_turns(self, before):
        return [s for (s,) in self.db.execute("SELECT session FROM turn GROUP BY session HAVING MAX(at) < ?", (before,))]

    def close(self):
        self.db.close()
