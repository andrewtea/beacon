"""Runs the pollers: each one when it is due, and none of them takes the others down.

A poller is anything with a name, an interval, and a `poll` that writes what it finds
to the evidence store. The runner is one loop on one thread. Its clock can be held at
a moment, which is how the mock scenario is played (`beacon.ingest.setup`).
"""

import logging
import math
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from beacon.evidence import EvidenceStore, Origin
from beacon.ingest.http import SourceError

logger = logging.getLogger("beacon.ingest")

# The longest a poller is left alone, whatever wait a source asks for. A quota that
# is spent for the day asks for most of a day, and gets it; a week is more than any
# source means, and keeps the arithmetic on dates inside what a date can hold.
MAX_WAIT_SECONDS = 7 * 86_400.0


@dataclass(frozen=True)
class PollReport:
    # Rows that were new to the store.
    added: int
    # Items the source sent that Beacon could not read, and left out.
    unreadable: int = 0
    # Whether the source's answer was not read to its end, so that items it lists
    # can be missing from this poll.
    partial: bool = False
    # Set by a poller that asked its source nothing because it is not yet time to,
    # as one that keeps to a source's allowance does: the seconds until it is. The
    # runner then polls it again after that long and not after its interval, and
    # the poll changes nothing of what is known of the source, since nothing was
    # learned of it. None for a poll that asked.
    again_in: float | None = None


@dataclass(frozen=True)
class PollerStatus:
    name: str
    # When the poller last asked its source, and whether the source could be read.
    # Both are None for a poller that has not polled yet.
    polled_at: datetime | None
    ok: bool | None
    # Whether that poll read only part of what the source lists.
    partial: bool = False


class PollerHeldError(Exception):
    """A poller is leaving its source alone for a while, for a reason of its own,
    and did not ask it. The text is fixed. `retry_after` is the seconds until it
    will ask again, which the runner waits before it polls again."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@runtime_checkable
class PaidWork(Protocol):
    """What a poller that pays for its work offers, so that shutting down neither
    starts a paid request nor loses the answer to one that is out."""

    def close(self) -> None:
        """Admit no more paid work, and call off work that has cost nothing yet.
        Work that was admitted before goes on, and can send its paid request after
        this returns: only `drain` says when none is left."""
        ...

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for work whose paid request is out to be
        finished with, and say whether it has been: the request is answered, and
        what came of it is stored. The store is closed only after this."""
        ...


class Poller(Protocol):
    # Names the poller in the log and in the state it saves.
    name: str

    @property
    def origin(self) -> Origin:
        """Where its rows come from: `live` only for a poller that asks the real
        source. A poller reads it from its HTTP client and offers no way to set it."""
        ...

    # How long to wait after a poll before the next. It is read after each poll, so
    # a poller can ask for longer after a poll that cost more.
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
    def __init__(
        self,
        pollers: Sequence[Poller],
        store: EvidenceStore,
        clock: Clock,
        *,
        last: Iterable[PollerStatus] = (),
    ) -> None:
        """`last` is how the pollers' last polls went in an earlier process, for a
        runner that goes on where that one stopped and may have nothing left to poll.

        Raises EvidenceStoreError when `store` is the live one and a poller is fed
        by a stand-in."""
        names = [poller.name for poller in pollers]
        if len(set(names)) != len(names):
            raise ValueError("Two pollers have the same name.")
        for poller in pollers:
            store.refuse_stand_in(poller.origin)
        self._pollers = list(pollers)
        self._store = store
        self._clock = clock
        # When each poller is next due. A poller that has never run is due at once.
        self._due: dict[str, datetime] = {}
        self._last = {status.name: status for status in last if status.name in names}

    def status(self) -> list[PollerStatus]:
        """How each poller's last poll went, in the order the pollers were given."""
        last = dict(self._last)
        return [
            last.get(poller.name, PollerStatus(poller.name, None, None)) for poller in self._pollers
        ]

    def step(
        self, stop: threading.Event | None = None, *, failed_stay_due: bool = False
    ) -> dict[str, PollReport | Exception]:
        """Poll every poller that is due now, and return what each one did. No
        further poller is begun once `stop` is set.

        A poller that fails is tried again after its interval, or when the wait the
        source asked for is over, if that is later. That wait is counted from when
        the source answered, which for a poll of several requests can be well after
        the step began. Its failure is logged by its kind, and the other pollers run.

        With `failed_stay_due`, a poller that fails stays due at this same moment.
        The mock scenario is played that way: a step that failed is played again,
        and only the pollers that failed in it poll again."""
        now = self._clock.now()
        results: dict[str, PollReport | Exception] = {}
        for poller in self._pollers:
            if stop is not None and stop.is_set():
                break
            due = self._due.get(poller.name)
            if due is not None and now < due:
                continue
            asked: object = None
            try:
                report = poller.poll(self._store, now)
            except Exception as error:
                results[poller.name] = error
                if stop is not None and stop.is_set():
                    # Stopping is what cut the poll short. That is no news about the
                    # source, and the poll is made again when Beacon next runs.
                    break
                if isinstance(error, SourceError | PollerHeldError):
                    # Both have a fixed text, and can say how long to wait.
                    logger.warning("%s", error)
                    asked = error.retry_after
                else:
                    # Only the kind of error is logged. Its text and its traceback
                    # can repeat what the poller was reading, which for radio is
                    # the traffic.
                    logger.error("The %s poller failed (%s).", poller.name, type(error).__name__)
            else:
                if report.again_in is not None:
                    # Nothing was asked, so there is nothing to report or to log, and
                    # the poller's last poll stays its last. The clock is read again:
                    # the poller counted from when it looked.
                    results[poller.name] = report
                    self._due[poller.name] = self._clock.now() + timedelta(
                        seconds=_seconds(report.again_in) or 1.0
                    )
                    continue
                if report.unreadable:
                    logger.warning(
                        "%s sent %d items Beacon could not read.", poller.name, report.unreadable
                    )
                before = self._last.get(poller.name)
                # Said when it begins, and not at every poll of a source whose
                # polls are partial for a long while, as real radio's are.
                if report.partial and not (before is not None and before.partial):
                    logger.warning(
                        "%s's answer was not read to its end, so items it lists may be missing.",
                        poller.name,
                    )
                results[poller.name] = report
            outcome = results[poller.name]
            ok = isinstance(outcome, PollReport)
            if ok or not failed_stay_due:
                self._due[poller.name] = max(
                    now + timedelta(seconds=_seconds(poller.interval_seconds) or 1.0),
                    # The clock, read again: the source asked when it answered.
                    self._clock.now() + timedelta(seconds=_seconds(asked)),
                )
            self._last[poller.name] = PollerStatus(
                poller.name, now, ok, isinstance(outcome, PollReport) and outcome.partial
            )
        return results

    def run(self, stop: threading.Event, *, sleep: Callable[[float], object] | None = None) -> None:
        """Step until `stop` is set, sleeping until the next poller is due."""
        wait = stop.wait if sleep is None else sleep
        while not stop.is_set():
            self.step(stop)
            now = self._clock.now()
            soonest = min(self._due.values(), default=now)
            wait(max(1.0, (soonest - now).total_seconds()))


def _seconds(wait: object) -> float:
    """A wait in seconds, from a poller or a source, and never more than
    `MAX_WAIT_SECONDS`. One that is not a usable number counts as none."""
    if isinstance(wait, bool) or not isinstance(wait, int | float):
        return 0.0
    if not math.isfinite(wait) or wait <= 0:
        return 0.0
    return min(float(wait), MAX_WAIT_SECONDS)
