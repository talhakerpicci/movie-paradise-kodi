"""The background service: watches Kodi's player and queues scrobble events.

Kodi does not call the Player callbacks when things happen. It queues them
and runs them on this thread while we sit in waitForAbort(), so a callback
reads the player as it is when it runs, not when the event happened. The
loop therefore handles each wait's callbacks as one batch: only the ones
after the last playback change in the batch (open, stop, end, error) can
trust what they read. Earlier AV starts are ignored and earlier pauses keep
their state change but not their timing.

A new playback is recognised by onPlayBackStarted, which Kodi also sends
for the next playlist item (with no stop for the previous one). The AV start
only tells us what it is.

Network requests run on a separate sender thread.
"""

import queue
import threading
import time
import uuid

import xbmc

from . import api, identity, kodi, log, playback
from .credentials import CredentialStore
from .outbox import HORIZON_SECONDS, Outbox, QueueFull
from .sender import (NOTICE_CLOCK, NOTICE_DROPPED, NOTICE_INCOMPATIBLE, NOTICE_PREMIUM, NOTICE_UNLINKED, Sender,
                     payload)

LOOP_SECONDS = 1.0
SEEK_SETTLE_SECONDS = 0.5
CHECKPOINT_KEY = "checkpoint"
# Kodi kills a script that takes more than 5 s to stop.
SENDER_JOIN_SECONDS = 4.0
# Older checkpoints are useless: the server refuses events older than a day.
CHECKPOINT_MAX_AGE = HORIZON_SECONDS - 3600
# Callbacks that change which playback is current.
_BOUNDARIES = ("open", "stop", "end", "error")
STARTUP_FLUSH_SECONDS = 0.01
STARTUP_ATTEMPTS = 5

_NOTICE_TEXT = {
    NOTICE_UNLINKED: 32044,
    NOTICE_PREMIUM: 32045,
    NOTICE_INCOMPATIBLE: 32046,
    NOTICE_DROPPED: 32047,
    NOTICE_CLOCK: 32053,
}


class Observation(object):
    """What one callback saw when it ran."""

    def __init__(self, kind, at, found=None, reason="", position_ms=None, duration_ms=None, path=None):
        self.kind = kind
        self.at = at
        self.found = found
        self.reason = reason
        self.position_ms = position_ms
        self.duration_ms = duration_ms
        self.path = path


class _Observer(xbmc.Player):
    def __init__(self, inbox, snapshot):
        xbmc.Player.__init__(self)
        self._inbox = inbox
        self._snapshot = snapshot

    def _timed(self, kind):
        position, duration = kodi.player_timing(self)
        self._inbox.put(Observation(kind, time.time(), position_ms=position, duration_ms=duration,
                                    path=kodi.playing_file(self) or None))

    def onPlayBackStarted(self):
        self._inbox.put(Observation("open", time.time()))

    def onAVStarted(self):
        at = time.time()
        found, reason, position, duration = self._snapshot(self)
        self._inbox.put(Observation("start", at, found, reason, position, duration, kodi.playing_file(self) or None))

    def onPlayBackPaused(self):
        self._timed("pause")

    def onPlayBackResumed(self):
        self._timed("resume")

    def onPlayBackSeek(self, time_ms, seek_offset):
        self._inbox.put(Observation("seek", time.time()))

    def onPlayBackSeekChapter(self, chapter):
        self._inbox.put(Observation("seek", time.time()))

    def onPlayBackStopped(self):
        self._inbox.put(Observation("stop", time.time()))

    def onPlayBackEnded(self):
        self._inbox.put(Observation("end", time.time()))

    def onPlayBackError(self):
        self._inbox.put(Observation("error", time.time()))


class _SenderThread(threading.Thread):
    def __init__(self, sender, monitor):
        threading.Thread.__init__(self, name="movieparadise-sender")
        self.daemon = True
        self._sender = sender
        self._monitor = monitor
        # Don't name these _stop: Thread.join() on Python 3.8 calls self._stop().
        self._wake_event = threading.Event()
        self._halt_event = threading.Event()

    def wake(self):
        self._wake_event.set()

    def halt(self):
        self._halt_event.set()
        self._wake_event.set()

    def run(self):
        while not self._halt_event.is_set() and not self._monitor.abortRequested():
            try:
                delay = self._sender.step()
            except Exception as err:
                log.error("delivery step failed: %s", type(err).__name__)
                delay = 30
            if delay == 0:
                continue
            self._wake_event.wait(timeout=300 if delay is None else min(delay, 300))
            self._wake_event.clear()


