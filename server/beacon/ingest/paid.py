"""What the pollers that pay Ember for each item share: radio, which pays to have a
call heard, and notices, which pay to have a document read.

An item is claimed in the evidence store before it is paid for
(`EvidenceStore.claim`), which is the poller's to do. What is kept here is what
follows once Ember has been asked, and it is the same for every such poller, so that
the rules about money are written once:

- **A claim is given up only when the paid request is known not to have been sent**
  (`PaidDesk.not_sent`): a spending stop, a client that only replays, a client that
  was closed. After anything else the item may have been paid for, and is finished
  with.
- **Trouble with Ember stops the poller, not the server.** After Ember refused an
  item or failed on one, the poller asks nothing for `HELD_INTERVAL_SECONDS`
  (`hold_after`), counted from the failure by the real clock and kept in the
  poller's state (`Hold`), so that a restart does not shorten it. The same happens
  after `UNUSABLE_RUN_LIMIT` answers in a row that were no form, each of which was
  paid for (`count_answer`); after that hold one more such answer holds it again.
- **A replaced ledger stops the poller for the life of the process.** What was
  spent is then not known from the ledger, and an operator has to compare Meta's
  Usage page with it first.
- **A poller that can spend has a budget of its own.** A live client is refused
  unless it pays under the poller's own `SpendScope`, by its label: not under
  none, and not under another poller's, whose budget it would then spend.
- **Shutting down admits nothing new and loses no answer.** `close` closes the
  poller's Ember client. Work under a claim is counted (`working`), and `drain`
  waits until its row is stored.
"""

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from beacon.ember import (
    BudgetExceededError,
    EmberCallError,
    EmberClient,
    EmberClosedError,
    EmberError,
    LedgerReplacedError,
    ReplayMissError,
)
from beacon.errors import raise_bare
from beacon.ingest.runner import MAX_WAIT_SECONDS, PollerHeldError

# How long a poller leaves Ember alone after Ember refused an item or failed on one.
HELD_INTERVAL_SECONDS = 600.0
# This many answers in a row that were no form, and the poller is held as well.
UNUSABLE_RUN_LIMIT = 3

# The refusals after which the request that costs money is known not to have been
# sent: a spending stop, a client that only replays, and a client that was closed.
# A replaced ledger is a spending stop by its class and is not among them: it is
# also raised after a request that was sent could not be settled.
_NOT_SENT = (BudgetExceededError, ReplayMissError, EmberClosedError)

logger = logging.getLogger("beacon.ingest")


@dataclass
class Hold:
    """What a paying poller remembers about Ember between polls, as part of the
    state it keeps in the evidence store."""

    # Until when it leaves Ember alone, by the wall clock.
    held_until: datetime | None = None
    # How many of Ember's last answers in a row were no form.
    unusable_run: int = 0

    def hold_state(self) -> dict[str, Any]:
        return {
            "held_until": None if self.held_until is None else self.held_until.isoformat(),
            "unusable_run": self.unusable_run,
        }


def read_hold(saved: Mapping[str, Any]) -> tuple[datetime | None, int]:
    """The `held_until` and `unusable_run` a poller saved, or none and nothing for
    what is not there or is not what `Hold.hold_state` writes."""
    held, run = saved.get("held_until"), saved.get("unusable_run")
    held_until = None
    if isinstance(held, str):
        with contextlib.suppress(ValueError):
            held_until = datetime.fromisoformat(held)
        if held_until is not None and held_until.utcoffset() is None:
            held_until = None
    usable_run = isinstance(run, int) and not isinstance(run, bool)
    return held_until, run if usable_run and 0 <= run <= UNUSABLE_RUN_LIMIT else 0


