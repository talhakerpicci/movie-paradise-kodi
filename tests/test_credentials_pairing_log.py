import json
import os
import stat

import pytest

from resources.lib import api, log, pairing
from resources.lib.credentials import CredentialStore, Credentials


# --- credentials ---

def test_the_link_round_trips_privately(tmp_path):
    store = CredentialStore(str(tmp_path))
    store.save(Credentials("tok-1234567890", "pair-1", 5, ["timing", "processed"]))
    loaded = store.load()
    assert (loaded.token, loaded.pairing_id, loaded.device_id) == ("tok-1234567890", "pair-1", 5)
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert not os.path.exists(store.path + ".tmp")


def test_a_corrupt_or_missing_link_is_no_link(tmp_path):
    store = CredentialStore(str(tmp_path))
    assert store.load() is None
    with open(store.path, "w") as handle:
        handle.write("{not json")
    assert store.load() is None
    with open(store.path, "w") as handle:
        json.dump({"token": ""}, handle)
    assert store.load() is None


def test_unlinking_removes_the_file(tmp_path):
    store = CredentialStore(str(tmp_path))
    store.save(Credentials("tok-1234567890", "pair-1"))
    store.clear()
    assert store.load() is None and not os.path.exists(store.path)


def test_a_401_for_an_old_link_never_removes_the_new_one(tmp_path):
    store = CredentialStore(str(tmp_path))
    store.save(Credentials("tok-new-1234567890", "pair-new"))
    assert store.clear_if("pair-old") is False
    assert store.load().pairing_id == "pair-new"
    assert store.clear_if("pair-new") is True
    assert store.load() is None
    assert store.clear_if("pair-new") is False, "nothing left to remove"


# --- log redaction ---

def test_credentials_never_reach_the_log():
    lines = []
    log.set_sink(lambda message, level: lines.append(message))
    try:
        log.info("Authorization: Bearer abc.def-ghi")
        log.info('{"token":"mp_dev_secret","deviceCode":"dc-123456789"}')
        log.remember_secret("raw-secret-value-1")
        log.info("oops raw-secret-value-1 leaked")
        log.info("deviceCode=dc-987654321&x=1")
    finally:
        log.set_sink(None)
    text = "\n".join(lines)
    for secret in ("abc.def-ghi", "mp_dev_secret", "dc-123456789", "raw-secret-value-1", "dc-987654321"):
        assert secret not in text


# --- pairing ---

class FakeTransport(object):
    def __init__(self, answers, clock=None):
        self.answers = list(answers)
        self.calls = []
        self.clock = clock
        self.polled_at = []

    def post(self, path, body, headers=None):
        self.calls.append((path, body))
        if self.clock is not None:
            self.polled_at.append(self.clock[0])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def r(status, body):
    return api.Response(status, {}, json.dumps(body).encode())


ISSUED = {"deviceCode": "dc-abcdefgh12", "userCode": "BCDF-GHJK", "verificationUri": "https://movieparadise.app/link-device",
          "expiresIn": 600, "interval": 5, "capabilities": ["timing", "processed"]}


def test_an_older_backend_is_refused_before_any_device_exists():
    t = FakeTransport([r(201, dict(ISSUED, capabilities=None))])
    with pytest.raises(pairing.PairingError) as err:
        pairing.start(t, "movieparadise-kodi", "Living room")
    assert err.value.kind == pairing.INCOMPATIBLE
    assert [c[0] for c in t.calls] == [pairing.DEVICE_CODE_PATH], "never reached device-token"


def test_the_pairing_session():
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "movieparadise-kodi", "x" * 200)
    assert (session.user_code, session.interval, session.expires_in) == ("BCDF-GHJK", 5, 600)


@pytest.mark.parametrize("answer,kind", [
    (r(400, {"error": "pairingCodeExpired"}), pairing.EXPIRED),
    (r(404, {"error": "notFound"}), pairing.EXPIRED),
    (r(400, {"error": "pairingCodeConsumed"}), pairing.CONSUMED),
    (r(403, {"error": "deviceQuotaExceeded"}), pairing.QUOTA),
    (r(400, {"error": "somethingElse"}), pairing.SERVER),
])
def test_poll_failures(answer, kind):
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "c", "l")
    with pytest.raises(pairing.PairingError) as err:
        pairing.poll_once(FakeTransport([answer]), session)
    assert err.value.kind == kind


