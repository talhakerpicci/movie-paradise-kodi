import json
import os
import re
import threading
import time

import pytest

from resources.lib import api, outbox as outbox_mod, sender as sender_mod
from resources.lib.credentials import CredentialStore, Credentials
from resources.lib.outbox import Outbox, QueueFull
from resources.lib.playback import Event

NOW = 1_790_000_000.0
LEASE = sender_mod.CLAIM_LEASE_SECONDS


@pytest.fixture
def box(tmp_path):
    b = Outbox(str(tmp_path / "queue.sqlite"), clock=lambda: NOW)
    yield b
    b.close()


def add(box, event_id, session="s1", action="progress", at=NOW, terminal=False, pairing="p1", payload=None):
    box.add(event_id, pairing, session, action, at, payload or {"action": action, "e": event_id}, terminal=terminal)


def ids(box):
    return set(r[0] for r in box._db.execute("SELECT event_id FROM events"))


# --- outbox ---

def test_events_of_a_playback_go_in_order(box):
    add(box, "a1", action="start")
    add(box, "a2", action="pause")
    first = box.claim_next("p1", LEASE, NOW)
    assert first.event_id == "a1"
    assert box.claim_next("p1", LEASE, NOW) is None, "the second waits for the first"
    box.delete("a1")
    assert box.claim_next("p1", LEASE, NOW).event_id == "a2"


def test_an_unsent_progress_sample_is_superseded(box):
    add(box, "a1", action="progress")
    add(box, "a2", action="progress")
    assert ids(box) == {"a2"}


def test_a_claimed_event_survives_a_newer_sample_and_is_retried_identically(box):
    """The in-flight row is marked attempted BEFORE the request: a sample
    queued while the request runs can never delete it."""
    add(box, "a1", action="progress", payload={"p": 1})
    claimed = box.claim_next("p1", LEASE, NOW)  # the request is now on the wire
    add(box, "a2", action="progress", payload={"p": 2})  # a sample lands mid-request
    assert ids(box) == {"a1", "a2"}
    assert box.claim_next("p1", LEASE, NOW + 1) is None, "the lease keeps it from a second sender"
    again = box.claim_next("p1", LEASE, NOW + LEASE + 1)
    assert again.event_id == "a1" and again.body() == claimed.body() == {"p": 1}


def test_a_stop_goes_before_other_playbacks_progress(box):
    add(box, "p-old", session="s1", action="progress")
    add(box, "stop-2", session="s2", action="stop", terminal=True)
    assert box.claim_next("p1", LEASE, NOW).event_id == "stop-2"


def test_the_horizon_is_the_events_own_clock(box):
    add(box, "old-stop", session="s1", action="stop", at=NOW - outbox_mod.HORIZON_SECONDS - 1, terminal=True)
    add(box, "fresh", session="s2", action="start", at=NOW - 60)
    assert box.expire(NOW) == 1
    assert ids(box) == {"fresh"}


def test_another_links_events_are_never_sent(box):
    add(box, "mine", pairing="p1")
    add(box, "theirs", session="s9", pairing="p0")
    box.purge_other_links("p1")
    assert ids(box) == {"mine"} and box.claim_next("p0", LEASE, NOW) is None


def test_a_hold_covers_events_queued_after_it(box):
    add(box, "old", session="s1")
    box.hold("p1", NOW + 3600)
    add(box, "new", session="s2", action="start")
    assert box.claim_next("p1", LEASE, NOW + 10) is None
    assert box.next_wake("p1") == NOW + 3600
    box.hold("p1", NOW + 60)  # never shortened
    assert box.hold_until("p1") == NOW + 3600
    assert box.claim_next("p1", LEASE, NOW + 3601) is not None


def test_bounds_apply_to_every_action(box, monkeypatch):
    monkeypatch.setattr(outbox_mod, "MAX_ROWS", 10)
    monkeypatch.setattr(outbox_mod, "TERMINAL_RESERVE_ROWS", 3)
    for i in range(7):
        add(box, "pause-%d" % i, session="s%d" % i, action="pause")
    with pytest.raises(QueueFull):
        add(box, "pause-7", session="s7", action="pause")  # ordinary events stop at the reserve
    for i in range(3):
        add(box, "stop-%d" % i, session="t%d" % i, action="stop", terminal=True)  # the reserve takes stops
    with pytest.raises(QueueFull):
        add(box, "stop-3", session="t3", action="stop", terminal=True)  # even stops are bounded
    assert box.count() == 10


