"""End-to-end tests of the service against a fake Kodi.

Like the real one, the fake runs Player callbacks late, while the service
waits, and each callback sees the player as it is at that moment. Every loop
wait applies one scripted step, advances the clock and then delivers the
owed callbacks. The short startup waits only deliver.
"""

import json
import sqlite3
import sys
import threading
import time as real_time

import pytest
import xbmc

from resources.lib import api, kodi, log, service as service_mod
from resources.lib.credentials import CredentialStore, Credentials
from resources.lib.outbox import Outbox

MIN = 60_000
NOW = 1_790_000_000.0
A = {"type": "movie", "id": -1, "file": "/m/a.mkv", "title": "A", "uniqueid": {"tmdb": "101"}}
B = {"type": "movie", "id": -1, "file": "/m/b.mkv", "title": "B", "uniqueid": {"tmdb": "202"}}
A_MEDIA = {"movie": {"ids": {"tmdb": 101}}}


class Clock(object):
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


class FakeKodi(object):
    def __init__(self, clock):
        self.clock = clock
        self.item = None
        self.paused = False
        self.owed = []  # callbacks Kodi runs at the service's next wait
        self.steps = []  # one per loop wait
        self.flush_hooks = []  # one per short wait, run after its delivery
        self.before_flush = []  # one per short wait, run before its delivery
        self.lookup_fails = False  # Player.GetItem answers nothing
        self.player = None
        self.monitor = None

    # --- what Kodi does (most of it owes the service a callback) ---

    def start(self, item, position_ms, duration_ms):
        """A file opens and its first frame shows (Kodi: onPlayBackStarted,
        then onAVStarted)."""
        self.item = item
        self.paused = False
        xbmc.Player.playing = {"file": item["file"], "position_ms": position_ms, "duration_ms": duration_ms}
        self.owed.append(("onPlayBackStarted", ()))
        self.owed.append(("onAVStarted", ()))

    def end(self):
        self.item = None
        xbmc.Player.playing = None
        self.owed.append(("onPlayBackEnded", ()))

    def stop(self):
        self.item = None
        xbmc.Player.playing = None
        self.owed.append(("onPlayBackStopped", ()))

    def pause(self):
        self.paused = True
        self.owed.append(("onPlayBackPaused", ()))

    def resume(self):
        self.paused = False
        self.owed.append(("onPlayBackResumed", ()))

    def at(self, position_ms):
        """Playing on to a position (no callback)."""
        xbmc.Player.playing["position_ms"] = position_ms

    def later(self, seconds):
        """Time passes while playing (no callback)."""
        self._advance(seconds)

    def _advance(self, seconds):
        self.clock.now += seconds
        playing = xbmc.Player.playing
        if playing is not None and not self.paused:
            playing["position_ms"] = min(playing["duration_ms"], playing["position_ms"] + int(seconds * 1000))

    # --- Kodi's side ---

    def rpc(self, method, params):
        if method == "Player.GetActivePlayers":
            return [{"playerid": 1, "type": "video"}] if self.item else []
        if method == "Player.GetItem":
            return {"item": self.item} if self.item and not self.lookup_fails else None
        return {}

    def _deliver(self):
        owed, self.owed = self.owed, []
        for name, args in owed:
            getattr(self.player, name)(*args)  # late: reads what plays NOW

    def wait(self, seconds):
        if self.monitor.aborted:
            return True
        if seconds < 0.5:  # the startup's short waits
            if self.before_flush:
                self.before_flush.pop(0)()
            self._advance(seconds)
            self._deliver()
            if self.flush_hooks:
                self.flush_hooks.pop(0)()
            return False
        if not self.steps:
            self.monitor.aborted = True
            return True
        self.steps.pop(0)()
        self._advance(seconds)
        self._deliver()
        return False


class ScriptedMonitor(object):
    def __init__(self, fake):
        self.fake = fake
        self.aborted = False

    def abortRequested(self):
        return self.aborted

    def waitForAbort(self, seconds=0):
        return self.fake.wait(seconds)


