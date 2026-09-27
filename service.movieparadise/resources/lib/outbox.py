"""The event queue, a small SQLite database in the add-on's profile.

Notes:
- An event is marked as attempted when it is picked for sending, before the
  request goes out. After that its id and body never change, so every retry
  is the same request (the server deduplicates on the event id).
- A newer progress event replaces an older one of the same playback, but
  only if the older one was never sent.
- Events are kept until the server confirms it processed them, refuses them
  for good, or they are older than 23 hours (the server takes up to 24).
- Events of one playback go in order. Stops go before other playbacks'
  progress.
- A hold (Retry-After, or an hour after a 403) pauses every event of that
  account link, including ones queued later.
- The queue is bounded. Unsent progress is dropped first to make room, and a
  small reserve is kept for stops.
- The settings dialog runs in its own interpreter with its own connection.
  Linking and unlinking happen inside exclusive(), a write transaction that
  also works as a lock between the two.
"""

import contextlib
import functools
import json
import sqlite3
import threading
import time

HORIZON_SECONDS = 23 * 3600
MAX_ROWS = 1000
MAX_BYTES = 2 * 1024 * 1024
TERMINAL_RESERVE_ROWS = 50
TERMINAL_RESERVE_BYTES = 64 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    pairing_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    terminal INTEGER NOT NULL,
    occurred_at REAL NOT NULL,
    payload TEXT NOT NULL,
    attempted INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_session ON events (pairing_id, session_id, seq);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_HEAD = """FROM events e
           WHERE pairing_id = ?
             AND NOT EXISTS (SELECT 1 FROM events o WHERE o.pairing_id = e.pairing_id AND o.session_id = e.session_id AND o.seq < e.seq)"""

_COLUMNS = "seq, event_id, pairing_id, session_id, action, terminal, occurred_at, payload, attempted, attempts, next_at"


class Event(object):
    def __init__(self, seq, event_id, pairing_id, session_id, action, terminal, occurred_at, payload, attempted, attempts, next_at):
        self.seq = seq
        self.event_id = event_id
        self.pairing_id = pairing_id
        self.session_id = session_id
        self.action = action
        self.terminal = bool(terminal)
        self.occurred_at = occurred_at
        self.payload = payload
        self.attempted = bool(attempted)
        self.attempts = attempts
        self.next_at = next_at

    def body(self):
        return json.loads(self.payload)


class QueueFull(Exception):
    """No room left for this event."""


