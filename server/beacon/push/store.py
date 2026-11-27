"""Where push subscriptions are kept: `data/push/mock.sqlite3` or `live.sqlite3`.

- **A subscription is an address and places.** The address is the push service's
  for one device and this site; the places are those the device asked to be
  notified about, each rounded to about a block, with no name. With them are kept
  when the subscription was first made and when it was last renewed, and nothing
  else: no key of the browser's, no address a request came from, no browser's name.
- **A subscription is deleted** when the person turns notifications off
  (`remove`), when the push service says its address is gone, and `KEPT` after it
  was last renewed (`sweep`). What was sent to it goes with it.
- **A send is claimed before it is made** (`claim`), under the subscription's
  address and the incident's id, so a device is sent one push for an incident. A
  send the push service did not say it took is tried again, `MAX_TRIES` times in
  all and no sooner than `RETRY_AFTER` for each try so far (`settle`): a device
  that got one twice shows it once, because the notification replaces itself.
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
# whenever it is opened with notifications on.
KEPT = timedelta(days=30)
# The most subscriptions one file holds.
MAX_SUBSCRIPTIONS = 2_000
# The most times one push is tried, and how long is waited for each try so far.
MAX_TRIES = 3
RETRY_AFTER = timedelta(seconds=60)
# How long a claim is kept: longer than an incident's id or a decision on crisis
# mode lasts.
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
        created_at TEXT NOT NULL,
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
class Subscription:
    endpoint: str
    # Each rounded to about a block.
    points: tuple[Point, ...]
    created_at: datetime


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
        places in place of the ones it had. Returns the moment it is kept until,
        or None when the file is full and this would be one more.

        Raises SubscriptionsError when the file is closed."""
        places = json.dumps([[point.lat, point.lon] for point in points])
        with self._writing() as db:
            # First, so that a file that is full of what ran out is not full.
            _sweep(db, now)
            known = db.execute(
                "SELECT 1 FROM subscription WHERE endpoint = ?", (endpoint,)
            ).fetchone()
            if known is None:
                count = db.execute("SELECT count(*) FROM subscription").fetchone()[0]
                if count >= MAX_SUBSCRIPTIONS:
                    return None
            db.execute(
                "INSERT INTO subscription (endpoint, places, created_at, renewed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (endpoint) DO UPDATE SET "
                "places = excluded.places, renewed_at = excluded.renewed_at",
                (endpoint, places, _stamp(now), _stamp(now)),
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
        and return how many. Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            return _sweep(db, now)

    def all(self) -> tuple[Subscription, ...]:
        """Every subscription, oldest first: at most `MAX_SUBSCRIPTIONS`."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT endpoint, places, created_at FROM subscription "
                "ORDER BY created_at, endpoint LIMIT ?",
                (MAX_SUBSCRIPTIONS,),
            ).fetchall()
        return tuple(
            Subscription(
                endpoint=endpoint,
                points=tuple(Point(lat, lon) for lat, lon in json.loads(places)),
                created_at=_moment(created_at),
            )
            for endpoint, places, created_at in rows
        )

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT count(*) FROM subscription").fetchone()[0]

    def claim(self, endpoint: str, incident_id: str, now: datetime) -> bool:
        """Whether a push about this incident may be sent to this address now. True
        once, and again only for a send that was settled as `again`, was tried
        fewer than `MAX_TRIES` times, and was last tried long enough ago. False
        for an address that is no longer kept.

        Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            kept = db.execute(
                "SELECT 1 FROM subscription WHERE endpoint = ?", (endpoint,)
            ).fetchone()
            if kept is None:
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
            if (
                state != "again"
                or tries >= MAX_TRIES
                or _moment(claimed_at) + RETRY_AFTER * tries > now
            ):
                return False
            db.execute(
                "UPDATE sent SET tries = tries + 1, state = 'claimed', claimed_at = ? "
                "WHERE endpoint = ? AND incident_id = ?",
                (_stamp(now), endpoint, incident_id),
            )
            return True

    def settle(self, endpoint: str, incident_id: str, outcome: Outcome) -> None:
        """Say what came of a send that was claimed. Raises SubscriptionsError
        when the file is closed: the claim then stays, and the send is not tried
        again."""
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
