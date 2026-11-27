"""Where push subscriptions are kept: `data/push/mock.sqlite3` or `live.sqlite3`.

Everything the file holds about a device, which is what a person is told before
they turn notifications on (`notice.ABOUT`):

- **A subscription** (`subscription`): the address the push service made for one
  device and this site; the places the device asked to be notified about, each
  rounded to about a block, with no name, and with when it was added; and when the
  subscription was last renewed. No key of the browser's, no address a request
  came from, no browser's name.
- **What was sent to it** (`sent`): for each incident a push was tried for, the
  incident's id beside the address, when the latest try was claimed, how many
  tries there were, and what came of the latest. It is what keeps a device from
  being sent a push for one incident again and again.

How long:

- **A subscription is deleted** when the person turns notifications off
  (`remove`), when the push service says its address is gone, and `KEPT` after it
  was last renewed (`sweep`). What was sent to it goes with it, and a `sent` row is
  kept no longer than `SENT_KEPT` in any case.

Sending:

- **A place has the time it was added**, which a renewal keeps for a place that
  is still named: a push is for crisis mode turned on near a place the device had
  already named, so adding a place brings no push about a decision made before.
- **A send is claimed before it is made** (`claim`), under the subscription's
  address and the incident's id, and only while the subscription still names the
  place the send is for. The claim is settled when the push service has answered
  (`settle`). Sending is **best effort, and neither exactly once nor certain**:
  a send the push service did not say it took is tried again, `MAX_TRIES` times in
  all and no sooner than `RETRY_AFTER` for each try so far; so is a claim that was
  never settled, once it is `CLAIM_STALE` old, because the server was stopped
  between the claim and the answer. A push service that took a push and whose
  answer was lost is asked again, and the device is then notified twice.
- At most `MAX_SUBSCRIPTIONS` are kept. One more is refused.
- A file is the mock store's or the live store's for life, in one shape
  (`beacon.files.begin_file`).
"""

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from beacon.errors import raise_bare
from beacon.evidence import Point
from beacon.files import Mode, begin_file

# How long a subscription is kept after it was last renewed. The app renews it
# when it is opened with notifications on.
KEPT = timedelta(days=30)
# The most subscriptions one file holds.
MAX_SUBSCRIPTIONS = 2_000
# The most times one push is tried, and how long is waited for each try so far.
MAX_TRIES = 3
RETRY_AFTER = timedelta(seconds=60)
# How old a claim that was never settled is before it is taken to be one whose
# server was stopped under it: far longer than a pass that sends may last.
CLAIM_STALE = timedelta(minutes=5)
# How long what was sent is kept: longer than an incident's id or a decision on
# crisis mode lasts.
SENT_KEPT = timedelta(hours=48)

Outcome = Literal["sent", "again", "failed"]

