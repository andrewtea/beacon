"""Where push subscriptions are kept: `data/push/mock.sqlite3` or `live.sqlite3`.

Everything the file holds about a device, which is what a person is told before
they turn notifications on (`notice.ABOUT`):

- **A subscription** (`subscription`): the address the push service made for one
  device and this site; the places the device asked to be notified about, each
  rounded to about a block, with no name, and with when this file was first told
  of it; and when the subscription was last renewed. No key of the browser's, no address a request
  came from, no browser's name.
- **What was sent to it** (`sent`): for each incident a push was tried for, the
  incident's id beside the address, when the latest try was claimed, how many
  tries there were, and what came of the latest. It is what keeps a device from
  being sent a push for one incident again and again.

- **A permit** (`permit`): a random number the server handed a device that is
  about to give its address, and until when it is good, which is `PERMIT_LIFE`
  at most. It is beside no address and no place, and it is deleted when it is
  used, when it is withdrawn, and when its time is over.

How long:

- **A subscription is deleted** when the person turns notifications off
  (`remove`), when the push service says its address is gone, and `KEPT` after it
  was last renewed (`sweep`). What was sent to it goes with it, and a `sent` row is
  kept no longer than `SENT_KEPT` in any case.

Sending:

- **A place has the time this file was first told of it**, which a renewal keeps
  for a place that is still named: a push is for crisis mode turned on near a
  place the device had already named, so adding a place brings no push about a
  decision made before. A new address has new times, whatever address the device
  had before: the file does not know one device's addresses from another's.
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

Giving an address:

- **An address is kept only with a permit, which is good once** (`permit`,
  `put`). A request to be notified can reach the server late: its sender gave up
  on it, turned notifications off, and was told that the address is deleted, and
  then the request arrives and would put the address and its places back, for
  `KEPT`. So the sender names the permits of the requests it has out when it asks
  for a deletion (`remove`), and the deletion takes them away in the same write.
  Whichever of the two comes first, the file ends without the address: a request
  whose permit is gone keeps nothing. A permit is also taken away by the request
  for the next one (`permit(revoke=...)`), so an older request to be notified,
  with places that were removed since, cannot follow a newer one.
- A file is the mock store's or the live store's for life, in one shape
  (`beacon.files.begin_file`). **Shape 1** kept a place without the time it was
  added, and is no shape this code can read or write; **shape 2** took an address
  without a permit. Neither was ever written by a Beacon on `main`, and there is
  one rule for both: a file in an earlier shape has its subscriptions and what
  was sent to them deleted when it is opened (`_EXPIRE`), which is the one thing
  that can be done with them on time. **Shape 3** has the rows of this one, from
  before a device said which words it holds when it gave its address
  (`Notice.id`): the live file's are kept and read as they are, because the
  words for real sources did not change, and the mock file's go the same way as
  the earlier shapes', with every permit that is out, because the words a device
  was handed for the invented scenario said that a stand-in decided. A device
  whose subscription went that way gives the server its address again when its
  app is next opened, with places that are added then.
"""

import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Collection, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from beacon.errors import raise_bare
from beacon.evidence import Point
from beacon.files import Mode, begin_file, change_as_it_is

# How long a subscription is kept after it was last renewed. The app renews it
# when it is opened with notifications on, and when a place is saved or removed.
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

# How long a permit to give an address is good for: longer than the app waits
# for one telling of the server, and no longer than a late request can matter.
PERMIT_LIFE = timedelta(minutes=2)
# The most permits that are out at once. One more is refused.
MAX_PERMITS = 2_000
# What a permit looks like: 24 random bytes, in URL-safe base64.
PERMIT_SHAPE = re.compile(r"[A-Za-z0-9_-]{32}")
# What `put` answers for a permit that is used, withdrawn, over, or was never
# given: nothing was kept.
SPENT: Literal["spent"] = "spent"

Outcome = Literal["sent", "again", "failed"]