def test_room_is_made_from_unsent_progress_first(box, monkeypatch):
    monkeypatch.setattr(outbox_mod, "MAX_ROWS", 5)
    monkeypatch.setattr(outbox_mod, "TERMINAL_RESERVE_ROWS", 1)
    for i in range(4):
        add(box, "prog-%d" % i, session="s%d" % i, action="progress")
    add(box, "stop-a", session="z", action="stop", terminal=True)
    assert box.count() == 5
    add(box, "pause-b", session="y", action="pause")  # evicts unsent progress
    assert "pause-b" in ids(box) and box.count() <= 5


def test_the_byte_cap_holds_too(box, monkeypatch):
    monkeypatch.setattr(outbox_mod, "MAX_BYTES", 400)
    monkeypatch.setattr(outbox_mod, "TERMINAL_RESERVE_BYTES", 100)
    big = {"x": "y" * 120}
    add(box, "a", session="s1", action="start", payload=big)
    add(box, "b", session="s2", action="start", payload=big)
    with pytest.raises(QueueFull):
        add(box, "c", session="s3", action="start", payload=big)
    add(box, "d", session="s4", action="stop", terminal=True, payload=big)


def test_a_terminal_and_its_checkpoint_commit_together(box):
    box.set_meta("checkpoint", {"session_id": "s1"})
    box.add("stop-1", "p1", "s1", "stop", NOW, {"action": "stop"}, terminal=True, checkpoint=("checkpoint", None))
    assert box.get_meta("checkpoint") is None and box.count() == 1
    box.set_meta("checkpoint", {"session_id": "s2"})
    with pytest.raises(Exception):
        box.add("stop-1", "p1", "s2", "stop", NOW, {"action": "stop"}, terminal=True, checkpoint=("checkpoint", None))
    assert box.get_meta("checkpoint") == {"session_id": "s2"}, "a failed add leaves the checkpoint alone"


def test_the_link_lock_nests_and_a_refused_add_undoes_only_itself(box, monkeypatch):
    monkeypatch.setattr(outbox_mod, "MAX_ROWS", 2)
    monkeypatch.setattr(outbox_mod, "TERMINAL_RESERVE_ROWS", 0)
    with box.exclusive():
        add(box, "a", session="s1", action="start")
        add(box, "b", session="s2", action="start")
        with pytest.raises(QueueFull):
            add(box, "c", session="s3", action="start")
        box.set_meta("checkpoint", {"x": 1})
    assert ids(box) == {"a", "b"} and box.get_meta("checkpoint") == {"x": 1}


def test_the_link_lock_spans_connections(tmp_path):
    """The settings script and the service each have their own connection:
    one waits for the other's link change."""
    path = str(tmp_path / "queue.sqlite")
    service, script = Outbox(path), Outbox(path)
    order = []
    inside = threading.Event()

    def script_links():
        inside.wait(5)
        with script.exclusive():
            order.append("script")

    t = threading.Thread(target=script_links)
    t.start()
    with service.exclusive():
        inside.set()
        time.sleep(0.3)
        order.append("service")
    t.join(5)
    service.close()
    script.close()
    assert order == ["service", "script"]


def test_the_queue_is_safe_across_threads(box):
    errors = []

    def writer(n):
        try:
            for i in range(50):
                add(box, "w%d-%d" % (n, i), session="s%d" % n, action="pause")
        except Exception as err:  # pragma: no cover - the failure is the assertion
            errors.append(err)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        box.claim_next("p1", LEASE, NOW)
    for t in threads:
        t.join()
    assert not errors and box.count() == 200


# --- payloads ---

def test_the_payload_carries_timing_media_and_completion_on_stop_only():
    media = {"show": {"ids": {"tmdb": 1399}}, "episode": {"season": 1, "number": 2}}
    stop = Event("stop", "sess-1", media, NOW + 0.123, 3_000_000, 3_600_000, completed=True, pairing_id="p1")
    body = sender_mod.payload(stop)
    assert body == {"action": "stop", "occurredAt": "2026-09-21T14:13:20.123Z", "sessionId": "sess-1",
                    "show": {"ids": {"tmdb": 1399}}, "episode": {"season": 1, "number": 2},
                    "positionMs": 3_000_000, "durationMs": 3_600_000, "completed": True}
    progress = sender_mod.payload(Event("progress", "sess-1", media, NOW, None, None, completed=True))
    assert "completed" not in progress and "positionMs" not in progress and "durationMs" not in progress
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", progress["occurredAt"])


