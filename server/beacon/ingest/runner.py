"""Runs the pollers: each one when it is due, and none of them takes the others down.

A poller is anything with a name, an interval, and a `poll` that writes what it finds
to the evidence store. The runner is one loop on one thread. It can also be stepped
through a span of time that has already passed, which is how the mock scenario is
caught up to the present and how tests play it.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from beacon.evidence import EvidenceStore
from beacon.ingest.http import SourceError

logger = logging.getLogger("beacon.ingest")

# How far apart the moments of a caught-up span are. It is the shortest interval a
# poller has, so catching up polls as often as running live would have.
CATCH_UP_STEP = timedelta(seconds=10)


@dataclass(frozen=True)
class PollReport:
    # Rows that were new to the store.
    added: int
    # Items the source sent that Beacon could not read, and left out.
    unreadable: int = 0


@dataclass(frozen=True)
class PollerStatus:
    name: str
    # When the poller last asked its source, and whether the source could be read.
    # Both are None for a poller that has not polled yet.
    polled_at: datetime | None
    ok: bool | None


class Poller(Protocol):
    # Names the poller in the log and in the state it saves.
    name: str
    interval_seconds: float

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        """Ask the source once and store what is new. Raises SourceError when the
        source cannot be read."""
        ...


class Clock:
    """The time, which the runner can hold at a moment while it catches up. The mock
    scenario reads the same clock, so what it serves is what was true at that moment.
    `now` replaces the system's time, which is how tests set it."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = (lambda: datetime.now(UTC)) if now is None else now
        self._held: datetime | None = None

    def now(self) -> datetime:
        return self._now() if self._held is None else self._held

    def hold(self, moment: datetime | None) -> None:
        self._held = moment


class Runner:
    def __init__(self, pollers: Sequence[Poller], store: EvidenceStore, clock: Clock) -> None:
        names = [poller.name for poller in pollers]
        if len(set(names)) != len(names):
            raise ValueError("Two pollers have the same name.")
        self._pollers = list(pollers)
        self._store = store
        self._clock = clock
        # When each poller is next due. A poller that has never run is due at once.
        self._due: dict[str, datetime] = {}
        self._last: dict[str, PollerStatus] = {}

    def status(self) -> list[PollerStatus]:
        """How each poller's last poll went, in the order the pollers were given."""
        last = dict(self._last)
        return [
            last.get(poller.name, PollerStatus(poller.name, None, None)) for poller in self._pollers
        ]

    def step(self) -> dict[str, PollReport | Exception]:
        """Poll every poller that is due now, and return what each one did.

        A poller that fails is tried again after its interval, or after the wait the
        source asked for when that is longer. Its failure is logged by its kind, and
        the other pollers run."""
        now = self._clock.now()
        results: dict[str, PollReport | Exception] = {}
        for poller in self._pollers:
            due = self._due.get(poller.name)
            if due is not None and now < due:
                continue
            wait = poller.interval_seconds
            try:
                report = poller.poll(self._store, now)
            except SourceError as error:
                # A source's error has a fixed text.
                logger.warning("%s", error)
                wait = max(wait, error.retry_after or 0.0)
                results[poller.name] = error
            except Exception as error:
                # Only the kind of error is logged. Its text and its traceback can
                # repeat what the poller was reading, which for radio is the traffic.
                logger.error("The %s poller failed (%s).", poller.name, type(error).__name__)
                results[poller.name] = error
            else:
                if report.unreadable:
                    logger.warning(
                        "%s sent %d items Beacon could not read.", poller.name, report.unreadable
                    )
                results[poller.name] = report
            self._due[poller.name] = now + timedelta(seconds=wait)
            self._last[poller.name] = PollerStatus(
                poller.name, now, isinstance(results[poller.name], PollReport)
            )
        return results

    def catch_up(self, start: datetime, end: datetime, stop: threading.Event | None = None) -> None:
        """Step through the span from `start` to `end`, holding the clock at each
        moment, as if the runner had been running all along. It leaves off early
        when `stop` is set."""
        moment = start
        try:
            while moment <= end and not (stop is not None and stop.is_set()):
                self._clock.hold(moment)
                self.step()
                moment += CATCH_UP_STEP
        finally:
            self._clock.hold(None)

    def run(self, stop: threading.Event, *, sleep: Callable[[float], object] | None = None) -> None:
        """Step until `stop` is set, sleeping until the next poller is due."""
        wait = stop.wait if sleep is None else sleep
        while not stop.is_set():
            self.step()
            now = self._clock.now()
            soonest = min(self._due.values(), default=now)
            wait(max(1.0, (soonest - now).total_seconds()))
