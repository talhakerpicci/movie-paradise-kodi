"""Linking Kodi to an account with a device code.

1. POST /v1/scrobble/device-code returns a user code, the link URL, the poll
   interval and the server's capabilities. We stop here if the server is too
   old.
2. The user enters the code in the Movie Paradise app.
3. POST /v1/scrobble/device-token is polled until it returns the device
   token, or the code expires.
"""

from . import api, log
from .credentials import Credentials, new_pairing_id

DEVICE_CODE_PATH = "/v1/scrobble/device-code"
DEVICE_TOKEN_PATH = "/v1/scrobble/device-token"
TICK_SECONDS = 1.0

# Failure kinds, each with its own message.
INCOMPATIBLE = "incompatible"
NETWORK = "network"
EXPIRED = "expired"
QUOTA = "quota"
CONSUMED = "consumed"
CANCELLED = "cancelled"
SERVER = "server"


class PairingError(Exception):
    def __init__(self, kind):
        Exception.__init__(self, kind)
        self.kind = kind


class PairingSession(object):
    def __init__(self, device_code, user_code, verification_uri, expires_in, interval, capabilities):
        self.device_code = device_code
        self.user_code = user_code
        self.verification_uri = verification_uri
        self.expires_in = expires_in
        self.interval = interval
        self.capabilities = capabilities


def start(transport, client_id, label):
    try:
        response = transport.post(DEVICE_CODE_PATH, {"clientId": client_id, "label": label[:80]})
    except api.TransportError:
        raise PairingError(NETWORK)
    if response.status != 201:
        raise PairingError(INCOMPATIBLE if response.status in (400, 404) else SERVER)
    body = response.json()
    capabilities = body.get("capabilities")
    if not api.has_required_capabilities(capabilities):
        raise PairingError(INCOMPATIBLE)
    device_code = body.get("deviceCode")
    user_code = body.get("userCode")
    if not isinstance(device_code, str) or not device_code or not isinstance(user_code, str) or not user_code:
        raise PairingError(SERVER)
    log.remember_secret(device_code)
    interval = body.get("interval") if isinstance(body.get("interval"), int) else 5
    expires_in = body.get("expiresIn") if isinstance(body.get("expiresIn"), int) else 600
    return PairingSession(device_code, user_code, body.get("verificationUri") or "https://movieparadise.app/link-device",
                          expires_in, max(interval, 1), list(capabilities))


def poll_once(transport, session, now=None):
    """Returns (credentials, 0) once linked, or (None, seconds to wait) while
    pending. Raises PairingError when pairing failed."""
    try:
        response = transport.post(DEVICE_TOKEN_PATH, {"deviceCode": session.device_code})
    except api.TransportError:
        return None, 0  # keep polling until the code expires
    if response.status == 201:
        body = response.json()
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise PairingError(SERVER)
        capabilities = body.get("capabilities") or session.capabilities
        device = body.get("device") if isinstance(body.get("device"), dict) else {}
        return Credentials(token, new_pairing_id(), device.get("id"), capabilities), 0
    key = response.error_key()
    if response.status == 400 and key == "authorizationPending":
        return None, 0
    if response.status == 404 or key == "pairingCodeExpired":
        raise PairingError(EXPIRED)
    if key == "pairingCodeConsumed":
        raise PairingError(CONSUMED)
    if response.status == 403 and key == "deviceQuotaExceeded":
        raise PairingError(QUOTA)
    if response.status == 429 or response.status >= 500:
        return None, api.retry_after_seconds(response, now)
    raise PairingError(SERVER)


def run(transport, session, wait, monotonic, cancelled, on_tick=None):
    """Poll until linked, expired or cancelled.

    wait(seconds) returns True when Kodi is shutting down, cancelled() when the
    user pressed cancel, and on_tick(remaining) updates the dialog. Waits in
    one-second steps so a cancel is noticed quickly.
    """
    deadline = monotonic() + session.expires_in
    next_poll = monotonic()
    while True:
        now = monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise PairingError(EXPIRED)
        if on_tick is not None:
            on_tick(remaining)
        if cancelled():
            raise PairingError(CANCELLED)
        if now < next_poll:
            if wait(min(TICK_SECONDS, next_poll - now)):
                raise PairingError(CANCELLED)
            continue
        creds, extra_wait = poll_once(transport, session)
        if creds is not None:
            return creds
        next_poll = monotonic() + max(session.interval, extra_wait)
