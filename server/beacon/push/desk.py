"""The pass that sends pushes.

`Notifier.notify` looks at one reading of the incidents and sends a push to every
subscription that is due one. Code decides who is due, and asks no model:

- the incident is open, is no medical response, and has a point;
- the investigator's decision turned crisis mode on for it (`Runs.crisis`), and
  the incident still has a signal of a kind the decision cited, which is what the
  incident's own page asks before it shows the decision's signals;
- the decision was made after the device subscribed: a push says that crisis
  mode was turned on, and a device that subscribes while it is on is shown that
  by the app, where the person is;
- one of the subscription's places is within `REACH_M` of the incident's point,
  as the saved-place check measures it.

A push is a POST with no body to the address the push service gave, signed with
the server's key. Nothing of an address, an incident, or a place is logged or
raised: a pass answers with counts.
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

# The most pushes one pass sends, and the longest it goes on sending. What is
# still due is sent by a later pass: it was not claimed.
MAX_SENDS = 200
PASS_SECONDS = 30.0
# How long a push service keeps a push for a device it cannot reach, in seconds.
TTL_SECONDS = 3600
# The most of a push service's reply that is read. Nothing in it is used.
_MAX_REPLY_BYTES = 65_536
# How long one signed token is used for a push service before another is signed.
# Apple asks for no more than one an hour, and a token is good for far longer
# (`vapid.TOKEN_LIFE`).
TOKEN_REUSED = timedelta(hours=1)
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
        self._subscriptions.sweep(now)
        due = []
        for incident in snapshot.incidents:
            if not incident.open or incident.point is None or incident.category == MEDICAL_EVENT:
                continue
            decision = self._runs.crisis(incident.id)
            if decision is None:
                continue
            cited = {cited.kind for cited in decision.cited}
            if any(signal.kind in cited for signal in incident.signals):
                due.append((incident.id, incident.point, decision.decided_at))
        if not due or self._http.closed:
            return Pass()
        counts = {"sent": 0, "gone": 0, "again": 0, "failed": 0}
        sends = 0
        stop_at = self._monotonic() + PASS_SECONDS
        for subscription in self._subscriptions.all():
            for incident_id, point, decided_at in due:
                if decided_at < subscription.created_at:
                    continue
                if not any(place.distance_m(point) <= REACH_M for place in subscription.points):
                    continue
                if sends >= MAX_SENDS or self._monotonic() >= stop_at or self._http.closed:
                    return Pass(**counts, more=True)
                if not self._subscriptions.claim(subscription.endpoint, incident_id, now):
                    continue
                sends += 1
                outcome = self._send(subscription.endpoint, now)
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

    def _send(self, endpoint: str, now: datetime) -> Literal["sent", "gone", "again", "failed"]:
        """Send one push, and say what came of it: `sent`, `gone`, `again`, or
        `failed`. It raises nothing about the address."""
        problem: SourceError | None = None
        service = audience(endpoint)
        signed = self._tokens.get(service)
        if signed is None or not timedelta(0) <= now - signed[0] < TOKEN_REUSED:
            signed = (now, self._vapid.authorization(service, now))
            self._tokens[service] = signed
        try:
            fetch(
                self._http,
                "POST",
                endpoint,
                source=_SOURCE,
                max_bytes=_MAX_REPLY_BYTES,
                headers={
                    "Authorization": signed[1],
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "high",
                },
            )
        except SourceError as error:
            problem = error
        if problem is None or problem.too_large:
            # A reply that was longer than is read was still an answer of 2xx.
            return "sent"
        if problem.status in _GONE:
            return "gone"
        if problem.status is None or problem.status == 429 or problem.status >= 500:
            # Not reached, not answered in time, or told to come back.
            return "again"
        return "failed"