# --- sender ---

class FakeTransport(object):
    def __init__(self, answers, clock=None, takes=0.0):
        self.answers = list(answers)
        self.sent = []
        self.clock = clock
        self.takes = takes

    def post(self, path, body, headers=None):
        self.sent.append((path, body, dict(headers or {})))
        if self.clock is not None:
            self.clock["now"] += self.takes  # a slow answer
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ack(processed):
    return api.Response(202, {"X-MP-Event-Processed": "true" if processed else "false", "X-MP-Capabilities": "timing,processed"}, b"{}")


def err(status, key="", headers=None):
    return api.Response(status, headers or {}, json.dumps({"error": key}).encode())


@pytest.fixture
def world(tmp_path):
    store = CredentialStore(str(tmp_path / "profile"))
    store.save(Credentials("mp_dev_tok_1", "p1", 1, ["timing", "processed"]))
    state = {"now": NOW}
    b = Outbox(str(tmp_path / "queue.sqlite"), clock=lambda: state["now"])
    notices = []

    def make(answers, takes=0.0):
        transport = FakeTransport(answers, state, takes)
        s = sender_mod.Sender(b, store, lambda creds: transport, notices.append, clock=lambda: state["now"], rand=lambda: 0.5)
        return s, transport

    yield b, store, notices, make, state
    b.close()


def test_processed_removes_and_admitted_is_asked_again_identically(world):
    b, store, notices, make, state = world
    add(b, "e1", action="stop", terminal=True, payload={"action": "stop", "x": 1})
    s, t = make([ack(False), ack(True)])
    assert s.step() == 0
    assert b.count() == 1
    state["now"] = NOW + 5
    assert s.step() == 0
    assert b.count() == 0
    (_, first, h1), (_, second, h2) = t.sent
    assert first == second and h1["X-MP-Event-Id"] == h2["X-MP-Event-Id"] == "e1"
    assert h1["Authorization"] == "Bearer mp_dev_tok_1"


def test_a_retry_after_holds_the_whole_link_from_the_answer(world):
    b, store, notices, make, state = world
    add(b, "e1", session="s1")
    s, t = make([err(429, "rateLimit", {"Retry-After": "600"})], takes=8.0)
    s.step()
    received = NOW + 8.0
    assert b.hold_until("p1") == received + 600, "measured from the answer, not the request"
    add(b, "e2", session="s2", action="start")
    state["now"] = received + 599
    assert s.step() > 0 and len(t.sent) == 1, "a new playback waits too"


def test_refusals_drop_and_the_clock_is_reported(world):
    b, store, notices, make, state = world
    add(b, "e1", session="s1")
    add(b, "e2", session="s2")
    s, t = make([err(422, "unsupportedShowIds"), err(422, "occurredAtOutOfRange")])
    s.step()
    s.step()
    assert b.count() == 0 and notices == [sender_mod.NOTICE_CLOCK]


def test_a_revoked_link_unlinks_and_empties_only_its_events(world):
    b, store, notices, make, state = world
    add(b, "e1")
    s, t = make([err(401, "invalidAuthenticationToken")])
    assert s.step() is None
    assert store.load() is None and b.count() == 0 and notices == [sender_mod.NOTICE_UNLINKED]


def test_a_401_for_an_old_link_never_deletes_the_new_one(world):
    """The user relinks while a request with the old bearer is in flight."""
    b, store, notices, make, state = world
    add(b, "e1")

    class Relinking(FakeTransport):
        def post(self, path, body, headers=None):
            store.save(Credentials("mp_dev_tok_NEW", "p2", 2, ["timing", "processed"]))
            add(b, "e-new", session="n1", pairing="p2")
            return err(401, "invalidAuthenticationToken")

    s = sender_mod.Sender(b, store, lambda creds: Relinking([]), notices.append, clock=lambda: state["now"], rand=lambda: 0.5)
    s.step()
    assert store.load().pairing_id == "p2" and "e-new" in ids(b) and notices == []


def test_premium_and_incompatible_hold_the_link(world):
    b, store, notices, make, state = world
    add(b, "e1", session="s1")
    s, t = make([err(403, "notPermitted"), api.Response(202, {}, b"{}")])
    s.step()
    add(b, "e2", session="s2", action="start")
    assert b.count() == 2 and b.claim_next("p1", LEASE, NOW + 60) is None and notices == [sender_mod.NOTICE_PREMIUM]
    state["now"] = NOW + sender_mod.PAUSE_SECONDS + 1
    s.step()
    assert b.count() == 2 and notices[-1] == sender_mod.NOTICE_INCOMPATIBLE