class PaidDesk:
    """One poller's dealings with the Ember client it pays through."""

    def __init__(
        self,
        ember: EmberClient,
        *,
        poller: str,
        items: str,
        scope: str,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`poller` and `items` are what the poller and what it pays for are called
        in the sentences an operator reads: `radio` and `calls`. `wall_clock` is
        the time a hold is counted by, which is the real time even while the mock
        scenario is played on a clock of its own.

        `ember` is closed with the desk (`close`), so it is a client of the
        poller's own. One that can spend has a budget of its own as well, under
        the spending stop: the `SpendScope` labelled `scope`, which is this
        poller's and no other's. What a poller may pay for over every run is a
        number an operator set for it, and never the whole stop or the number set
        for another poller."""
        if ember.mode == "live" and (ember.scope is None or ember.scope.label != scope):
            raise ValueError(
                f"The {poller} poller pays under a budget of its own: its Ember client "
                f"needs the spending scope {scope}."
            )
        self._ember = ember
        self._wall_clock = (lambda: datetime.now(UTC)) if wall_clock is None else wall_clock
        self._held = (
            f"The {poller} poller is leaving Ember alone for a while, after Ember refused "
            "or failed."
        )
        self._unusable = (
            f"The {poller} poller is leaving Ember alone for a while: its answers about "
            f"{items} were not filled-in forms, several times in a row."
        )
        self._lost = (
            f"The {poller} poller has stopped: the Ember ledger was replaced while it ran, "
            "so what was spent is not known from it. Compare Meta's Usage page with the "
            "ledger, then start Beacon again."
        )
        # Set when the Ember ledger was replaced under this process. Nothing is
        # paid for after that, until an operator has looked and started Beacon again.
        self._ledger_lost = False
        # How many claimed items are being worked on, which is one or none: from
        # the claim until the item's row is stored or the item is given up.
        self._working = threading.Condition()
        self._claimed = 0

    def close(self) -> None:
        """Admit no more paid work. An item the Ember client had already admitted
        still sends its paid request (`EmberClient.close`); `drain` waits for it."""
        self._ember.close()

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the item that is being worked on under
        a claim to be finished with, and say whether it has been: Ember has
        answered, and the row is stored. A shutdown waits for this before it closes
        the store, so that an answer that was paid for is not thrown away."""
        with self._working:
            return self._working.wait_for(lambda: self._claimed == 0, timeout)

    @contextlib.contextmanager
    def working(self) -> Iterator[None]:
        """Around the work on an item that is claimed, which `drain` waits for."""
        with self._working:
            self._claimed += 1
        try:
            yield
        finally:
            with self._working:
                self._claimed -= 1
                self._working.notify_all()

    def check(self, hold: Hold) -> None:
        """Raises PollerHeldError when the poller is to ask Ember nothing just now:
        the ledger was replaced, or a hold is not over. A hold that is over is
        taken off."""
        if self._ledger_lost:
            raise_bare(PollerHeldError(self._lost, MAX_WAIT_SECONDS))
        if hold.held_until is not None:
            left = (hold.held_until - self._wall_clock()).total_seconds()
            if left > 0:
                raise_bare(PollerHeldError(self._held, left))
            hold.held_until = None

    @staticmethod
    def not_sent(failure: EmberError) -> bool:
        """Whether the request that costs money is known not to have been sent, so
        that the claim on its item can be given up."""
        return isinstance(failure, _NOT_SENT) and not isinstance(failure, LedgerReplacedError)

    def hold_after(self, failure: EmberError, hold: Hold) -> None:
        """Hold the poller after Ember refused an item or failed on one: for
        `HELD_INTERVAL_SECONDS`, or for as long as Ember asked when that is longer.
        A replaced ledger also stops it for the life of the process. The caller
        saves `hold`, and raises the failure."""
        if isinstance(failure, LedgerReplacedError):
            self._ledger_lost = True
            logger.error("%s", self._lost)
        asked = failure.retry_after if isinstance(failure, EmberCallError) else None
        seconds = max(HELD_INTERVAL_SECONDS, min(asked or 0.0, MAX_WAIT_SECONDS))
        # Counted from now, when Ember failed, and kept: a restart does not end it.
        hold.held_until = self._wall_clock() + timedelta(seconds=seconds)

    def count_answer(self, hold: Hold, *, usable: bool) -> PollerHeldError | None:
        """Count an answer Ember gave, and was paid for, in `hold`.

        Returns None, or, when answers that are no form have run on, the error to
        end the poll with: the poller is then held, and the caller saves `hold`
        before it raises the error. After the hold, one more answer of the kind
        holds the poller again."""
        if usable:
            hold.unusable_run = 0
            return None
        hold.unusable_run += 1
        if hold.unusable_run < UNUSABLE_RUN_LIMIT:
            return None
        hold.unusable_run = UNUSABLE_RUN_LIMIT - 1
        hold.held_until = self._wall_clock() + timedelta(seconds=HELD_INTERVAL_SECONDS)
        return PollerHeldError(self._unusable, HELD_INTERVAL_SECONDS)