class InertThread(object):
    def start(self):
        pass

    def wake(self):
        pass

    def halt(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class World(object):
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World()
    w.path = tmp_path
    w.clock = Clock(NOW)
    w.fk = FakeKodi(w.clock)
    w.fk.monitor = ScriptedMonitor(w.fk)
    w.notices = []
    w.lines = []
    monkeypatch.setattr(service_mod, "time", w.clock)
    monkeypatch.setattr(xbmc, "Monitor", lambda: w.fk.monitor)
    monkeypatch.setattr(kodi, "install_log_sink", lambda: None)
    monkeypatch.setattr(kodi, "rpc", w.fk.rpc)
    monkeypatch.setattr(kodi, "setting_bool", lambda setting_id, default=True: default)
    monkeypatch.setattr(kodi, "is_paused", lambda: w.fk.paused)
    monkeypatch.setattr(kodi, "profile_dir", lambda: str(tmp_path))
    monkeypatch.setattr(kodi, "boot_id", lambda: "boot-1")
    monkeypatch.setattr(kodi, "user_agent", lambda: "test")
    monkeypatch.setattr(kodi, "notify", lambda message, error=False: w.notices.append(message))
    monkeypatch.setattr(kodi, "text", lambda string_id, **values: str(string_id))
    log.set_sink(lambda message, level: w.lines.append((level, message)))
    w.store = CredentialStore(str(tmp_path))
    w.store.save(Credentials("mp_dev_tok_1", "p1", 1, ["timing", "processed"]))
    yield w
    log.set_sink(None)
    xbmc.Player.playing = None


def make_service(world, inert_sender=True):
    svc = service_mod.Service()
    world.fk.player = svc._player
    if inert_sender:
        svc._sender_thread = InertThread()
    return svc


def queued(world):
    """(action, tmdb id, session, completed, link) of every queued event, in order."""
    db = sqlite3.connect(str(world.path / "queue.sqlite"))
    try:
        rows = db.execute("SELECT pairing_id, session_id, action, payload FROM events ORDER BY seq").fetchall()
    finally:
        db.close()
    out = []
    for pairing, session, action, body in rows:
        body = json.loads(body)
        out.append((action, body.get("movie", {}).get("ids", {}).get("tmdb"), session, body.get("completed", False), pairing))
    return out


def checkpoint(**overrides):
    value = {"session_id": "sess-old", "key": "movie:tmdb:101", "path": "/m/a.mkv", "media": A_MEDIA,
             "state": "playing", "position_ms": 91 * MIN, "duration_ms": 100 * MIN, "at": NOW - 30,
             "max_ratio": 0.91, "boot_id": "boot-1", "pairing_id": "p1"}
    value.update(overrides)
    return value


def write_checkpoint(world, value):
    box = Outbox(str(world.path / "queue.sqlite"))
    box.set_meta("checkpoint", value)
    box.close()


def read_checkpoint(world):
    box = Outbox(str(world.path / "queue.sqlite"))
    try:
        return box.get_meta("checkpoint")
    finally:
        box.close()


# --- ordering ---

def test_quick_transitions_reported_late_never_complete_the_title_playing_by_then(world):
    """A 0.8 s clip starts and ends and B starts before Kodi runs the first
    callback: that start reads B. The clip is unknowable and never becomes
    a playback; B is one."""
    fk = world.fk
    fk.steps = [lambda: (fk.start(A, 0, 800), fk.end(), fk.start(B, 0, 100 * MIN))]
    make_service(world).run()
    assert [(a, t, c) for a, t, _, c, _ in queued(world)] == [("start", 202, False)]


def test_a_late_start_never_records_a_title_resumed_past_the_threshold_twice(world):
    """A starts and ends before its callbacks run, B resumes at 95 %:
    one playback of B, completed once."""
    fk = world.fk
    fk.steps = [lambda: (fk.start(A, 0, 800), fk.end(), fk.start(B, 95 * MIN, 100 * MIN)), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, t, c) for a, t, _, c, _ in rows] == [("start", 202, False), ("stop", 202, True)]
    assert rows[0][2] == rows[1][2]


def test_a_late_pause_never_lends_the_next_titles_timing(world):
    """A pauses at 20 % and stops, and B starts at 95 %, before the
    callbacks run: the pause reads B's timing, which is not A's."""
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.at(20 * MIN),
                lambda: (fk.pause(), fk.stop(), fk.start(B, 95 * MIN, 100 * MIN)), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, t, c) for a, t, _, c, _ in rows] == [
        ("start", 101, False), ("pause", 101, False), ("stop", 101, False), ("start", 202, False), ("stop", 202, True)]
    db = sqlite3.connect(str(world.path / "queue.sqlite"))
    stop_a = json.loads(db.execute("SELECT payload FROM events WHERE action = 'stop' ORDER BY seq").fetchone()[0])
    db.close()
    assert stop_a["positionMs"] < 21 * MIN, "A's own last position"