def _locked(method):
    """The connection is shared by the service loop and the sender thread."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _hold_key(pairing_id):
    return "hold:" + pairing_id


class Outbox(object):
    def __init__(self, path, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    @_locked
    def close(self):
        self._db.close()

    @contextlib.contextmanager
    def _transaction(self):
        """A write transaction, or a savepoint when already inside one."""
        if self._db.in_transaction:
            self._db.execute("SAVEPOINT nested")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK TO nested")
                self._db.execute("RELEASE nested")
                raise
            self._db.execute("RELEASE nested")
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    @contextlib.contextmanager
    def exclusive(self):
        """Hold the database write lock, across connections, for the block."""
        with self._lock:
            with self._transaction():
                yield

    # --- meta (the playback checkpoint, notices, holds) ---

    def _get_meta(self, key):
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_meta(self, key, value):
        if value is None:
            self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    @_locked
    def get_meta(self, key):
        return self._get_meta(key)

    @_locked
    def set_meta(self, key, value):
        self._set_meta(key, value)

    @_locked
    def hold_until(self, pairing_id):
        value = self._get_meta(_hold_key(pairing_id))
        return float(value) if value is not None else 0.0

    @_locked
    def hold(self, pairing_id, until):
        """Send nothing for this link before `until`. Never shortens a hold."""
        if until > self.hold_until(pairing_id):
            self._set_meta(_hold_key(pairing_id), until)

    # --- events ---

    def _usage(self):
        return self._db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM events").fetchone()

    def _fits(self, size, terminal):
        rows, total = self._usage()
        row_cap = MAX_ROWS if terminal else MAX_ROWS - TERMINAL_RESERVE_ROWS
        byte_cap = MAX_BYTES if terminal else MAX_BYTES - TERMINAL_RESERVE_BYTES
        return rows + 1 <= row_cap and total + size <= byte_cap

    def _make_room(self, size, terminal):
        """Drop the oldest unsent progress until the event fits."""
        while not self._fits(size, terminal):
            victims = self._db.execute(
                "SELECT seq FROM events WHERE action = 'progress' AND attempted = 0 ORDER BY seq LIMIT 50").fetchall()
            if not victims:
                return False
            self._db.executemany("DELETE FROM events WHERE seq = ?", victims)
        return True

    @_locked
    def add(self, event_id, pairing_id, session_id, action, occurred_at, payload, terminal=False, checkpoint=None):
        """Queue one event. With `checkpoint`, the playback checkpoint is saved
        in the same transaction. Raises QueueFull if there is no room."""
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._transaction():
            if action == "progress":
                # Replace an unsent progress event of the same playback.
                self._db.execute(
                    "DELETE FROM events WHERE pairing_id = ? AND session_id = ? AND action = 'progress' AND attempted = 0",
                    (pairing_id, session_id))
            if not self._make_room(len(body), terminal):
                raise QueueFull()
            self._db.execute(
                "INSERT INTO events (event_id, pairing_id, session_id, action, terminal, occurred_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, pairing_id, session_id, action, 1 if terminal else 0, occurred_at, body))
            if checkpoint is not None:
                self._set_meta(checkpoint[0], checkpoint[1])

    @_locked
    def claim_next(self, pairing_id, lease_seconds, now=None):
        """Pick the next event to send and mark it attempted, with a lease so
        it isn't picked again while the request runs."""
        now = self._clock() if now is None else now
        if now < self.hold_until(pairing_id):
            return None
        row = self._db.execute(
            "SELECT " + _COLUMNS + " " + _HEAD + " AND next_at <= ? ORDER BY terminal DESC, seq LIMIT 1",
            (pairing_id, now)).fetchone()
        if row is None:
            return None
        event = Event(*row)
        self._db.execute("UPDATE events SET attempted = 1, attempts = attempts + 1, next_at = ? WHERE seq = ?",
                         (now + lease_seconds, event.seq))
        return event

    @_locked
    def next_wake(self, pairing_id):
        """When the next event of this link can be sent."""
        row = self._db.execute("SELECT MIN(next_at) " + _HEAD, (pairing_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return max(float(row[0]), self.hold_until(pairing_id))

    @_locked
    def reschedule(self, event_id, next_at):
        self._db.execute("UPDATE events SET next_at = ? WHERE event_id = ?", (next_at, event_id))

    @_locked
    def delete(self, event_id):
        self._db.execute("DELETE FROM events WHERE event_id = ?", (event_id,))

    @_locked
    def expire(self, now=None):
        """Drop events the server would refuse as too old. Returns how many stops were dropped."""
        now = self._clock() if now is None else now
        cutoff = now - HORIZON_SECONDS
        terminals = self._db.execute("SELECT COUNT(*) FROM events WHERE occurred_at < ? AND terminal = 1", (cutoff,)).fetchone()[0]
        self._db.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff,))
        return terminals

    @_locked
    def purge_other_links(self, pairing_id):
        self._db.execute("DELETE FROM events WHERE pairing_id != ?", (pairing_id,))
        self._db.execute("DELETE FROM meta WHERE key LIKE 'hold:%' AND key != ?", (_hold_key(pairing_id),))

    @_locked
    def purge_link(self, pairing_id):
        self._db.execute("DELETE FROM events WHERE pairing_id = ?", (pairing_id,))
        self._set_meta(_hold_key(pairing_id), None)

    @_locked
    def purge_all(self):
        self._db.execute("DELETE FROM events")
        self._db.execute("DELETE FROM meta WHERE key LIKE 'hold:%'")

    @_locked
    def count(self, pairing_id=None):
        if pairing_id is None:
            return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return self._db.execute("SELECT COUNT(*) FROM events WHERE pairing_id = ?", (pairing_id,)).fetchone()[0]
