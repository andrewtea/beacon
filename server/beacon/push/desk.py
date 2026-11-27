"""The pass that sends pushes.

`Notifier.notify` looks at one reading of the incidents and sends a push to every
subscription that is due one. Code decides who is due, and asks no model:

- the incident is open, is no medical response, and has a point that is its
  block's. An incident whose only point Beacon took from a 511 event brings no
  push: a notification says an incident is near a place and can carry no credit,
  and 511's point is shown only with 511's credit, as the saved places' page does;
- the investigator's decision turned crisis mode on for it (`Runs.crisis`), and
  the incident still has a signal of a kind the decision cited, which is what the
  incident's own page asks before it shows the decision's signals;
- one of the subscription's places is within `REACH_M` of the incident's point,
  as the saved-place check measures it, and was named before the decision was
  made: a push says that crisis mode was turned on near a place the device had
  named, and a place that is named while it is on is shown that by the app,
  where the person is.

A push is a POST with no body to the address the push service gave, signed with
the server's key. Its answer is its status: nothing of a reply's body is read or
waited for. Nothing of an address, an incident, or a place is logged or raised: a
pass answers with counts.

Sending is best effort (`store`): a push can fail to arrive, and a device can be
notified twice for one incident.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from beacon.incident import Snapshot
from beacon.ingest.http import SourceError, SourceHttp, fetch
from beacon.investigate import Runs
from beacon.places import REACH_M
from beacon.push.endpoint import audience
from beacon.push.store import Subscriptions, SubscriptionsError
from beacon.push.vapid import Vapid
from beacon.radio.vocabulary import MEDICAL_EVENT

# The most pushes one pass sends, and the longest it lasts, from before it reads
# anything. What is still due is sent by a later pass: it was not claimed.
MAX_SENDS = 200
PASS_SECONDS = 30.0
# The longest one push service is waited for, when the pass has that long left.
SEND_SECONDS = 10.0
# With less than this left, a pass sends nothing more: seconds.
_LEAST_SECONDS = 0.5
# How long a push service keeps a push for a device it cannot reach, in seconds.
TTL_SECONDS = 3600
# How long one signed token is used for a push service before another is signed.
# Apple asks for no more than one an hour, and a token is good for far longer
# (`vapid.TOKEN_LIFE`).
TOKEN_REUSED = timedelta(hours=1)
# The most tokens that are kept. Edge's and Safari's push services are many hosts
# under one name, so the hosts a file's addresses name are not few by themselves.
MAX_TOKENS = 64
# What a push service is called in an error. Which one it was is not said.
_SOURCE = "A push service"
# The address is gone: the person removed the site's permission, or the browser
# made another address.
_GONE = frozenset({404, 410})


@dataclass(frozen=True)
class Pass:
    """What one pass did, in counts."""

    sent: int = 0
    # Addresses a push service said are gone, which were deleted.
    gone: int = 0
    # Sends a push service did not say it took, which are tried again.
    again: int = 0
    # Sends a push service refused in a way that trying again does not mend.
    failed: int = 0
    # Whether the pass stopped with more to send.
    more: bool = False

    @property
    def quiet(self) -> bool:
        return not (self.sent or self.gone or self.again or self.failed or self.more)


class Notifier:
    """Sends pushes for one file of subscriptions."""

    def __init__(
        self,
        subscriptions: Subscriptions,
        runs: Runs,
        vapid: Vapid,
        http: SourceHttp,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._subscriptions = subscriptions
        self._runs = runs
        self._vapid = vapid
        self._http = http
        self._monotonic = monotonic
        # The token in use for each push service, with when it was signed. A pass
        # is made by one thread at a time.
        self._tokens: dict[str, tuple[datetime, str]] = {}

    def close(self) -> None:
        """End a send that is out and make no more."""
        self._http.close()

    def notify(self, snapshot: Snapshot, now: datetime) -> Pass:
        """Send what is due on this reading of the incidents. Subscriptions whose
        time is over are deleted first.

        Raises SubscriptionsError when the subscriptions file is closed."""
        # From before anything is read: the reading is part of what a pass takes.
        stop_at = self._monotonic() + PASS_SECONDS
        self._subscriptions.sweep(now)
        self._forget_tokens(now)
        due = []
        for incident in snapshot.incidents:
            if (
                not incident.open
                or incident.point is None
                or incident.point_from != "blocks"
                or incident.category == MEDICAL_EVENT
            ):
                continue
            decision = self._runs.crisis(incident.id)
            if decision is None:
                continue
            cited = {cited.kind for cited in decision.cited}
            if any(signal.kind in cited for signal in incident.signals):
                due.append((incident.id, incident.point, decision.decided_at))
        if not due or self._http.closed:
            return Pass()
        # Read once. A send that needs nothing more is passed over without a
        # write, so the sends a pass is allowed all go to what is still due, and
        # the devices that were kept last are reached however many came before.
        tried = self._subscriptions.tries()
        counts = {"sent": 0, "gone": 0, "again": 0, "failed": 0}
        sends = 0
        for subscription in self._subscriptions.all():
            for incident_id, point, decided_at in due:
                before = tried.get((subscription.endpoint, incident_id))
                if before is not None and not before.again_at(now):
                    continue
                place = next(
                    (
                        place
                        for place in subscription.places
                        if place.added_at < decided_at and place.point.distance_m(point) <= REACH_M
                    ),
                    None,
                )
                if place is None:
                    continue
                left = stop_at - self._monotonic()
                if sends >= MAX_SENDS or left < _LEAST_SECONDS or self._http.closed:
                    return Pass(**counts, more=True)
                # The file is asked again, in one write: the subscription may have
                # been deleted or given other places since it was read.
                if not self._subscriptions.claim(subscription.endpoint, incident_id, place, now):
                    continue
                sends += 1
                outcome = self._send(subscription.endpoint, now, min(SEND_SECONDS, left))
                counts[outcome] += 1
                try:
                    if outcome == "gone":
                        self._subscriptions.remove(subscription.endpoint)
                        break
                    self._subscriptions.settle(subscription.endpoint, incident_id, outcome)
                except SubscriptionsError:
                    # Closed while the push was out. Its claim stays as it is.
                    return Pass(**counts, more=True)
        return Pass(**counts)

    def _forget_tokens(self, now: datetime) -> None:
        """Drop the tokens that are no longer used, so that what is kept is the
        push services of this hour and not of every hour since the server began."""
        self._tokens = {
            service: signed
            for service, signed in self._tokens.items()
            if timedelta(0) <= now - signed[0] < TOKEN_REUSED
        }

    def _token(self, service: str, now: datetime) -> str:
        signed = self._tokens.get(service)
        if signed is None or not timedelta(0) <= now - signed[0] < TOKEN_REUSED:
            if service not in self._tokens and len(self._tokens) >= MAX_TOKENS:
                # The one signed longest ago makes room. Its push service is
                # signed for again when a push next goes to it.
                del self._tokens[min(self._tokens, key=lambda kept: self._tokens[kept][0])]
            signed = (now, self._vapid.authorization(service, now))
            self._tokens[service] = signed
        return signed[1]

    def _send(
        self, endpoint: str, now: datetime, seconds: float
    ) -> Literal["sent", "gone", "again", "failed"]:
        """Send one push, waiting `seconds` at the longest, and say what came of
        it: `sent`, `gone`, `again`, or `failed`. It raises nothing about the
        address."""
        problem: SourceError | None = None
        try:
            fetch(
                self._http,
                "POST",
                endpoint,
                source=_SOURCE,
                max_bytes=0,
                headers={
                    "Authorization": self._token(audience(endpoint), now),
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "high",
                },
                max_seconds=seconds,
                # A 2xx status is the push service taking the push. Its body is
                # not waited for: a body that failed to arrive would otherwise
                # have a push that was taken sent a second time.
                read_body=False,
            )
        except SourceError as error:
            problem = error
        if problem is None:
            return "sent"
        if problem.status in _GONE:
            return "gone"
        if problem.status is None or problem.status == 429 or problem.status >= 500:
            # Not reached, not answered in time, or told to come back. Whether
            # the push service took a push it did not answer for is not known.
            return "again"
        return "failed"