def test_a_short_film_that_ends_naturally_is_watched(world):
    """No minimum length: an eight-second film sampled last at seven seconds."""
    fk = world.fk
    film = dict(A, file="/m/short.mkv")
    fk.steps = [lambda: fk.start(film, 0, 8000)] + [lambda: None] * 6 + [lambda: fk.end()]
    make_service(world).run()
    assert [(a, c) for a, _, _, c, _ in queued(world)] == [("start", False), ("stop", True)]


def test_transitions_reported_in_time_complete_the_title_that_ended(world):
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.at(95 * MIN), lambda: None,
                lambda: fk.end(), lambda: fk.start(B, 0, 100 * MIN)]
    make_service(world).run()
    rows = queued(world)
    assert [(a, t, c) for a, t, _, c, _ in rows] == [("start", 101, False), ("stop", 101, True), ("start", 202, False)]
    assert rows[0][2] == rows[1][2] != rows[2][2]


def test_a_duplicate_av_start_is_one_playback(world):
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.owed.append(("onAVStarted", ())), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [a for a, _, _, _, _ in rows] == ["start", "stop"] and rows[0][2] == rows[1][2]


def test_a_start_seen_after_the_play_ended_changes_nothing(world):
    fk = world.fk
    fk.steps = [lambda: (fk.start(A, 0, 100 * MIN), fk.stop())]
    make_service(world).run()
    assert queued(world) == []


def test_startup_during_playback_is_one_playback_with_the_start_kodi_still_owes(world):
    fk = world.fk
    fk.start(A, 10 * MIN, 100 * MIN)  # playing before the service; Kodi still owes its AV start
    fk.steps = [lambda: None, lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [a for a, _, _, _, _ in rows] == ["start", "stop"] and rows[0][2] == rows[1][2]


def test_a_restart_during_a_long_pause_continues_the_playback(world):
    """Paused at 95 % for 11 minutes, the service restarts, then the stop:
    one playback, recorded once."""
    fk = world.fk
    write_checkpoint(world, checkpoint(state="paused", position_ms=95 * MIN, at=NOW - 660, max_ratio=0.95))
    fk.start(A, 95 * MIN, 100 * MIN)
    fk.paused = True
    fk.owed = []
    fk.steps = [lambda: fk.stop()]
    make_service(world).run()
    assert queued(world) == [("stop", 101, "sess-old", True, "p1")]


def test_a_latch_set_by_a_duplicate_start_survives_a_restart(world):
    """The latch is saved whatever set it."""
    fk = world.fk
    seen = []
    svc = make_service(world)
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN),
                lambda: (fk.at(95 * MIN), fk.owed.append(("onAVStarted", ()))),
                lambda: seen.append(svc._outbox.get_meta("checkpoint"))]
    svc.run()
    assert seen[0]["max_ratio"] >= 0.9
    fk.monitor.aborted = False
    fk.owed = []
    fk.steps = [lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, c) for a, _, _, c, _ in rows] == [("start", False), ("stop", True)] and rows[0][2] == rows[1][2]


def test_a_restart_mid_play_continues_the_checkpointed_playback_and_its_latch(world):
    fk = world.fk
    write_checkpoint(world, checkpoint())
    fk.start(A, 91 * MIN + 30_000, 100 * MIN)
    fk.owed = []  # the play began long ago: no AV start is owed
    fk.steps = [lambda: fk.stop()]
    make_service(world).run()
    assert queued(world) == [("stop", 101, "sess-old", True, "p1")]


def test_a_playback_that_ended_while_kodi_was_closed_gets_its_stop(world):
    write_checkpoint(world, checkpoint(boot_id="boot-0", at=NOW - 3600, max_ratio=0.95))
    make_service(world).run()
    assert queued(world) == [("stop", 101, "sess-old", True, "p1")]
    db = sqlite3.connect(str(world.path / "queue.sqlite"))
    body = json.loads(db.execute("SELECT payload FROM events").fetchone()[0])
    db.close()
    assert body["occurredAt"] == "2026-09-21T13:13:20.000Z", "at its last known moment"
    assert read_checkpoint(world) is None


def test_a_checkpointed_playback_that_did_not_continue_is_closed_before_the_new_one(world):
    fk = world.fk
    write_checkpoint(world, checkpoint(boot_id="boot-0", at=NOW - 3600, max_ratio=0.95))
    fk.start(B, 5 * MIN, 100 * MIN)
    fk.owed = []
    make_service(world).run()
    rows = queued(world)
    assert [(a, t, s == "sess-old", c) for a, t, s, c, _ in rows] == [("stop", 101, True, True), ("start", 202, False, False)]


