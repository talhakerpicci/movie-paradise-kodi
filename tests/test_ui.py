"""The settings actions run in their own interpreter with their own queue
connection. Check they don't race the service."""

import os
import threading
import time

import pytest

from resources.lib import api, kodi, pairing, sender as sender_mod, ui
from resources.lib.credentials import CredentialStore, Credentials
from resources.lib.outbox import Outbox


class _Dialog(object):
    def yesno(self, *args, **kwargs):
        return True

    def ok(self, *args, **kwargs):
        pass


class _Progress(object):
    def create(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def close(self):
        pass

    def iscanceled(self):
        return False


class _Monitor(object):
    def waitForAbort(self, seconds=0):
        return False


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(kodi, "profile_dir", lambda: str(tmp_path))
    monkeypatch.setattr(kodi, "text", lambda string_id, **values: str(string_id))
    monkeypatch.setattr(kodi, "user_agent", lambda: "test")
    monkeypatch.setattr(kodi, "device_label", lambda: "Living room")
    monkeypatch.setattr(ui.xbmcgui, "Dialog", _Dialog, raising=False)
    monkeypatch.setattr(ui.xbmcgui, "DialogProgress", _Progress, raising=False)
    monkeypatch.setattr(ui.xbmc, "Monitor", _Monitor, raising=False)
    CredentialStore(str(tmp_path)).save(Credentials("mp_dev_tok_OLD", "p1", 1, ["timing", "processed"]))
    return tmp_path


def test_unlink_during_a_service_write_leaves_nothing_behind(profile):
    """The service checked the link (p1) and is still writing under it."""
    service = Outbox(str(profile / "queue.sqlite"))
    started = threading.Event()

    def settings():
        started.wait(5)
        ui.unlink()

    t = threading.Thread(target=settings)
    t.start()
    try:
        with service.exclusive():
            started.set()
            time.sleep(0.3)  # the unlink runs right now
            service.add("e1", "p1", "s1", "start", time.time(), {"action": "start"})
        t.join(5)
        assert service.count() == 0 and CredentialStore(str(profile)).load() is None
    finally:
        service.close()


def test_a_relink_during_a_401_for_the_old_link_keeps_the_new_link(profile, monkeypatch):
    """The sender read the old link and is removing it when the user links
    again: the new link's save waits for the removal, never the reverse."""
    new = Credentials("mp_dev_tok_NEW", "p2", 2, ["timing", "processed"])
    session = pairing.PairingSession("dc-1234567890", "BCDF-GHJK", "https://movieparadise.app/link-device", 600, 5,
                                     ["timing", "processed"])
    monkeypatch.setattr(pairing, "start", lambda transport, client_id, label: session)
    monkeypatch.setattr(pairing, "run", lambda *a, **k: new)
    service = Outbox(str(profile / "queue.sqlite"))
    service.add("e1", "p1", "s1", "start", time.time(), {"action": "start"})
    removing = threading.Event()
    real_remove = os.remove

    def slow_remove(target):
        removing.set()
        time.sleep(0.3)  # the user finishes linking right now
        real_remove(target)

    def settings():
        removing.wait(5)
        ui.link()

    class Revoked(object):
        def post(self, path, body, headers=None):
            return api.Response(401, {}, b'{"error":"invalidAuthenticationToken"}')

    monkeypatch.setattr(api, "Transport", lambda *a, **k: object())
    monkeypatch.setattr(os, "remove", slow_remove)
    t = threading.Thread(target=settings)
    t.start()
    s = sender_mod.Sender(service, CredentialStore(str(profile)), lambda creds: Revoked(), lambda kind: None)
    try:
        assert s.step() is None
        t.join(5)
    finally:
        monkeypatch.undo()
        service.close()
    assert removing.is_set(), "the old link was removed"
    assert CredentialStore(str(profile)).load().pairing_id == "p2", "the new link survived"