class Service(object):
    def __init__(self):
        kodi.install_log_sink()
        self._monitor = xbmc.Monitor()
        self._inbox = queue.Queue()
        self._player = _Observer(self._inbox, self._snapshot)
        profile = kodi.profile_dir()
        self._store = CredentialStore(profile)
        self._outbox = Outbox(profile.rstrip("/\\") + "/queue.sqlite")
        self._tracker = playback.Tracker(lambda: str(uuid.uuid4()))
        self._boot_id = kodi.boot_id()
        self._seek_due = None
        self._saved_latched = False
        user_agent = kodi.user_agent()

        def transport_for(creds):
            return api.Transport(creds.api_base or kodi.API_BASE, user_agent)

        self._sender = Sender(self._outbox, self._store, transport_for, self._notice)
        self._sender_thread = _SenderThread(self._sender, self._monitor)

    def _notice(self, kind):
        string_id = _NOTICE_TEXT.get(kind)
        if string_id:
            kodi.notify(kodi.text(string_id), error=kind in (NOTICE_UNLINKED, NOTICE_DROPPED))

    def _snapshot(self, player):
        """(identity or None, skip reason, position, duration) of what plays now.

        The identity's path is the file actually playing (for a plugin item,
        the resolved stream), so later checks compare like with like.
        """
        found, reason = identity.current(kodi.rpc, kodi.setting_bool("exclude_server_addons", True))
        position, duration = kodi.player_timing(player)
        if found is not None:
            found.path = kodi.playing_file(player) or found.path
            if not identity.plausible_length(found, duration):
                found, reason = None, identity.SKIP_EXTRA
        return found, reason, position, duration

    def _pairing_id(self):
        creds = self._store.load()
        return creds.pairing_id if creds is not None else None

    # --- queueing ---

    def _enqueue(self, events):
        """Queue events, but only if the account link they were recorded under
        is still the current one. If the user unlinked or linked another
        account, the events are dropped and the playback is forgotten."""
        if not events:
            return
        wrote = full = False
        with self._outbox.exclusive():
            current = self._pairing_id()
            for event in events:
                if event.pairing_id is None or event.pairing_id != current:
                    if self._tracker.active and self._tracker.session["pairing_id"] == event.pairing_id:
                        self._tracker.abandon()
                    continue
                checkpoint = (CHECKPOINT_KEY, None) if event.terminal else None
                try:
                    self._outbox.add(str(uuid.uuid4()), event.pairing_id, event.session_id, event.action,
                                     event.occurred_at, payload(event), terminal=event.terminal, checkpoint=checkpoint)
                    wrote = True
                except QueueFull:
                    full = True
            self._save_checkpoint()
        if full:
            kodi.notify(kodi.text(32048), error=True)
        if wrote:
            self._sender_thread.wake()

    def _save_checkpoint(self):
        value = self._tracker.checkpoint(self._boot_id) if self._tracker.active else None
        self._outbox.set_meta(CHECKPOINT_KEY, value)
        self._saved_latched = self._tracker.latched

    def _save_latch(self):
        """Save the checkpoint as soon as the playback passes 90 %, so a crash
        after that point still counts as watched."""
        if self._tracker.latched and not self._saved_latched:
            self._save_checkpoint()

    # --- observations ---

    def _handle(self, obs, trusted=True):
        """Apply one observation. `trusted` is False when a later callback in
        the same batch changed the playback, so what it read may be stale."""
        if obs.kind == "open":
            # A new file opened, so the current playback is over.
            self._seek_due = None
            self._enqueue(self._tracker.stop(obs.at, natural_end=False))
        elif obs.kind == "start":
            if not trusted:
                return
            self._seek_due = None
            if self._tracker.active and obs.path is not None and obs.path == self._tracker.session["path"]:
                # Same file with no open in between: a repeated AV start.
                self._tracker.sample(obs.position_ms, obs.duration_ms, obs.at, path=obs.path)
                return
            if obs.found is None:
                log.info("not shared: %s", obs.reason)
                self._enqueue(self._tracker.stop(obs.at, natural_end=False))
                return
            pairing_id = self._pairing_id()
            if pairing_id is None:
                self._enqueue(self._tracker.stop(obs.at, natural_end=False))
                return
            self._enqueue(self._tracker.start(obs.found, obs.position_ms, obs.duration_ms, obs.at, pairing_id))
        elif obs.kind in ("pause", "resume"):
            position, duration = (obs.position_ms, obs.duration_ms) if trusted else (None, None)
            change = self._tracker.pause if obs.kind == "pause" else self._tracker.resume
            self._enqueue(change(position, duration, obs.at, path=obs.path))
        elif obs.kind == "seek":
            if trusted:
                self._seek_due = time.time() + SEEK_SETTLE_SECONDS
        elif obs.kind == "end":
            self._seek_due = None
            self._enqueue(self._tracker.stop(obs.at, natural_end=True))
        elif obs.kind in ("stop", "error"):
            self._seek_due = None
            self._enqueue(self._tracker.stop(obs.at, natural_end=False))

    def _current_path(self):
        return kodi.playing_file(self._player) or None

    def _drain(self):
        batch = []
        while True:
            try:
                batch.append(self._inbox.get_nowait())
            except queue.Empty:
                return batch

    def _settle(self):
        """Read the player at startup.

        Callbacks queued before we first wait describe events we can't order
        against the reading. So: read, let Kodi deliver, and if a playback
        change came in, read again. The callbacks delivered along the way are
        dropped, since the final reading already reflects them. Returns None
        if Kodi is shutting down or playback keeps changing.
        """
        for _ in range(STARTUP_ATTEMPTS):
            found, reason, position, duration = self._snapshot(self._player)
            playing = not kodi.is_paused()
            if self._monitor.waitForAbort(STARTUP_FLUSH_SECONDS):
                return None
            late = self._drain()
            if not any(obs.kind in _BOUNDARIES for obs in late):
                for obs in late:
                    self._inbox.put(obs)
                return found, reason, position, duration, playing
        log.warning("playback kept changing at startup; adopting nothing")
        return None

    def _resume_after_restart(self, reading):
        """Pick up where the last run left off: continue the checkpointed
        playback if it is still playing, otherwise send its stop and start
        tracking whatever plays now."""
        now = time.time()
        checkpoint = self._outbox.get_meta(CHECKPOINT_KEY)
        found, reason, position, duration, playing = reading
        pairing_id = self._pairing_id()
        if found is not None and pairing_id is not None and position is not None:
            if self._tracker.resume_from(checkpoint, found, position, duration, playing, self._boot_id, now,
                                         pairing_id, CHECKPOINT_MAX_AGE):
                log.info("continuing the playback after a service restart")
                self._save_checkpoint()
                return
            events = playback.stop_from_checkpoint(checkpoint, now, CHECKPOINT_MAX_AGE)
            events.extend(self._tracker.start(found, position, duration, now, pairing_id))
            if not playing:
                events.extend(self._tracker.pause(position, duration, now))
            self._enqueue(events)
            return
        if found is None and reason != identity.SKIP_NOT_PLAYING:
            log.info("not shared: %s", reason)
        self._enqueue(playback.stop_from_checkpoint(checkpoint, now, CHECKPOINT_MAX_AGE))
        self._outbox.set_meta(CHECKPOINT_KEY, None)

    def _loop_once(self):
        batch = self._drain()
        last_change = max([i for i, obs in enumerate(batch) if obs.kind in _BOUNDARIES] or [-1])
        for i, obs in enumerate(batch):
            self._handle(obs, trusted=i >= last_change)
        now = time.time()
        if self._seek_due is not None and now >= self._seek_due:
            self._seek_due = None
            position, duration = kodi.player_timing(self._player)
            if self._tracker.active and self._current_path() == self._tracker.session["path"]:
                self._enqueue(self._tracker.seek(position, duration, now))
        if self._tracker.state == playback.PLAYING:
            position, duration = kodi.player_timing(self._player)
            self._tracker.sample(position, duration, now, path=self._current_path())
            self._enqueue(self._tracker.tick(now))
        self._save_latch()

    def run(self):
        log.info("service started")
        self._sender_thread.start()
        try:
            try:
                reading = self._settle()
                if reading is not None:
                    self._resume_after_restart(reading)
            except Exception as err:
                log.error("startup check failed: %s", type(err).__name__)
            while not self._monitor.abortRequested():
                try:
                    self._loop_once()
                except Exception as err:
                    log.error("playback step failed: %s", type(err).__name__)
                if self._monitor.waitForAbort(LOOP_SECONDS):
                    break
        finally:
            self._sender_thread.halt()
            self._sender_thread.join(SENDER_JOIN_SECONDS)
            if self._sender_thread.is_alive():
                # Still inside a request: keep the queue open for its answer.
                log.warning("the sender did not stop in time; leaving the queue open")
            else:
                self._outbox.close()
            log.info("service stopped")


def run():
    Service().run()
