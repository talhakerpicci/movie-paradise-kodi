"""HTTP client for the Movie Paradise API.

HTTPS only. Redirects are refused (they could send the token to another
host), responses are size-capped and requests time out. Headers and bodies
are never logged.
"""

import email.utils
import json
import socket
import ssl
import time
import urllib.error
import urllib.request

from . import log

TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024
REQUIRED_CAPABILITIES = frozenset(("timing", "processed"))


class TransportError(Exception):
    """No HTTP response (network error, TLS, timeout, or too large)."""


class Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = dict((k.lower(), v) for k, v in (headers or {}).items())
        self.body = body or b""

    def json(self):
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def error_key(self):
        value = self.json().get("error")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return json.dumps(value, sort_keys=True)
        return ""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def default_opener():
    context = ssl.create_default_context()
    return urllib.request.build_opener(_RefuseRedirects(), urllib.request.HTTPSHandler(context=context))


class Transport(object):
    def __init__(self, base_url, user_agent, opener=None):
        base_url = (base_url or "").rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("the API base URL must use https")
        self.base_url = base_url
        self.user_agent = user_agent
        self._opener = opener or default_opener()

    def post(self, path, payload, headers=None):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        all_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        all_headers.update(headers or {})
        request = urllib.request.Request(self.base_url + path, data=data, method="POST", headers=all_headers)
        try:
            raw = self._opener.open(request, timeout=TIMEOUT_SECONDS)
            status, response_headers = raw.status, raw.headers
            body = raw.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            status, response_headers = err.code, err.headers
            try:
                body = err.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, AttributeError, ValueError):
                body = b""
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError, ValueError) as err:
            raise TransportError(type(err).__name__)
        if body is not None and len(body) > MAX_RESPONSE_BYTES:
            raise TransportError("response too large")
        return Response(status, dict(response_headers.items()) if response_headers is not None else {}, body)


def retry_after_seconds(response, now=None):
    """Retry-After in seconds (seconds or HTTP date), 0 if missing."""
    value = (response.headers.get("retry-after") or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return 0
    if moment is None:
        return 0
    now = time.time() if now is None else now
    return max(0, int(moment.timestamp() - now))


# Ingest outcomes.
PROCESSED = "processed"        # 202 + X-MP-Event-Processed: true -> forget the event
ADMITTED = "admitted"          # 202 + false -> keep, retry the identical request later
RETRY = "retry"                # 429 / 5xx / inflight -> retry after the floor
REJECTED = "rejected"          # 400 / 409 / 422 -> never retried
CLOCK = "clock"                # 422 occurredAtOutOfRange -> dropped, the clock is reported
UNAUTHORIZED = "unauthorized"  # 401 -> the link is gone
FORBIDDEN = "forbidden"        # 403 -> keep the link, explain (Premium)
INCOMPATIBLE = "incompatible"  # an older or foreign backend -> keep the event, pause


def classify_ingest(response):
    """Classify a /v1/scrobble response as (outcome, detail)."""
    status = response.status
    if status == 202:
        processed = (response.headers.get("x-mp-event-processed") or "").strip().lower()
        capabilities = set(c.strip() for c in (response.headers.get("x-mp-capabilities") or "").split(",") if c.strip())
        if processed not in ("true", "false") or not REQUIRED_CAPABILITIES.issubset(capabilities):
            return INCOMPATIBLE, "acknowledgement headers missing"
        return (PROCESSED if processed == "true" else ADMITTED), ""
    if status == 401:
        return UNAUTHORIZED, ""
    if status == 403:
        return FORBIDDEN, response.error_key()
    if status == 429 or status >= 500:
        return RETRY, str(status)
    key = response.error_key()
    if status == 400 and "unknown key" in key:
        return INCOMPATIBLE, key
    if status == 422 and key == "occurredAtOutOfRange":
        return CLOCK, key
    if status in (400, 404, 409, 422):
        return REJECTED, key or str(status)
    # Redirects and anything unexpected: keep the event.
    return INCOMPATIBLE, str(status)


def has_required_capabilities(values):
    return REQUIRED_CAPABILITIES.issubset(set(values or ()))


def log_response(label, response):
    log.debug("%s -> %s %s", label, response.status, response.error_key())
