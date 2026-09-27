"""Playback state: turns what Kodi reports into scrobble events.

Each playback gets its own session id, kept through pause, resume and
seeks. Playing the same file again starts a new session. The stop is sent
once, with `completed` set when the title ended naturally or the playback
reached 90 % at any point (seeking back afterwards does not undo it).

Timing is only taken from the playback's own file, so a late reading from
the next playlist item cannot move the position or the watched flag.
"""

THRESHOLD = 0.90
PROGRESS_EVERY_SECONDS = 60.0

PLAYING = "playing"
PAUSED = "paused"


class Event(object):
    """One event for the queue. `pairing_id` is the account link the playback
    started under; the event is only ever sent with that link."""

    def __init__(self, action, session_id, media, occurred_at, position_ms, duration_ms, completed=None, pairing_id=None):
        self.action = action
        self.pairing_id = pairing_id
        self.session_id = session_id
        self.media = media
        self.occurred_at = occurred_at
        self.position_ms = position_ms
        self.duration_ms = duration_ms
        self.completed = completed

    @property
    def terminal(self):
        return self.action == "stop"


def clamp_timing(position_ms, duration_ms):
    """Integer milliseconds the API accepts, or (None, None) without a length."""
    try:
        duration = int(duration_ms)
        position = int(position_ms)
    except (TypeError, ValueError):
        return None, None
    if duration <= 0:
        return None, None
    return max(0, min(position, duration)), duration


class Tracker(object):
    def __init__(self, new_id):
        self._new_id = new_id
        self.session = None

    # --- state ---

    @property
    def active(self):
        return self.session is not None

    @property
    def state(self):
        return self.session["state"] if self.session else None

    @property
    def latched(self):
        return self.session is not None and self.session["max_ratio"] >= THRESHOLD

    def checkpoint(self, boot_id):
        """State saved so a restarted service can pick the playback up again."""
        if self.session is None:
            return None
        s = self.session
        return {
            "session_id": s["id"], "key": s["key"], "path": s["path"], "media": s["media"], "state": s["state"],
            "position_ms": s["position_ms"], "duration_ms": s["duration_ms"], "at": s["sampled_at"],
            "max_ratio": s["max_ratio"], "boot_id": boot_id, "pairing_id": s["pairing_id"],
        }

    # --- observations ---

    def _event(self, action, now, completed=None):
        s = self.session
        return Event(action, s["id"], s["media"], now, s["position_ms"], s["duration_ms"], completed, s["pairing_id"])

    def _observe(self, position_ms, duration_ms, now, path=None):
        """Record a position reading. Readings from another file are ignored."""
        s = self.session
        s["sampled_at"] = now
        if path is not None and path != s["path"]:
            return
        position, duration = clamp_timing(position_ms, duration_ms)
        if duration is None:
            return
        s["position_ms"], s["duration_ms"] = position, duration
        s["max_ratio"] = max(s["max_ratio"], float(position) / float(duration))

    def is_duplicate_start(self, identity):
        """Same title and file with no stop in between: the same playback."""
        s = self.session
        return s is not None and s["key"] == identity.key and s["path"] == identity.path

    def start(self, identity, position_ms, duration_ms, now, pairing_id=None):
        """A new playback. Stops the previous one first, if any."""
        if self.is_duplicate_start(identity):
            self._observe(position_ms, duration_ms, now, identity.path)
            return []
        events = []
        if self.session is not None:
            events.extend(self.stop(now, natural_end=False))
        self.session = {
            "id": self._new_id(), "key": identity.key, "path": identity.path, "media": identity.media, "state": PLAYING,
            "position_ms": None, "duration_ms": None, "max_ratio": 0.0,
            "sampled_at": now, "progress_at": now, "pairing_id": pairing_id,
        }
        self._observe(position_ms, duration_ms, now)
        events.append(self._event("start", now))
        return events

    def resume_from(self, checkpoint, identity, position_ms, duration_ms, playing, boot_id, now, pairing_id, max_age):
        """Continue a checkpointed playback after a service restart.

        Requires the same Kodi process, account link, title and file. The
        position does not matter (the user may have seeked or paused for a
        long time). At worst a replay made while the service was down gets
        merged into the old session, which is better than counting it twice.
        """
        if not checkpoint or not checkpoint.get("session_id") or checkpoint.get("boot_id") != boot_id:
            return False
        if checkpoint.get("key") != identity.key or (checkpoint.get("path") or "") != identity.path:
            return False
        if pairing_id is None or checkpoint.get("pairing_id") != pairing_id:
            return False
        age = now - float(checkpoint.get("at") or 0)
        if age < 0 or age > max_age:
            return False
        self.session = {
            "id": checkpoint["session_id"], "key": identity.key, "path": identity.path, "media": identity.media,
            "state": PLAYING if playing else PAUSED,
            "position_ms": checkpoint.get("position_ms"), "duration_ms": checkpoint.get("duration_ms"),
            "max_ratio": float(checkpoint.get("max_ratio") or 0.0), "sampled_at": now, "progress_at": now,
            "pairing_id": pairing_id,
        }
        self._observe(position_ms, duration_ms, now)
        return True

    def sample(self, position_ms, duration_ms, now, path=None):
        """A quiet reading (every second while playing)."""
        if self.session is not None:
            self._observe(position_ms, duration_ms, now, path)

    def abandon(self):
        """Drop the playback without an event (its link is gone)."""
        self.session = None

    def tick(self, now):
        """A progress event every minute while playing."""
        s = self.session
        if s is None or s["state"] != PLAYING or now - s["progress_at"] < PROGRESS_EVERY_SECONDS:
            return []
        s["progress_at"] = now
        return [self._event("progress", now)]

    def pause(self, position_ms, duration_ms, now, path=None):
        s = self.session
        if s is None or s["state"] == PAUSED:
            return []
        self._observe(position_ms, duration_ms, now, path)
        s["state"] = PAUSED
        return [self._event("pause", now)]

    def resume(self, position_ms, duration_ms, now, path=None):
        s = self.session
        if s is None or s["state"] == PLAYING:
            return []
        self._observe(position_ms, duration_ms, now, path)
        s["state"] = PLAYING
        s["progress_at"] = now
        return [self._event("resume", now)]

    def seek(self, position_ms, duration_ms, now, path=None):
        """After a seek: progress while playing, pause while paused (the server
        treats progress as playing)."""
        s = self.session
        if s is None:
            return []
        self._observe(position_ms, duration_ms, now, path)
        if s["state"] == PAUSED:
            return [self._event("pause", now)]
        s["progress_at"] = now
        return [self._event("progress", now)]

    def stop(self, now, natural_end):
        """The stop event, with the last known timing."""
        s = self.session
        if s is None:
            return []
        event = self._event("stop", now, completed=bool(natural_end) or s["max_ratio"] >= THRESHOLD)
        self.session = None
        return [event]


def stop_from_checkpoint(checkpoint, now, max_age):
    """The stop for a checkpointed playback that ended while the service was
    not running (Kodi crashed or was closed). Uses the last known time and
    position. Returns nothing if the checkpoint is too old for the server."""
    if not checkpoint or not checkpoint.get("session_id") or not checkpoint.get("pairing_id"):
        return []
    at = float(checkpoint.get("at") or 0)
    if at <= 0 or now - at > max_age:
        return []
    completed = float(checkpoint.get("max_ratio") or 0.0) >= THRESHOLD
    return [Event("stop", checkpoint["session_id"], checkpoint.get("media") or {}, at, checkpoint.get("position_ms"),
                  checkpoint.get("duration_ms"), completed, checkpoint["pairing_id"])]
