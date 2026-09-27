import itertools

from resources.lib import playback
from resources.lib.identity import Identity

MOVIE = Identity({"movie": {"ids": {"tmdb": 603}}}, "movie:tmdb:603", "The Matrix", "/m/matrix.mkv")
EPISODE = Identity({"show": {"ids": {"tmdb": 1399}}, "episode": {"season": 1, "number": 2}}, "episode:tmdb:1399:1:2", "GoT S01E02", "/tv/got.s01e02.mkv")
MIN = 60_000
DUR = 100 * MIN


def tracker():
    ids = ("play-%d" % i for i in itertools.count(1))
    return playback.Tracker(lambda: next(ids))


def actions(events):
    return [e.action for e in events]


def test_start_carries_the_timing_and_a_fresh_session():
    t = tracker()
    events = t.start(MOVIE, 5 * MIN, DUR, 1000.0)
    assert actions(events) == ["start"]
    e = events[0]
    assert (e.session_id, e.position_ms, e.duration_ms, e.media) == ("play-1", 5 * MIN, DUR, MOVIE.media)
    assert t.state == playback.PLAYING


def test_progress_once_a_minute_while_playing_only():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    assert t.tick(59.0) == []
    t.sample(MIN, DUR, 60.0)
    events = t.tick(60.0)
    assert actions(events) == ["progress"] and events[0].position_ms == MIN
    assert t.tick(90.0) == []
    t.pause(MIN + 30_000, DUR, 90.0)
    assert t.tick(500.0) == [], "a paused play sends no progress"


def test_pause_and_resume_are_boundaries_and_idempotent():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    assert actions(t.pause(MIN, DUR, 60.0)) == ["pause"]
    assert t.pause(MIN, DUR, 61.0) == []
    assert actions(t.resume(MIN, DUR, 120.0)) == ["resume"]
    assert t.resume(MIN, DUR, 121.0) == []


def test_a_seek_while_paused_is_a_pause_at_the_new_spot():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    assert actions(t.seek(30 * MIN, DUR, 10.0)) == ["progress"]
    t.pause(30 * MIN, DUR, 20.0)
    events = t.seek(50 * MIN, DUR, 30.0)
    assert actions(events) == ["pause"] and events[0].position_ms == 50 * MIN


def test_one_stop_per_playback():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    events = t.stop(10.0, natural_end=False)
    assert actions(events) == ["stop"] and events[0].completed is False
    assert t.stop(11.0, natural_end=False) == []
    assert not t.active


def test_a_natural_end_completes():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    t.sample(50 * MIN, DUR, 10.0)
    assert t.stop(20.0, natural_end=True)[0].completed is True


def test_a_short_film_that_ends_naturally_completes():
    """No minimum length: an eight-second film last sampled at seven seconds."""
    t = tracker()
    t.start(MOVIE, 0, 8000, 0.0)
    t.sample(7000, 8000, 7.0)
    assert t.stop(8.0, natural_end=True)[0].completed is True


def test_the_latch():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    t.sample(50 * MIN, DUR, 1.0)
    assert not t.latched
    t.sample(91 * MIN, DUR, 2.0)
    assert t.latched
    t.seek(10 * MIN, DUR, 3.0)
    assert t.latched, "seeking back never unsets it"


def test_timing_from_another_file_moves_nothing():
    """A late callback or the next playlist item reads another file's timing."""
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    t.sample(20 * MIN, DUR, 1.0)
    t.sample(99 * MIN, DUR, 2.0, path="/m/next.mkv")
    t.pause(99 * MIN, DUR, 3.0, path="/m/next.mkv")
    assert t.state == playback.PAUSED and not t.latched
    stop = t.stop(4.0, natural_end=False)[0]
    assert (stop.position_ms, stop.completed) == (20 * MIN, False)


def test_the_ninety_percent_latch_survives_seeking_back():
    """Offline, 95 %, back to 20 %, stop: still completed, with the REAL timing."""
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    t.sample(95 * MIN, DUR, 10.0)
    t.seek(20 * MIN, DUR, 20.0)
    stop = t.stop(30.0, natural_end=False)[0]
    assert stop.completed is True
    assert stop.position_ms == 20 * MIN


def test_short_of_the_threshold_does_not_complete():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    t.sample(89 * MIN, DUR, 10.0)
    assert t.stop(20.0, natural_end=False)[0].completed is False


def test_a_new_play_ends_the_previous_one_and_gets_a_new_session():
    t = tracker()
    t.start(EPISODE, 0, DUR, 0.0)
    t.sample(98 * MIN, DUR, 10.0)
    events = t.start(MOVIE, 0, DUR, 20.0)
    assert actions(events) == ["stop", "start"]
    assert events[0].session_id == "play-1" and events[0].completed is True
    assert events[1].session_id == "play-2"


def test_the_same_file_played_again_is_a_new_session():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0)
    t.stop(10.0, natural_end=False)
    assert t.start(MOVIE, 0, DUR, 20.0)[0].session_id == "play-2"


def test_timing_is_clamped_and_needs_a_length():
    assert playback.clamp_timing(DUR + 500, DUR) == (DUR, DUR)
    assert playback.clamp_timing(-5, DUR) == (0, DUR)
    assert playback.clamp_timing(1000, 0) == (None, None)
    assert playback.clamp_timing(None, DUR) == (None, None)
    t = tracker()
    e = t.start(MOVIE, 1000, 0, 0.0)[0]
    assert e.position_ms is None and e.duration_ms is None