_WAIT_SECONDS = 5.0
_FORMAT = 4
# 0 is a file that says no shape, which a new file is too. 1 kept places as pairs
# and a time a subscription was made, and 2 took an address without a permit.
_EARLIER = frozenset({0, 1, 2})
# 3 has the rows of this shape, from before a device said which words it holds
# when it gave its address. **What becomes of them is the mode's.** The words for
# real sources are what they were, so the live file's rows are read as they are.
# The words for the invented scenario said that a stand-in decided, which a
# device that has not opened the app since still shows, and Muse Ember can now
# investigate there (`beacon.telling`): the mock file's subscriptions go, with
# what was sent to them and every permit that is out, and a device is told anew,
# with the words of now, when it next opens the app.
_WORDS_KEPT = 3
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
    "CREATE TABLE IF NOT EXISTS permit (id TEXT PRIMARY KEY, until TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
# Run on a file of an earlier shape, after `_SCHEMA`: its subscriptions and what
# was sent to them go, and the two tables are made anew in this shape. The file's
# mode stays.
_EXPIRE = (
    "DROP TABLE IF EXISTS sent",
    "DROP TABLE IF EXISTS subscription",
    # A permit that is out was given to a device of the earlier shape's.
    "DELETE FROM permit",
    _SCHEMA[0],
    _SCHEMA[1],
)
# Every shape this Beacon takes a subscriptions file in, the tables one has in any
# of them, and the one no other of Beacon's files has: how a file is told from
# another kind's without opening it for use.
SHAPES = frozenset({_FORMAT, _WORDS_KEPT, *_EARLIER})
TABLES = frozenset({"subscription", "sent", "permit", "meta"})
OWN_TABLE = "subscription"
# The shapes in which what was sent is forgotten as the file is (`forget_sent`).
# A mock file of an earlier shape has what was sent deleted, with its
# subscriptions, by whatever opens it next (`_EXPIRE`).
FORGET_SHAPES = frozenset({_FORMAT})
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
    # When this file was first told of it, since the device last named it
    # without a break. A device can have saved the place long before.
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
            earlier=_EARLIER | (frozenset({_WORDS_KEPT}) if mode == "mock" else frozenset()),
            drop=_EXPIRE,
            unknown=_UNKNOWN_SHAPE,
            readable=frozenset({_WORDS_KEPT}) if mode == "live" else frozenset(),
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

    def permit(self, now: datetime, revoke: Collection[str] = ()) -> str | None:
        """A permit to give an address once, good for `PERMIT_LIFE`, with the
        permits in `revoke` taken away in the same write: they are the ones of
        earlier requests of the same device, which are then too late whenever
        they come. Returns None when as many permits are out as may be.

        Raises SubscriptionsError when the file is closed."""
        made = secrets.token_urlsafe(24)
        with self._writing() as db:
            _withdraw(db, revoke)
            db.execute("DELETE FROM permit WHERE until <= ?", (_stamp(now),))
            if db.execute("SELECT count(*) FROM permit").fetchone()[0] >= MAX_PERMITS:
                return None
            db.execute(
                "INSERT INTO permit (id, until) VALUES (?, ?)", (made, _stamp(now + PERMIT_LIFE))
            )
        return made

    def put(
        self,
        endpoint: str,
        points: Sequence[Point],
        now: datetime,
        *,
        permit: str | None = None,
    ) -> datetime | Literal["spent"] | None:
        """Keep a subscription, or renew the one kept under its address with these
        places in place of the ones it had. A place it had already keeps the time
        it was added; any other is added now. Returns the moment it is kept until,
        or None when the file is full and this would be one more.

        With a `permit`, which a request from a device always has, the permit is
        used up in the same write, and `SPENT` is the answer, with nothing kept,
        when it is not there to use: it was used, withdrawn by a deletion or by
        the request for a later permit, or its time is over.

        Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            # First, so that a file that is full of what ran out is not full.
            _sweep(db, now)
            if permit is not None:
                used = db.execute("DELETE FROM permit WHERE id = ?", (permit,))
                if used.rowcount != 1:
                    return SPENT
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

    def remove(self, endpoint: str | None, permits: Collection[str] = ()) -> bool:
        """Delete a subscription and what was sent to it, and take away
        `permits` in the same write: the permits of the requests to be notified
        that its device has out, none of which can then put the address back.
        With no address, only the permits go. Returns whether there was a
        subscription. Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            _withdraw(db, permits)
            if endpoint is None:
                return False
            db.execute("DELETE FROM sent WHERE endpoint = ?", (endpoint,))
            gone = db.execute("DELETE FROM subscription WHERE endpoint = ?", (endpoint,))
            return gone.rowcount > 0

    def permits(self) -> int:
        """How many permits are out."""
        with closing(self._connect()) as db:
            return db.execute("SELECT count(*) FROM permit").fetchone()[0]

    def sweep(self, now: datetime) -> int:
        """Delete the subscriptions that were last renewed more than `KEPT` ago,
        with what was sent to them, what was sent more than `SENT_KEPT` ago, and
        the permits whose time is over.
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

    def tries(self, incident_ids: Collection[str] | None = None) -> dict[tuple[str, str], Try]:
        """What is kept of the sends about these incidents, under each send's
        address and its incident's id; of every send when none are named. A pass
        reads it once for the incidents it has a push due for, to pass over what
        needs no send without a write."""
        query = "SELECT endpoint, incident_id, tries, state, claimed_at FROM sent"
        named: tuple[str, ...] = ()
        if incident_ids is not None:
            named = tuple(dict.fromkeys(incident_ids))
            query += f" WHERE incident_id IN ({', '.join('?' * len(named))})"
        with closing(self._connect()) as db:
            rows = db.execute(query, named).fetchall() if incident_ids is None or named else []
        return {
            (endpoint, incident_id): Try(tries, state, _moment(claimed_at))
            for endpoint, incident_id, tries, state, claimed_at in rows
        }

    def claim(self, endpoint: str, incident_id: str, place: Place, now: datetime) -> bool:
        """Whether a push about this incident may be sent to this address now,
        for `place`. True once, and again only for a send that may be tried once
        more (`Try.again_at`). False for an address that is no longer kept, for
        one whose time is over at `now`, whether or not a sweep has deleted it
        yet, and for one that no longer names `place` as it was when the send was
        found to be due: a place that was removed, or removed and named again,
        since.

        Raises SubscriptionsError when the file is closed."""
        with self._writing() as db:
            # The same test as `_sweep` makes: a sweep can have failed, and what
            # it would have deleted is sent nothing.
            kept = db.execute(
                "SELECT places FROM subscription WHERE endpoint = ? AND renewed_at >= ?",
                (endpoint, _stamp(now - KEPT)),
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
    db.execute("DELETE FROM permit WHERE until <= ?", (_stamp(now),))
    return gone


def _withdraw(db: sqlite3.Connection, permits: Collection[str]) -> None:
    for permit in dict.fromkeys(permits):
        db.execute("DELETE FROM permit WHERE id = ?", (permit,))


def forget_sent(path: Path, mode: Mode) -> int | None:
    """Delete what the subscriptions file at `path` keeps of every push that was
    sent or tried, and nothing else: the subscriptions, their places, and the
    permits stay. Returns how many rows went, or None, with nothing written, when
    the file is not `mode`'s subscriptions in a shape of `FORGET_SHAPES`.

    For when the invented story is begun anew (`beacon.serve.set_aside`): what
    was sent is kept under an incident's id, and a story begun anew can give an
    incident the id one had in the last telling, of which a device would then not
    be notified. Sooner than `SENT_KEPT`, which is the most a row is kept.

    **The file is changed as it is** (`beacon.files.change_as_it_is`), by whoever
    holds the data directory's stores alone: it is not opened as `Subscriptions`
    opens it, which makes a file this shape and, for a mock file of an earlier
    one, deletes its subscriptions.

    Raises sqlite3.Error when the file is not there or cannot be written to."""
    return change_as_it_is(
        path,
        mode,
        shapes=FORGET_SHAPES,
        tables=TABLES,
        own=OWN_TABLE,
        statements=(("DELETE FROM sent", ()),),
    )