def test_a_checkpoint_past_the_servers_window_is_forgotten(world):
    write_checkpoint(world, checkpoint(boot_id="boot-0", at=NOW - 23 * 3600, max_ratio=0.95))
    make_service(world).run()
    assert queued(world) == [] and read_checkpoint(world) is None


def test_the_latch_is_saved_the_moment_it_is_set(world):
    fk = world.fk
    seen = []
    svc = make_service(world)
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.at(91 * MIN),
                lambda: seen.append(svc._outbox.get_meta("checkpoint"))]
    svc.run()
    assert seen[0]["max_ratio"] >= 0.9
    assert [a for a, _, _, _, _ in queued(world)] == ["start"], "no event was needed to save it"


def test_an_extra_carrying_the_movies_runtime_is_not_shared(world):
    fk = world.fk
    trailer = {"type": "movie", "id": -1, "file": "plugin://plugin.video.someaddon/play?t=1", "runtime": 8160,
               "uniqueid": {"tmdb": "101"}}
    fk.steps = [lambda: fk.start(trailer, 0, 150_000), lambda: fk.end()]
    make_service(world).run()
    assert queued(world) == []


def test_callbacks_queued_before_the_startup_reading_never_touch_the_playback_it_adopts(world):
    """The observer is registered while A plays; A ends and B opens at
    20 % before the service first waits. The queued end of A must not
    complete B, which the startup reading adopts."""
    fk = world.fk
    write_checkpoint(world, checkpoint(position_ms=50 * MIN, max_ratio=0.5))
    fk.start(A, 50 * MIN, 100 * MIN)
    fk.owed = []  # A began before the observer existed
    svc = make_service(world)
    fk.end()
    fk.start(B, 20 * MIN, 100 * MIN)  # queued, not yet delivered
    fk.steps = [lambda: fk.stop()]
    svc.run()
    assert [(a, t, c) for a, t, _, c, _ in queued(world)] == [
        ("stop", 101, False), ("start", 202, False), ("stop", 202, False)]


def test_a_change_of_playback_right_after_the_startup_reading_is_read_again(world):
    """The reading shows A; A ends and B opens before the verifying
    delivery, which carries the change: the reading is stale and is taken
    again (adopting it would let A's end complete A at 50 %)."""
    fk = world.fk
    fk.start(A, 50 * MIN, 100 * MIN)
    fk.owed = []
    fk.before_flush = [lambda: (fk.end(), fk.start(B, 20 * MIN, 100 * MIN))]
    fk.steps = [lambda: fk.stop()]
    make_service(world).run()
    assert [(a, t, c) for a, t, _, c, _ in queued(world)] == [("start", 202, False), ("stop", 202, False)]


def test_duplicate_av_starts_delivered_together_are_one_playback(world):
    """Two duplicate AV starts at 95 %: one playback, completed once."""
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.at(95 * MIN),
                lambda: fk.owed.extend([("onAVStarted", ()), ("onAVStarted", ())]), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, c) for a, _, _, c, _ in rows] == [("start", False), ("stop", True)] and rows[0][2] == rows[1][2]


def test_a_duplicate_av_start_delivered_with_the_natural_end_keeps_the_completion(world):
    """The eight-second film: a duplicate AV start and the natural end run
    in one delivery."""
    fk = world.fk
    film = dict(A, file="/m/short.mkv")
    fk.steps = ([lambda: fk.start(film, 0, 8000)] + [lambda: None] * 6
                + [lambda: (fk.owed.append(("onAVStarted", ())), fk.end())])
    make_service(world).run()
    assert [(a, c) for a, _, _, c, _ in queued(world)] == [("start", False), ("stop", True)]


def test_a_duplicate_av_start_whose_lookup_fails_keeps_the_playback(world):
    """The tracked file's own AV start again, while JSON-RPC answers nothing."""
    fk = world.fk

    def dup_with_a_failed_lookup():
        fk.lookup_fails = True
        fk.owed.append(("onAVStarted", ()))

    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), dup_with_a_failed_lookup,
                lambda: (setattr(fk, "lookup_fails", False), fk.later(61)), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [a for a, _, _, _, _ in rows] == ["start", "progress", "stop"], "still playing a minute later"
    assert len(set(r[2] for r in rows)) == 1


