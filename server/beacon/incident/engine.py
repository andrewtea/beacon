"""The incident engine: the evidence store read as the incidents going on now.

    engine = open_engine(os.environ, store)
    snapshot = engine.snapshot()       # one reading: the incidents, and as of when
    snapshot.incidents, snapshot.as_of, snapshot.partial
    snapshot.get("inc-3f2a9c1d5b7e0a64")

Nothing the engine works out is stored. An incident is a function of the evidence
rows (`beacon.incident.build`), so there is no second record that could disagree with
the first, and a store that is deleted and begun again takes its incidents with it.
What is worked out is kept in memory for as long as the store has no new row, and
for a few seconds at most, because what is in force also changes with the time.

A reading is of one state of the store. The engine notes the id of the newest row
when it begins, and every search of the reading looks no further, so a row that is
stored while it reads is in none of it and in all of the next. A caller takes one
`Snapshot` and answers from it, and never puts two readings together.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from beacon.evidence import Evidence, EvidenceStore, Kind, Mode, Point
from beacon.incident.build import build_incidents
from beacon.incident.model import Incident
from beacon.incident.places import Blocks
from beacon.redact import Names

# How far back the engine reads. An incident older than this is gone from it, and one
# that has gone on for longer loses its first calls.
HORIZON = timedelta(hours=12)
# The most rows read, the newest first, so that what is left out is the oldest.
MAX_ROWS = 20_000
# How long what was worked out is used again while the store has no new row.
FRESH = timedelta(seconds=5)

# Taps and aircraft are not read with these: see `beacon.incident.build.Nearby`.
_KINDS = ("radio", "traffic", "alert", "notice")
# The most taps, and the most positions of aircraft, read for one incident.
MAX_NEARBY = 5_000

logger = logging.getLogger("beacon.incident")


@dataclass(frozen=True)
class Snapshot:
    """One reading of the store: the incidents, the moment they are as of, and
    whether anything was left out of the reading."""

    as_of: datetime
    # Newest first.
    incidents: tuple[Incident, ...]
    # Whether there were more rows than the engine reads: the oldest rows of the
    # last twelve hours, the taps or the aircraft near an incident, or the rows that
    # can end an old alert. An incident can then lack its first lines, count fewer
    # taps than there were, or show an alert as `unknown`.
    partial: bool
    # The id of the newest evidence row the reading took in.
    last_id: int

    def get(self, incident_id: str) -> Incident | None:
        """The incident of that id, or None when the reading has none."""
        for incident in self.incidents:
            if incident.id == incident_id:
                return incident
        return None


class Engine:
    """Reads one evidence store as incidents. It is safe to share between threads."""

    def __init__(
        self,
        store: EvidenceStore,
        blocks: Blocks,
        *,
        names: Names | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`names` are the names a text may hold to pass the redaction pass. With
        none, no word of a notice is shown."""
        self._store = store
        self._blocks = blocks
        self._names = names
        self._clock = (lambda: datetime.now(UTC)) if clock is None else clock
        self._working = threading.Lock()
        self._made: Snapshot | None = None

    @property
    def mode(self) -> Mode:
        """`mock` when the store is the invented scenario's: every incident read
        from it is then a stand-in's."""
        return self._store.mode

    @property
    def store(self) -> EvidenceStore:
        return self._store

    @property
    def blocks(self) -> Blocks:
        return self._blocks

    @property
    def names(self) -> Names | None:
        """The names a text may hold to pass the redaction pass, or None."""
        return self._names

    def incidents(self) -> list[Incident]:
        """Every incident whose calls are within `HORIZON`, newest first."""
        return list(self.snapshot().incidents)

    def get(self, incident_id: str) -> Incident | None:
        """The incident of that id, or None when the engine has none."""
        return self.snapshot().get(incident_id)

    @property
    def partial(self) -> bool:
        """Whether the latest reading left rows out (`Snapshot.partial`)."""
        return self.snapshot().partial

    def snapshot(self) -> Snapshot:
        """The incidents as of now, from one state of the store. The same reading
        is handed out again while the store has no new row and it is not yet
        `FRESH` old."""
        with self._working:
            # The time is read by the one that holds the lock, so readings are made
            # in the order of their times and a later one never has an earlier time.
            now = self._clock()
            last = self._store.last_id()
            made = self._made
            if (
                made is not None
                and made.last_id == last
                and timedelta(0) <= now - made.as_of < FRESH
            ):
                return made
            made = self._read(now, last, was_partial=made is not None and made.partial)
            self._made = made
            return made

    def _read(self, now: datetime, last: int, *, was_partial: bool) -> Snapshot:
        most = MAX_ROWS
        # One more than is used, which says whether there were more.
        rows = self._store.search(
            kinds=_KINDS,
            start=now - HORIZON,
            end=now,
            up_to_id=last,
            newest_first=True,
            limit=most + 1,
        )
        partial = len(rows) > most
        rows = rows[:most]
        # What ends an alert or a closure is a later row of the same source, and it
        # can be from before the horizon when the message it ends is found by a span
        # that began earlier still. So every alert and traffic row since the oldest
        # message found is read too, whatever its age.
        unresolved_before: datetime | None = None
        messages = [row for row in rows if row.kind in ("alert", "traffic")]
        earliest = min((row.occurred_at for row in messages), default=now)
        if earliest < now - HORIZON:
            later = self._store.search(
                kinds=("alert", "traffic"),
                start=earliest,
                end=now,
                up_to_id=last,
                newest_first=True,
                limit=most + 1,
            )
            if len(later) > most:
                # The oldest of them were left out, and one of those can be what
                # ended a message older than the oldest that was read: of such a
                # message it is not known whether it is in force.
                later = later[:most]
                unresolved_before = later[-1].occurred_at
                partial = True
            have = {row.id for row in rows}
            rows += [row for row in later if row.id not in have]
        overflowed = [False]

        def nearby(
            kind: Kind, point: Point, radius_m: float, start: datetime, end: datetime
        ) -> tuple[Sequence[Evidence], bool]:
            found = self._store.search(
                kinds=[kind],
                start=start,
                end=end,
                near=point,
                radius_m=radius_m,
                up_to_id=last,
                newest_first=True,
                limit=MAX_NEARBY + 1,
            )
            more = len(found) > MAX_NEARBY
            overflowed[0] = overflowed[0] or more
            return found[:MAX_NEARBY], more

        found = build_incidents(
            rows,
            blocks=self._blocks,
            names=self._names,
            now=now,
            nearby=nearby,
            unresolved_before=unresolved_before,
        )
        partial = partial or overflowed[0]
        if partial and not was_partial:
            logger.warning(
                "The incident engine found more rows than it reads at once and left "
                "some out: an incident can lack its first lines, count fewer taps than "
                "there were, or show an old alert as unknown."
            )
        return Snapshot(as_of=now, incidents=tuple(found), partial=partial, last_id=last)
