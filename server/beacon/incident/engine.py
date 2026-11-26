"""The incident engine: the evidence store read as the incidents going on now.

    engine = open_engine(os.environ, store)
    engine.incidents()                 # every incident of the last twelve hours
    engine.get("inc-3f2a9c1d5b7e0a64")

Nothing the engine works out is stored. An incident is a function of the evidence
rows (`beacon.incident.build`), so there is no second record that could disagree with
the first, and a store that is deleted and begun again takes its incidents with it.
What is worked out is kept in memory for as long as the store has no new row, and
for a few seconds at most, because what is in force also changes with the time.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from beacon.evidence import Evidence, EvidenceStore, Kind, Mode, Point
from beacon.incident.build import build_incidents
from beacon.incident.model import Incident
from beacon.incident.places import Blocks

# How far back the engine reads. An incident older than this is gone from it, and one
# that has gone on for longer loses its first calls, and with them its id. An incident
# counts at most `MAX_NEARBY` taps.
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


class Engine:
    """Reads one evidence store as incidents. It is safe to share between threads."""

    def __init__(
        self,
        store: EvidenceStore,
        blocks: Blocks,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._blocks = blocks
        self._clock = (lambda: datetime.now(UTC)) if clock is None else clock
        self._working = threading.Lock()
        self._made: tuple[int, datetime, list[Incident], bool] | None = None

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

    def now(self) -> datetime:
        return self._clock()

    def incidents(self) -> list[Incident]:
        """Every incident whose calls are within `HORIZON`, newest first."""
        return list(self._read()[2])

    @property
    def partial(self) -> bool:
        """Whether the last reading left rows out because there were more than
        `MAX_ROWS` of them: the oldest, so an incident can lack its first lines."""
        return self._read()[3]

    def _read(self) -> tuple[int, datetime, list[Incident], bool]:
        now = self._clock()
        with self._working:
            last = self._store.last_id()
            made = self._made
            if made is not None and made[0] == last and timedelta(0) <= now - made[1] < FRESH:
                return made
            rows = self._store.search(
                kinds=_KINDS, start=now - HORIZON, end=now, newest_first=True, limit=MAX_ROWS
            )
            partial = len(rows) >= MAX_ROWS
            # What ends an alert or a closure is a later row of the same source,
            # and it can be from before the horizon when the message it ends is
            # found by a span that began earlier still. So every alert and traffic
            # row since the oldest message found is read too, whatever its age.
            messages = [row for row in rows if row.kind in ("alert", "traffic")]
            earliest = min((row.occurred_at for row in messages), default=now)
            if earliest < now - HORIZON:
                later = self._store.search(
                    kinds=("alert", "traffic"),
                    start=earliest,
                    end=now,
                    newest_first=True,
                    limit=MAX_ROWS,
                )
                partial = partial or len(later) >= MAX_ROWS
                have = {row.id for row in rows}
                rows += [row for row in later if row.id not in have]
            if partial and not (made is not None and made[3]):
                logger.warning(
                    "The incident engine read the newest %d rows and left older ones "
                    "out, so an incident can lack its first lines.",
                    MAX_ROWS,
                )
            found = build_incidents(rows, blocks=self._blocks, now=now, nearby=self._nearby)
            self._made = (last, now, found, partial)
            return self._made

    def get(self, incident_id: str) -> Incident | None:
        """The incident of that id, or None when the engine has none."""
        for incident in self.incidents():
            if incident.id == incident_id:
                return incident
        return None

    def _nearby(
        self, kind: Kind, point: Point, radius_m: float, start: datetime, end: datetime
    ) -> Sequence[Evidence]:
        return self._store.search(
            kinds=[kind],
            start=start,
            end=end,
            near=point,
            radius_m=radius_m,
            newest_first=True,
            limit=MAX_NEARBY,
        )