def test_poll_keeps_waiting_through_pending_and_outages():
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "c", "l")
    assert pairing.poll_once(FakeTransport([r(400, {"error": "authorizationPending"})]), session) == (None, 0)
    assert pairing.poll_once(FakeTransport([api.TransportError("down")]), session) == (None, 0)
    assert pairing.poll_once(FakeTransport([r(503, {})]), session) == (None, 0)


def test_poll_honours_retry_after():
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "c", "l")
    limited = api.Response(429, {"Retry-After": "40"}, b"{}")
    assert pairing.poll_once(FakeTransport([limited]), session) == (None, 40)
    dated = api.Response(503, {"Retry-After": "Mon, 21 Sep 2026 14:14:30 GMT"}, b"{}")
    assert pairing.poll_once(FakeTransport([dated]), session, now=1790000000.0) == (None, 70)


def test_the_run_waits_out_a_retry_after_longer_than_the_interval():
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "c", "l")
    linked = {"device": {"id": 9}, "token": "mp_dev_TOKEN", "capabilities": ["timing", "processed"]}
    clock = [0.0]
    t = FakeTransport([api.Response(429, {"Retry-After": "40"}, b"{}"), r(503, {}), r(201, linked)], clock)
    assert pairing.run(t, session, _clocked_wait(clock), lambda: clock[0], lambda: False).token == "mp_dev_TOKEN"
    assert t.polled_at == [0.0, 40.0, 45.0], "the floor, then back to the interval"


def test_a_cancel_or_the_codes_expiry_during_a_long_retry_after_is_noticed_at_once():
    """A long Retry-After is waited in ticks, never one uninterruptible wait."""
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "c", "l")
    clock = [0.0]
    limited = api.Response(429, {"Retry-After": "300"}, b"{}")
    with pytest.raises(pairing.PairingError) as err:
        pairing.run(FakeTransport([limited], clock), session, _clocked_wait(clock), lambda: clock[0],
                    lambda: clock[0] >= 3)
    assert err.value.kind == pairing.CANCELLED and clock[0] <= 3 + pairing.TICK_SECONDS
    short = pairing.start(FakeTransport([r(201, dict(ISSUED, expiresIn=10))]), "c", "l")
    clock = [0.0]
    with pytest.raises(pairing.PairingError) as err:
        pairing.run(FakeTransport([limited], clock), short, _clocked_wait(clock), lambda: clock[0], lambda: False)
    assert err.value.kind == pairing.EXPIRED and clock[0] <= 10 + pairing.TICK_SECONDS


def _clocked_wait(clock, waits=None):
    def wait(seconds):
        if waits is not None:
            waits.append(seconds)
        clock[0] += seconds
        return False
    return wait


def test_the_run_links_and_each_link_is_a_new_pairing():
    session = pairing.start(FakeTransport([r(201, ISSUED)]), "c", "l")
    linked = {"device": {"id": 9}, "token": "mp_dev_TOKEN", "capabilities": ["timing", "processed"]}
    clock = [0.0]
    waits = []
    t = FakeTransport([r(400, {"error": "authorizationPending"}), r(201, linked)], clock)
    creds = pairing.run(t, session, _clocked_wait(clock, waits), lambda: clock[0], lambda: False)
    assert creds.token == "mp_dev_TOKEN" and creds.device_id == 9 and creds.pairing_id
    assert t.polled_at == [0.0, 5.0], "polls no faster than the interval"
    assert max(waits) <= pairing.TICK_SECONDS, "in short ticks"
    other = pairing.run(FakeTransport([r(201, linked)]), session, _clocked_wait(clock), lambda: clock[0], lambda: False)
    assert other.pairing_id != creds.pairing_id


def test_the_run_expires_and_cancels():
    session = pairing.start(FakeTransport([r(201, dict(ISSUED, expiresIn=10))]), "c", "l")
    clock = [0.0]

    def wait(seconds):
        clock[0] += seconds
        return False

    pending = [r(400, {"error": "authorizationPending"})] * 5
    with pytest.raises(pairing.PairingError) as err:
        pairing.run(FakeTransport(pending), session, wait, lambda: clock[0], lambda: False)
    assert err.value.kind == pairing.EXPIRED
    with pytest.raises(pairing.PairingError) as err:
        pairing.run(FakeTransport(pending), session, wait, lambda: 0.0, lambda: True)
    assert err.value.kind == pairing.CANCELLED