def test_repeat_one_is_a_new_playback_each_time(world):
    """Kodi reopens the same file (repeat one, or the next playlist item is
    the same file): its open ends the playback, so the same file is a new
    one, never a duplicate."""
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.at(95 * MIN), lambda: None,
                lambda: fk.start(A, 0, 100 * MIN), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, c) for a, _, _, c, _ in rows] == [("start", False), ("stop", True), ("start", False), ("stop", False)]
    assert rows[0][2] == rows[1][2] != rows[2][2] == rows[3][2]


def test_a_playlists_next_item_ends_the_previous_without_a_stop_of_its_own(world):
    """Kodi opens the next item with no stop for the previous one."""
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: fk.at(95 * MIN), lambda: None,
                lambda: fk.start(B, 0, 100 * MIN), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, t, c) for a, t, _, c, _ in rows] == [
        ("start", 101, False), ("stop", 101, True), ("start", 202, False), ("stop", 202, False)]


# --- the link ---

def test_a_relink_mid_play_sends_nothing_of_that_playback_to_the_new_account(world):
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN),
                lambda: world.store.save(Credentials("mp_dev_tok_2", "p2", 2, ["timing", "processed"])),
                lambda: fk.later(61), lambda: fk.stop()]
    make_service(world).run()
    rows = queued(world)
    assert [(a, p) for a, _, _, _, p in rows] == [("start", "p1")], "the minute's progress and the stop were dropped"
    assert read_checkpoint(world) is None


def test_an_unlink_mid_play_ends_the_playback_quietly(world):
    fk = world.fk
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN), lambda: world.store.clear(), lambda: fk.later(61),
                lambda: fk.start(B, 0, 100 * MIN)]
    make_service(world).run()
    assert [(a, t) for a, t, _, _, _ in queued(world)] == [("start", 101)]


def test_an_unlink_by_the_settings_script_never_leaves_the_playbacks_events_behind(world):
    """The service checks the link and queues under the link lock: the
    script's unlink (its own connection) lands before or after, never between."""
    fk = world.fk
    svc = make_service(world)
    checked = threading.Event()
    real = svc._pairing_id

    def slow_pairing_id():
        value = real()
        if sys._getframe(1).f_code.co_name == "_enqueue":
            checked.set()
            real_time.sleep(0.3)  # the script unlinks right now
        return value

    def script_unlinks():
        checked.wait(5)
        box = Outbox(str(world.path / "queue.sqlite"))
        try:
            with box.exclusive():
                CredentialStore(str(world.path)).clear()
                box.purge_all()
        finally:
            box.close()

    svc._pairing_id = slow_pairing_id
    t = threading.Thread(target=script_unlinks)
    t.start()
    fk.steps = [lambda: fk.start(A, 0, 100 * MIN)]
    svc.run()
    t.join(5)
    assert checked.is_set() and queued(world) == []


# --- shutdown (a real sender thread, on the interpreter the suite runs on) ---

def test_shutdown_when_idle_stops_the_sender_and_closes_the_queue(world):
    svc = make_service(world, inert_sender=False)
    svc.run()
    assert not svc._sender_thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        svc._outbox.count()


def test_shutdown_during_a_blocked_request_leaves_the_queue_open_for_its_answer(world, monkeypatch):
    class Blocking(object):
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def post(self, path, body, headers=None):
            self.entered.set()
            self.release.wait(10)
            return api.Response(202, {"X-MP-Event-Processed": "true", "X-MP-Capabilities": "timing,processed"}, b"{}")

    blocking = Blocking()
    monkeypatch.setattr(api, "Transport", lambda *a, **k: blocking)
    monkeypatch.setattr(service_mod, "SENDER_JOIN_SECONDS", 0.2)
    svc = make_service(world, inert_sender=False)
    svc._outbox.add("e1", "p1", "s1", "stop", real_time.time(), {"action": "stop"}, terminal=True)
    world.fk.steps = [lambda: blocking.entered.wait(5)]
    svc.run()
    try:
        assert svc._sender_thread.is_alive(), "still inside its request"
        blocking.release.set()
        svc._sender_thread.join(5)
        assert not svc._sender_thread.is_alive()
        assert svc._outbox.count() == 0, "the answer landed in a queue that was still open"
        assert not [m for _, m in world.lines if "failed" in m]
    finally:
        blocking.release.set()
        svc._outbox.close()