def _checkpointed(state="playing"):
    t = tracker()
    t.start(MOVIE, 10 * MIN, DUR, 1000.0, "p1")
    if state == "paused":
        t.pause(10 * MIN, DUR, 1000.0)
    return t.checkpoint("boot-1")


AGE = 22 * 3600


def test_a_service_restart_continues_the_same_playback():
    cp = _checkpointed()
    t = tracker()
    assert t.resume_from(cp, MOVIE, 12 * MIN, DUR, True, "boot-1", 1000.0 + 150, "p1", AGE)
    assert t.session["id"] == cp["session_id"] and t.state == playback.PLAYING


def test_continuity_needs_the_same_kodi_process_and_link():
    assert not tracker().resume_from(_checkpointed(), MOVIE, 12 * MIN, DUR, True, "boot-2", 1150.0, "p1", AGE)
    assert not tracker().resume_from(_checkpointed(), MOVIE, 12 * MIN, DUR, True, "boot-1", 1150.0, "p2", AGE)
    assert not tracker().resume_from(_checkpointed(), MOVIE, 12 * MIN, DUR, True, "boot-1", 1150.0, None, AGE)


def test_continuity_needs_the_same_title_file_and_a_checkpoint_the_server_still_takes():
    cp = _checkpointed()
    assert not tracker().resume_from(cp, EPISODE, 12 * MIN, DUR, True, "boot-1", 1150.0, "p1", AGE)
    other_file = Identity(MOVIE.media, MOVIE.key, MOVIE.label, "/m/matrix.version2.mkv")
    assert not tracker().resume_from(cp, other_file, 12 * MIN, DUR, True, "boot-1", 1150.0, "p1", AGE)
    assert not tracker().resume_from(cp, MOVIE, 12 * MIN, DUR, True, "boot-1", 1000.0 + AGE + 1, "p1", AGE)
    assert not tracker().resume_from(None, MOVIE, 12 * MIN, DUR, True, "boot-1", 1150.0, "p1", AGE)


def test_continuity_holds_through_a_seek_and_a_long_pause():
    """In one Kodi process the same file is the same playback, whatever its
    position and however long it sat paused (a new session would record a
    latched playback twice)."""
    cp = _checkpointed()
    assert tracker().resume_from(cp, MOVIE, 2 * MIN, DUR, True, "boot-1", 1100.0, "p1", AGE), "seeked back"
    assert tracker().resume_from(cp, MOVIE, 60 * MIN, DUR, True, "boot-1", 1100.0, "p1", AGE), "seeked ahead"
    paused = _checkpointed("paused")
    t = tracker()
    assert t.resume_from(paused, MOVIE, 10 * MIN, DUR, False, "boot-1", 1000.0 + 11 * 60, "p1", AGE)
    assert t.state == playback.PAUSED


def test_the_latch_rides_the_checkpoint():
    t = tracker()
    t.start(MOVIE, 0, DUR, 1000.0, "p1")
    t.sample(95 * MIN, DUR, 1010.0)
    t.seek(20 * MIN, DUR, 1020.0)
    cp = t.checkpoint("boot-1")
    again = tracker()
    assert again.resume_from(cp, MOVIE, 21 * MIN, DUR, True, "boot-1", 1080.0, "p1", AGE)
    assert again.stop(1090.0, natural_end=False)[0].completed is True


def test_a_duplicate_av_start_is_the_same_playback():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0, "p1")
    assert t.start(MOVIE, 5000, DUR, 1.0, "p1") == []
    assert t.session["id"] == "play-1"


def test_samples_from_another_file_are_ignored():
    t = tracker()
    t.start(MOVIE, 0, DUR, 0.0, "p1")
    t.sample(99 * MIN, DUR, 10.0, path="/m/next-in-playlist.mkv")
    assert t.stop(20.0, natural_end=False)[0].completed is False
    t.start(MOVIE, 0, DUR, 30.0, "p1")
    t.sample(99 * MIN, DUR, 40.0, path=MOVIE.path)
    assert t.stop(50.0, natural_end=False)[0].completed is True


def test_every_event_carries_the_link_the_playback_began_with():
    t = tracker()
    events = t.start(MOVIE, 0, DUR, 0.0, "p1") + t.pause(MIN, DUR, 10.0) + t.stop(20.0, natural_end=False)
    assert set(e.pairing_id for e in events) == {"p1"}


def test_a_checkpointed_playback_that_never_stopped_is_closed_from_its_checkpoint():
    t = tracker()
    t.start(MOVIE, 0, DUR, 1000.0, "p1")
    t.sample(95 * MIN, DUR, 1010.0)
    cp = t.checkpoint("boot-1")
    events = playback.stop_from_checkpoint(cp, 4000.0, max_age=3600)
    assert actions(events) == ["stop"]
    stop = events[0]
    assert (stop.session_id, stop.pairing_id, stop.occurred_at, stop.completed) == ("play-1", "p1", 1010.0, True)
    assert (stop.position_ms, stop.duration_ms, stop.media) == (95 * MIN, DUR, MOVIE.media)


def test_an_unlatched_checkpoint_closes_without_a_watch_and_old_ones_not_at_all():
    t = tracker()
    t.start(MOVIE, 0, DUR, 1000.0, "p1")
    cp = t.checkpoint("boot-1")
    assert playback.stop_from_checkpoint(cp, 2000.0, max_age=3600)[0].completed is False
    assert playback.stop_from_checkpoint(cp, 1000.0 + 3601, max_age=3600) == []
    assert playback.stop_from_checkpoint(None, 2000.0, max_age=3600) == []
    assert playback.stop_from_checkpoint(dict(cp, pairing_id=None), 2000.0, max_age=3600) == []
