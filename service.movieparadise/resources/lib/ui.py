"""The settings actions: link, status and unlink."""

import xbmc
import xbmcgui

from . import api, kodi, log, pairing
from .credentials import CredentialStore
from .outbox import Outbox

_PAIRING_MESSAGES = {
    pairing.EXPIRED: 32035,
    pairing.QUOTA: 32036,
    pairing.CONSUMED: 32037,
    pairing.INCOMPATIBLE: 32038,
    pairing.NETWORK: 32039,
    pairing.SERVER: 32039,
}


def _store():
    return CredentialStore(kodi.profile_dir())


def _outbox():
    return Outbox(kodi.profile_dir().rstrip("/\\") + "/queue.sqlite")


def link():
    transport = api.Transport(kodi.API_BASE, kodi.user_agent())
    dialog = xbmcgui.Dialog()
    try:
        session = pairing.start(transport, kodi.CLIENT_ID, kodi.device_label())
    except pairing.PairingError as err:
        dialog.ok(kodi.text(32030), kodi.text(_PAIRING_MESSAGES.get(err.kind, 32039)))
        return
    monitor = xbmc.Monitor()
    progress = xbmcgui.DialogProgress()
    lines = [
        kodi.text(32031),
        "[B]%s[/B]" % session.user_code,
        kodi.text(32032, url=session.verification_uri),
    ]
    progress.create(kodi.text(32030), "\n".join(lines))

    def on_tick(remaining):
        minutes = max(1, int(remaining // 60) + (1 if remaining % 60 else 0))
        percent = int(100 * remaining / max(session.expires_in, 1))
        progress.update(max(0, min(100, percent)), "\n".join(lines + [kodi.text(32033, minutes=minutes)]))

    try:
        creds = pairing.run(
            transport, session,
            wait=monitor.waitForAbort,
            monotonic=_monotonic,
            cancelled=progress.iscanceled,
            on_tick=on_tick)
    except pairing.PairingError as err:
        progress.close()
        if err.kind != pairing.CANCELLED:
            dialog.ok(kodi.text(32030), kodi.text(_PAIRING_MESSAGES.get(err.kind, 32039)))
        return
    progress.close()
    creds.api_base = kodi.API_BASE
    queue = _outbox()
    try:
        with queue.exclusive():  # the link lock: never interleaves with the service's link checks
            _store().save(creds)
            queue.purge_other_links(creds.pairing_id)
    finally:
        queue.close()
    log.info("linked")
    dialog.ok(kodi.text(32030), kodi.text(32034))


def status():
    creds = _store().load()
    dialog = xbmcgui.Dialog()
    if creds is None:
        dialog.ok(kodi.text(32000), kodi.text(32041))
        return
    queue = _outbox()
    try:
        waiting = queue.count(creds.pairing_id)
    finally:
        queue.close()
    dialog.ok(kodi.text(32000), "%s\n%s" % (kodi.text(32040), kodi.text(32049, count=waiting)))


def unlink():
    dialog = xbmcgui.Dialog()
    if not dialog.yesno(kodi.text(32000), kodi.text(32042)):
        return
    queue = _outbox()
    try:
        with queue.exclusive():
            _store().clear()
            queue.purge_all()
    finally:
        queue.close()
    dialog.ok(kodi.text(32000), kodi.text(32043))


def _monotonic():
    import time
    return time.monotonic()


def main(argv):
    kodi.install_log_sink()
    action = argv[1] if len(argv) > 1 else ""
    if not action:
        choice = xbmcgui.Dialog().select(kodi.text(32000), [kodi.text(32050), kodi.text(32051), kodi.text(32052)])
        action = {0: "link", 1: "status", 2: "unlink"}.get(choice, "")
    if action == "link":
        link()
    elif action == "status":
        status()
    elif action == "unlink":
        unlink()