def test_notices_are_not_repeated_within_a_day(world):
    b, store, notices, make, state = world
    add(b, "e1", session="s1")
    add(b, "e2", session="s2")
    s, t = make([err(403, "x"), err(403, "x")])
    s.step()
    state["now"] = NOW + sender_mod.PAUSE_SECONDS + 1
    s.step()
    assert notices == [sender_mod.NOTICE_PREMIUM]


def test_network_trouble_backs_off_from_the_failure(world):
    b, store, notices, make, state = world
    add(b, "e1")
    s, t = make([api.TransportError("timeout")], takes=10.0)
    s.step()
    assert b.claim_next("p1", LEASE, NOW + 11) is None
    assert b.claim_next("p1", LEASE, NOW + 10 + 60) is not None


def test_nothing_is_sent_without_a_link(world):
    b, store, notices, make, state = world
    add(b, "e1")
    store.clear()
    s, t = make([])
    assert s.step() is None and t.sent == []


def test_a_401_never_removes_a_link_the_settings_script_saves_meanwhile(tmp_path, monkeypatch):
    """The sender reads the old link, the script saves a new one, the sender
    removes the file: without the link lock that removes the NEW link."""
    path = str(tmp_path / "queue.sqlite")
    service_box, script_box = Outbox(path), Outbox(path)
    profile = str(tmp_path / "profile")
    store = CredentialStore(profile)
    store.save(Credentials("mp_dev_tok_OLD", "p1", 1, ["timing", "processed"]))
    add(service_box, "e1")
    removing = threading.Event()
    real_remove = os.remove

    def slow_remove(target):
        removing.set()
        time.sleep(0.3)  # the script saves the new link right now
        real_remove(target)

    def script_relinks():
        removing.wait(5)
        with script_box.exclusive():
            CredentialStore(profile).save(Credentials("mp_dev_tok_NEW", "p2", 2, ["timing", "processed"]))

    monkeypatch.setattr(os, "remove", slow_remove)
    t = threading.Thread(target=script_relinks)
    t.start()
    transport = FakeTransport([err(401, "invalidAuthenticationToken")])
    s = sender_mod.Sender(service_box, store, lambda creds: transport, lambda kind: None, clock=lambda: NOW, rand=lambda: 0.5)
    try:
        assert s.step() is None
        t.join(5)
    finally:
        monkeypatch.undo()
        service_box.close()
        script_box.close()
    assert removing.is_set(), "the old link was removed"
    assert store.load().pairing_id == "p2", "the new link survived"


def test_a_relink_never_lands_between_the_senders_link_read_and_its_purge(tmp_path):
    """The sender reads the old link, the settings script relinks, the
    service queues the new link's stop: a purge by the stale link would
    delete that stop. Under the link lock the relink waits for the step."""
    path = str(tmp_path / "queue.sqlite")
    sender_box, script_box, service_box = Outbox(path), Outbox(path), Outbox(path)
    profile = str(tmp_path / "profile")
    store = CredentialStore(profile)
    store.save(Credentials("mp_dev_tok_OLD", "p1", 1, ["timing", "processed"]))
    read = threading.Event()
    real_load = store.load

    def slow_load():
        creds = real_load()
        read.set()
        time.sleep(0.3)  # the relink and the new stop land right now
        return creds

    def relink_and_queue():
        read.wait(5)
        with script_box.exclusive():
            CredentialStore(profile).save(Credentials("mp_dev_tok_NEW", "p2", 2, ["timing", "processed"]))
            script_box.purge_other_links("p2")
        with service_box.exclusive():
            service_box.add("stop-new", "p2", "n1", "stop", NOW, {"action": "stop"}, terminal=True)

    store.load = slow_load
    t = threading.Thread(target=relink_and_queue)
    t.start()
    s = sender_mod.Sender(sender_box, store, lambda creds: FakeTransport([]), lambda kind: None,
                          clock=lambda: NOW, rand=lambda: 0.5)
    try:
        s.step()
        t.join(5)
        assert read.is_set()
        assert "stop-new" in ids(sender_box), "the new link's stop survived"
    finally:
        sender_box.close()
        script_box.close()
        service_box.close()