_WAIT_SECONDS = 5.0
_FORMAT = 1
# 0 is a file that says no shape, which a new file is too.
_EARLIER = frozenset({0})
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS subscription (
        endpoint TEXT PRIMARY KEY,
        places TEXT NOT NULL,
        renewed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sent (
        endpoint TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        tries INTEGER NOT NULL,
        state TEXT NOT NULL,
        PRIMARY KEY (endpoint, incident_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_UNKNOWN_SHAPE = (
    "The push subscriptions file is in a shape this Beacon does not know: a newer "
    "one wrote it. It is left as it is. Run a Beacon that knows the shape."
)


class SubscriptionsError(Exception):
    """The subscriptions file cannot be used as asked. The text is fixed."""


@dataclass(frozen=True)
class Place:
    """A place a device asked to be notified about."""

    # Rounded to about a block.
    point: Point
    # When the device first named it, since it last named it without a break.
    added_at: datetime


@dataclass(frozen=True)
class Subscription:
    endpoint: str
    places: tuple[Place, ...]


@dataclass(frozen=True)
class Try:
    """What is kept of the sends of one push: how many tries, what came of the
    latest (`claimed` while none has), and when it was claimed."""

    tries: int
    state: str
    claimed_at: datetime

    def again_at(self, now: datetime) -> bool:
        """Whether the push may be tried once more now."""
        if self.tries >= MAX_TRIES:
            return False
        if self.state == "again":
            return self.claimed_at + RETRY_AFTER * self.tries <= now
        # Claimed and never settled: the server was stopped under it.
        return self.state == "claimed" and self.claimed_at + CLAIM_STALE <= now


class Subscriptions:
    """One file of push subscriptions. It is safe to share between threads."""

    def __init__(self, path: Path, mode: Mode) -> None:
        """Raises SubscriptionsError when the file was begun as the other mode's,
        and when it is in a shape this Beacon does not know. Such a file is left
        as it was."""
        self._path = path
        self._mode = mode
        self._closing = threading.Lock()
        self._closed = False
        refusal = begin_file(
            path,
            mode,
            what="push subscriptions",
            schema=_SCHEMA,
            shape=_FORMAT,
            earlier=_EARLIER,
            drop=(),
            unknown=_UNKNOWN_SHAPE,
        )
        if refusal is not None:
            raise SubscriptionsError(refusal)

    @property
    def mode(self) -> Mode:
        return self._mode

    def close(self) -> None:
        """Write nothing more. A write that is under way is finished first."""
        with self._closing:
            self._closed = True

    def put(self, endpoint: str, points: Sequence[Point], now: datetime) -> datetime | None:
        """Keep a subscription, or renew the one kept under its address with these
        places in place of the ones it had. A place it had already keeps the time
        it was added; any other is added now. Returns the moment it is kept until,
        or None when the file is full and this would be one more.

        Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            # First, so that a file that is full of what ran out is not full.
            _sweep(db, now)
            known = db.execute(
                "SELECT places FROM subscription WHERE endpoint = ?", (endpoint,)
            ).fetchone()
            if known is None:
                count = db.execute("SELECT count(*) FROM subscription").fetchone()[0]
                if count >= MAX_SUBSCRIPTIONS:
                    return None
            had = {} if known is None else {place.point: place for place in _places(known[0])}
            places = json.dumps(
                [
                    [point.lat, point.lon, _stamp(had[point].added_at if point in had else now)]
                    for point in dict.fromkeys(points)
                ]
            )
            db.execute(
                "INSERT INTO subscription (endpoint, places, renewed_at) VALUES (?, ?, ?) "
                "ON CONFLICT (endpoint) DO UPDATE SET "
                "places = excluded.places, renewed_at = excluded.renewed_at",
                (endpoint, places, _stamp(now)),
            )
        return now + KEPT

    def remove(self, endpoint: str) -> bool:
        """Delete a subscription and what was sent to it. Returns whether there
        was one. Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            db.execute("DELETE FROM sent WHERE endpoint = ?", (endpoint,))
            gone = db.execute("DELETE FROM subscription WHERE endpoint = ?", (endpoint,))
            return gone.rowcount > 0

    def sweep(self, now: datetime) -> int:
        """Delete the subscriptions that were last renewed more than `KEPT` ago,
        with what was sent to them, and what was sent more than `SENT_KEPT` ago.
        Returns how many subscriptions went. Raises SubscriptionsError when the
        file is closed."""
        with self._writing() as db:
            return _sweep(db, now)

    def all(self) -> tuple[Subscription, ...]:
        """Every subscription, in the order they were first kept: at most
        `MAX_SUBSCRIPTIONS`."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT endpoint, places FROM subscription ORDER BY rowid LIMIT ?",
                (MAX_SUBSCRIPTIONS,),
            ).fetchall()
        return tuple(
            Subscription(endpoint=endpoint, places=_places(places)) for endpoint, places in rows
        )

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT count(*) FROM subscription").fetchone()[0]

    def tries(self) -> dict[tuple[str, str], Try]:
        """What is kept of every send, under its address and its incident's id.
        A pass reads it once, to pass over what needs no send without a write."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT endpoint, incident_id, tries, state, claimed_at FROM sent"
            ).fetchall()
        return {
            (endpoint, incident_id): Try(tries, state, _moment(claimed_at))
            for endpoint, incident_id, tries, state, claimed_at in rows
        }

    def claim(self, endpoint: str, incident_id: str, place: Place, now: datetime) -> bool:
        """Whether a push about this incident may be sent to this address now,
        for `place`. True once, and again only for a send that may be tried once
        more (`Try.again_at`). False for an address that is no longer kept, and
        for one that no longer names `place` as it was when the send was found to
        be due: a place that was removed, or removed and named again, since.

        Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            kept = db.execute(
                "SELECT places FROM subscription WHERE endpoint = ?", (endpoint,)
            ).fetchone()
            if kept is None or place not in _places(kept[0]):
                return False
            found = db.execute(
                "SELECT tries, state, claimed_at FROM sent WHERE endpoint = ? AND incident_id = ?",
                (endpoint, incident_id),
            ).fetchone()
            if found is None:
                db.execute(
                    "INSERT INTO sent (endpoint, incident_id, claimed_at, tries, state) "
                    "VALUES (?, ?, ?, 1, 'claimed')",
                    (endpoint, incident_id, _stamp(now)),
                )
                return True
            tries, state, claimed_at = found
            if not Try(tries, state, _moment(claimed_at)).again_at(now):
                return False
            db.execute(
                "UPDATE sent SET tries = tries + 1, state = 'claimed', claimed_at = ? "
                "WHERE endpoint = ? AND incident_id = ?",
                (_stamp(now), endpoint, incident_id),
            )
            return True

    def settle(self, endpoint: str, incident_id: str, outcome: Outcome) -> None:
        """Say what came of a send that was claimed. Raises SubscriptionsError
        when the file is closed: the claim then stays as it is, and is tried again
        as one that was never settled."""
        with self._writing() as db:
            db.execute(
                "UPDATE sent SET state = ? WHERE endpoint = ? AND incident_id = ? "
                "AND state = 'claimed'",
                (outcome, endpoint, incident_id),
            )

    def _connect(self) -> sqlite3.Connection:
        # A connection for each operation, as the evidence store does. The file
        # was accepted when this object was built, and its journal mode set.
        return sqlite3.connect(self._path, timeout=_WAIT_SECONDS, isolation_level=None)

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """One write, begun only while the file is open and committed before
        `close` returns."""
        with self._closing:
            if self._closed:
                raise_bare(SubscriptionsError("The push subscriptions file is closed."))
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")


_STAMP = "%Y-%m-%dT%H:%M:%S.%f"


def _stamp(moment: datetime) -> str:
    """A moment as text that sorts as time does: UTC, always with microseconds."""
    return moment.astimezone(UTC).strftime(_STAMP)


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)


def _places(kept: str) -> tuple[Place, ...]:
    return tuple(
        Place(Point(lat, lon), _moment(added_at)) for lat, lon, added_at in json.loads(kept)
    )


def _sweep(db: sqlite3.Connection, now: datetime) -> int:
    gone = db.execute(
        "DELETE FROM subscription WHERE renewed_at < ?", (_stamp(now - KEPT),)
    ).rowcount
    db.execute(
        "DELETE FROM sent WHERE claimed_at < ? "
        "OR endpoint NOT IN (SELECT endpoint FROM subscription)",
        (_stamp(now - SENT_KEPT),),
    )
    return gone
