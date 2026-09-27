"""Sends queued events to POST /v1/scrobble.

A 202 means the server accepted the event. The event is only removed once
X-MP-Event-Processed says it was also processed; until then the same request
is sent again after a short wait.
"""

import datetime
import random
import time

from . import api, log

SCROBBLE_PATH = "/v1/scrobble"
CONFIRM_BACKOFF = (2, 5, 10, 30, 60)
RETRY_BACKOFF = (5, 15, 60, 300, 900)
PAUSE_SECONDS = 3600
NOTICE_EVERY_SECONDS = 24 * 3600
CLAIM_LEASE_SECONDS = api.TIMEOUT_SECONDS + 20

# Notices shown to the user.
NOTICE_UNLINKED = "unlinked"
NOTICE_PREMIUM = "premium"
NOTICE_INCOMPATIBLE = "incompatible"
NOTICE_DROPPED = "dropped"
NOTICE_CLOCK = "clock"


def iso8601(epoch_seconds):
    moment = datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (moment.microsecond // 1000)


def payload(event):
    """The API body for a playback event (playback.Event)."""
    body = {"action": event.action, "occurredAt": iso8601(event.occurred_at), "sessionId": event.session_id}
    for key, value in event.media.items():
        body[key] = dict((k, dict(v) if isinstance(v, dict) else v) for k, v in value.items())
    if event.position_ms is not None and event.duration_ms is not None:
        body["positionMs"] = int(event.position_ms)
        body["durationMs"] = int(event.duration_ms)
    if event.action == "stop" and event.completed:
        body["completed"] = True
    return body


def _backoff(table, attempts, rand):
    base = table[min(max(attempts, 0), len(table) - 1)]
    return base * (0.8 + 0.4 * rand())


class Sender(object):
    def __init__(self, outbox, credential_store, transport_factory, notify, clock=time.time, rand=random.random):
        self._outbox = outbox
        self._store = credential_store
        self._transport_factory = transport_factory
        self._notify = notify
        self._clock = clock
        self._rand = rand

    def _notice(self, kind, now):
        """Show each notice at most once a day (except "unlinked")."""
        key = "notice:" + kind
        last = self._outbox.get_meta(key)
        if kind != NOTICE_UNLINKED and last is not None and now - float(last) < NOTICE_EVERY_SECONDS:
            return
        self._outbox.set_meta(key, now)
        self._notify(kind)

    def step(self):
        """Send at most one event. Returns the seconds to wait before the next
        step (0 to go on right away), or None when there is nothing to send."""
        now = self._clock()
        # Read the link and pick the event under the lock, so a relink from
        # the settings dialog can't slip in between. The request itself runs
        # outside it.
        with self._outbox.exclusive():
            creds = self._store.load()
            if creds is None:
                return None
            self._outbox.purge_other_links(creds.pairing_id)
            dropped = self._outbox.expire(now)
            event = self._outbox.claim_next(creds.pairing_id, CLAIM_LEASE_SECONDS, now)
            wake = self._outbox.next_wake(creds.pairing_id) if event is None else None
        if dropped > 0:
            self._notice(NOTICE_DROPPED, now)
        if event is None:
            return None if wake is None else max(0.0, wake - now)
        headers = {"Authorization": "Bearer " + creds.token, "X-MP-Event-Id": event.event_id}
        try:
            response = self._transport_factory(creds).post(SCROBBLE_PATH, event.body(), headers)
        except api.TransportError as err:
            received = self._clock()
            log.debug("send %s: %s", event.action, err)
            self._outbox.reschedule(event.event_id, received + _backoff(RETRY_BACKOFF, event.attempts, self._rand))
            return 0
        received = self._clock()
        outcome, detail = api.classify_ingest(response)
        log.debug("send %s -> %s %s", event.action, outcome, detail)
        if outcome == api.PROCESSED:
            self._outbox.delete(event.event_id)
        elif outcome == api.ADMITTED:
            self._outbox.reschedule(event.event_id, received + _backoff(CONFIRM_BACKOFF, event.attempts, self._rand))
        elif outcome == api.RETRY:
            floor = api.retry_after_seconds(response, received)
            if floor > 0:
                self._outbox.hold(creds.pairing_id, received + floor)
            self._outbox.reschedule(event.event_id, received + max(floor, _backoff(RETRY_BACKOFF, event.attempts, self._rand)))
        elif outcome == api.REJECTED:
            log.warning("the server refused a %s event: %s", event.action, detail)
            self._outbox.delete(event.event_id)
        elif outcome == api.CLOCK:
            self._outbox.delete(event.event_id)
            self._notice(NOTICE_CLOCK, received)
        elif outcome == api.UNAUTHORIZED:
            # Remove the link only if it is still the one this request used.
            with self._outbox.exclusive():
                removed = self._store.clear_if(creds.pairing_id)
                if removed:
                    self._outbox.purge_link(creds.pairing_id)
            if removed:
                self._notice(NOTICE_UNLINKED, received)
            return None
        else:  # forbidden or incompatible: keep the events, retry in an hour
            self._outbox.hold(creds.pairing_id, received + PAUSE_SECONDS)
            self._outbox.reschedule(event.event_id, received + PAUSE_SECONDS)
            self._notice(NOTICE_PREMIUM if outcome == api.FORBIDDEN else NOTICE_INCOMPATIBLE, received)
        return 0
