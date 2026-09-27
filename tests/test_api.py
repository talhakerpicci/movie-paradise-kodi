import email.utils
import io
import urllib.error

import pytest

from resources.lib import api


def resp(status, headers=None, body=b""):
    return api.Response(status, headers or {}, body)


ACK = {"X-MP-Capabilities": "timing,processed"}


def test_only_an_explicit_processed_true_acknowledges():
    assert api.classify_ingest(resp(202, dict(ACK, **{"X-MP-Event-Processed": "true"}))) == (api.PROCESSED, "")
    assert api.classify_ingest(resp(202, dict(ACK, **{"X-MP-Event-Processed": "false"})))[0] == api.ADMITTED


@pytest.mark.parametrize("headers", [
    {},
    {"X-MP-Event-Processed": "true"},
    {"X-MP-Event-Processed": "yes", "X-MP-Capabilities": "timing,processed"},
    {"X-MP-Event-Processed": "true", "X-MP-Capabilities": "timing"},
])
def test_a_202_without_the_acknowledgement_is_incompatible(headers):
    assert api.classify_ingest(resp(202, headers))[0] == api.INCOMPATIBLE


@pytest.mark.parametrize("status,body,outcome", [
    (401, b'{"error":"invalid"}', api.UNAUTHORIZED),
    (403, b'{"error":"notPermitted"}', api.FORBIDDEN),
    (429, b"", api.RETRY),
    (503, b'{"error":"temporarilyUnavailable"}', api.RETRY),
    (500, b"", api.RETRY),
    (400, b'{"error":"body contains unknown key \\"positionMs\\""}', api.INCOMPATIBLE),
    (400, b'{"error":"body contains badly-formed JSON"}', api.REJECTED),
    (409, b'{"error":"eventIdBodyMismatch"}', api.REJECTED),
    (422, b'{"error":"occurredAtOutOfRange"}', api.CLOCK),
    (422, b'{"error":"unsupportedShowIds"}', api.REJECTED),
    (302, b"", api.INCOMPATIBLE),
])
def test_classification(status, body, outcome):
    assert api.classify_ingest(resp(status, body=body))[0] == outcome


def test_retry_after_seconds_and_dates():
    assert api.retry_after_seconds(resp(503, {"Retry-After": "30"})) == 30
    now = 1_790_000_000
    date = email.utils.formatdate(now + 120, usegmt=True)
    assert 118 <= api.retry_after_seconds(resp(503, {"Retry-After": date}), now=now) <= 120
    assert api.retry_after_seconds(resp(503)) == 0
    assert api.retry_after_seconds(resp(503, {"Retry-After": "soon"})) == 0


def test_the_transport_only_speaks_https():
    with pytest.raises(ValueError):
        api.Transport("http://api.movieparadise.app", "ua")


class FakeOpener(object):
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return self.behaviour(request)


class FakeRaw(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, n):
        return self._body[:n]


def test_a_post_carries_json_and_the_headers():
    opener = FakeOpener(lambda req: FakeRaw(202, {"X-MP-Event-Processed": "false"}, b'{"status":"accepted"}'))
    t = api.Transport("https://api.example", "MovieParadise-Kodi/0.1.0", opener)
    r = t.post("/v1/scrobble", {"a": 1}, {"X-MP-Event-Id": "e-1"})
    request, timeout = opener.requests[0]
    assert request.full_url == "https://api.example/v1/scrobble" and request.data == b'{"a":1}'
    assert request.get_header("X-mp-event-id") == "e-1" and request.get_header("User-agent") == "MovieParadise-Kodi/0.1.0"
    assert timeout == api.TIMEOUT_SECONDS and r.status == 202


def test_http_errors_are_answers_not_exceptions():
    def fail(req):
        raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(b'{"error":"invalidTiming"}'))
    r = api.Transport("https://api.example", "ua", FakeOpener(fail)).post("/v1/scrobble", {})
    assert r.status == 422 and r.error_key() == "invalidTiming"


def test_network_failures_and_oversized_answers_raise():
    def boom(req):
        raise urllib.error.URLError("down")
    with pytest.raises(api.TransportError):
        api.Transport("https://api.example", "ua", FakeOpener(boom)).post("/x", {})
    big = FakeOpener(lambda req: FakeRaw(200, {}, b"x" * (api.MAX_RESPONSE_BYTES + 10)))
    with pytest.raises(api.TransportError):
        api.Transport("https://api.example", "ua", big).post("/x", {})


def test_redirects_are_refused():
    handler = api._RefuseRedirects()
    request = urllib.request.Request("https://api.example/v1/scrobble", data=b"{}", headers={"Authorization": "Bearer secret"})
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, "https://evil.example/")


import urllib.request  # noqa: E402  (used above; kept last for the stubbed environment)
