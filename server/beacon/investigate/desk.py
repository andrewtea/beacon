"""The investigation desk: which taps are looked into, when, and paid for once.

A tap comes from anybody, and looking into one costs money, so the desk bounds it:

- **A tap is investigated once.** Its id is claimed in the investigations file
  before Ember is asked anything (`Runs.claim`), and the claim is never given up: a
  run makes several paid calls, and after a failure it is seldom known that none
  was sent. A run that Ember cut short is kept as "unconfirmed" and is not made
  again.
- **Only a tap that just came.** A tap older than `STALE` when its turn comes is
  passed over. Claims are kept for a day (`store.KEPT`), and a tap's row for longer:
  a tap whose claim is gone is too old to be claimed again, so it is not paid for
  twice.
- **As the evidence stood when the tap came** (`Engine.at`), however long the tap
  waited: what began after it is not what it was about.
- **Under a budget of its own**: a live client pays under the `SpendScope` labelled
  `SCOPE`, which holds every tap's investigation together, and is refused without
  it. Each tap has a cap of its own as well (`loop.TAP_BUDGET_USD`).
- **A few at a time.** Taps wait in a queue of `QUEUE` places for one of `WORKERS`
  threads. A tap that finds the queue full is passed over, and its answer says it
  was not investigated: a crowd of taps is not a crowd of paid calls.
- What follows once Ember has been asked is `beacon.ingest.paid.PaidDesk`'s, as for
  the pollers that pay: trouble with Ember holds the desk for a while, and taps that
  come meanwhile are passed over; a replaced ledger stops it for the life of the
  process; shutting down admits nothing new and waits for the runs that are out.
  The hold is one for the whole desk (`_hold`), read and changed under a lock, so a
  run that ends well on one thread does not take off the hold a run that failed on
  another has just put on. A run that was already under way when the hold was put
  on is finished: it was claimed, and its tap is not looked into again.

Reading a tap's answer (`Runs.get`) never asks Ember anything.
"""

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from beacon.ember import EmberClient, EmberClosedError, InputTooLargeError
from beacon.incident import Engine
from beacon.ingest.paid import Hold, PaidDesk, read_hold
from beacon.ingest.runner import PollerHeldError
from beacon.investigate.loop import Outcome, Run, investigate_tap, never_sent
from beacon.investigate.store import KEPT, Runs, RunsError
from beacon.investigate.tools import Scene
from beacon.taps import Taps
from beacon.taps.desk import SOURCE as TAP_SOURCE

# The label of the spending scope investigations are paid under.
SCOPE = "investigate:tap"
# How many taps are looked into at once, and how many wait for that.
WORKERS = 2
QUEUE = 32
# How old a tap may be when its turn comes. One that is older is passed over.
STALE = timedelta(minutes=15)
# How long `stop` waits for a worker that is between two taps to end.
_JOIN_SECONDS = 5.0

logger = logging.getLogger("beacon.investigate")


class InvestigationDesk:
    """Looks into the taps of one evidence store, into one investigations file."""

    def __init__(
        self,
        ember: EmberClient,
        runs: Runs,
        engine: Engine,
        taps: Taps,
        *,
        workers: int = WORKERS,
        clock: Callable[[], datetime] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        scope: str = SCOPE,
    ) -> None:
        """`ember` is the desk's own client and is closed with it. A live one pays
        under the scope labelled `scope`: `SCOPE` on the live store, and a scope of
        its own when Muse Ember works on the invented story (`beacon.telling`), so
        that neither is counted against the other. `clock` is the time a run is
        stamped with, and `wall_clock` the time a hold is counted by. With
        `workers=0` nothing runs by itself, and `investigate` is called by hand."""
        self._paid = PaidDesk(
            ember, poller="investigator", items="taps", scope=scope, wall_clock=wall_clock
        )
        self._ember = ember
        self._runs = runs
        self._engine = engine
        self._taps = taps
        self._clock = (lambda: datetime.now(UTC)) if clock is None else clock
        # Set by `close`, before anything is waited for. A run reads it after it
        # has entered `working()`, so either `drain` waits for that run or the run
        # sees the desk closed and claims nothing.
        self._closing = threading.Event()
        # What the desk remembers about Ember between runs, which the runs of every
        # worker share. It is read from the file once, changed under the lock, and
        # saved with each run.
        self._holding = threading.Lock()
        self._hold = Hold(*read_hold(runs.state()))
        self._waiting: queue.Queue[str | None] = queue.Queue(maxsize=QUEUE)
        # The taps that are waiting for a worker, and the ones a run is being made
        # for. Whoever reads a tap's answer asks (`state_of`), so that a tap is
        # said to be pending for as long as it is, and no longer. A tap is taken
        # into the queue, and the queue is emptied at a stop, under this lock, so
        # that none is taken in after the stop has emptied it; and a tap has one
        # run under way at most, whose thread alone takes its mark off.
        self._tracking = threading.Lock()
        self._queued: set[str] = set()
        self._running: set[str] = set()
        self._threads = [
            threading.Thread(target=self._work, name=f"beacon-investigate-{n}", daemon=True)
            for n in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    @property
    def runs(self) -> Runs:
        return self._runs

    @property
    def active(self) -> bool:
        """Whether a tap that is handed in now will be looked into."""
        return not self._closing.is_set()

    @property
    def investigator(self) -> str:
        """What investigates for this desk: `live` for Muse Ember, `fake` for a
        stand-in. It says where a tap's clip or photo can be sent."""
        return self._ember.mode

    def state_of(self, tap_id: str) -> str | None:
        """`queued` for a tap that waits for a worker, `running` for one a run is
        being made for, and None for any other: its run is kept, or none is coming
        from this process."""
        with self._tracking:
            if tap_id in self._running:
                return "running"
            return "queued" if tap_id in self._queued else None

    def submit(self, tap_id: str) -> bool:
        """Hand a tap in to be looked into, and say whether it was taken. It does
        not wait for a thread of the desk's, and it does not raise: whoever took
        the tap still has a receipt to give. A tap that comes while the desk is
        closing is left alone. One that finds the queue full is passed over for
        good, which is written to the investigations file here, in one write that
        can wait for the file for a few seconds; when it cannot be written, the
        log says so, and the tap's answer says it was not investigated all the
        same, because it has no run and none is coming."""
        with self._tracking:
            # Looked at under the lock a stop empties the queue under: a tap that
            # is taken in is either emptied out by the stop or was never taken.
            if self._closing.is_set():
                return False
            if tap_id in self._queued or tap_id in self._running:
                # It is here already, and has one place.
                return True
            try:
                # It does not wait: the queue is full or it is not.
                self._waiting.put_nowait(tap_id)
            except queue.Full:
                full = True
            else:
                full = False
                self._queued.add(tap_id)
        if full:
            failed: str | None = None
            try:
                self._runs.skip(tap_id, self._clock())
            except RunsError:
                # The file is closed: the server is shutting down.
                pass
            except Exception as error:
                # The file is locked, or cannot be written. The tap was taken and
                # stored before it was handed in here, and its sender is still to
                # be given its id.
                failed = type(error).__name__
            if failed is not None:
                # Only the kind of error, and not the tap's id, which is what its
                # sender reads its answer with.
                logger.warning(
                    "A tap that found the queue full could not be recorded as passed "
                    "over (%s). Its answer says it was not investigated.",
                    failed,
                )
        return not full

    def investigate(self, tap_id: str) -> Run | None:
        """Look into one tap, now, and return its run. None when none was made: the
        tap has no row, it was investigated or passed over before, the desk is
        leaving Ember alone, or it is closed.

        Raises RunsError when the investigations file is closed."""
        with self._tracking:
            self._queued.discard(tap_id)
            if tap_id in self._running:
                # Another thread is making its run, and the mark is that thread's:
                # taken off here, the tap would be said to have no run coming
                # while its run is being made.
                return None
            self._running.add(tap_id)
        try:
            return self._investigate(tap_id)
        finally:
            # After the run's row is written, so a reader never finds neither.
            with self._tracking:
                self._running.discard(tap_id)

    def _investigate(self, tap_id: str) -> Run | None:
        row = self._taps.store.find(TAP_SOURCE, tap_id)
        if row is None or row.kind != "tap" or row.point is None:
            return None
        age = self._clock() - row.occurred_at
        if age > STALE:
            # Too old to be looked into, and maybe older than its claim was kept:
            # it is never claimed, so it is never paid for a second time.
            if age < KEPT:
                self._runs.skip(tap_id, self._clock())
            return None
        held = False
        with self._holding:
            try:
                self._paid.check(self._hold)
            except PollerHeldError:
                held = True
        if held:
            # Passed over, and told so: nothing is asked of Ember while it is in trouble.
            self._runs.skip(tap_id, self._clock())
            return None
        # From before the claim to the run's row, a shutdown waits (`drain`).
        with self._paid.working():
            if self._closing.is_set():
                # Closed, and maybe already drained: nothing is claimed, so nothing
                # is left behind that says a tap was looked into when it was not.
                return None
            if not self._runs.claim(tap_id, self._clock()):
                return None
            scene = Scene(
                tap_id=tap_id,
                at=row.occurred_at,
                point=row.point,
                has_photo=row.data.get("photo") is not None,
                has_clip=row.data.get("clip") is not None,
                # As things stood when the tap came, not when its turn did.
                snapshot=self._engine.at(row.occurred_at),
                store=self._taps.store,
                media=self._taps.media,  # type: ignore[arg-type]
            )
            made = investigate_tap(self._ember, scene, clock=self._clock)
            failure = made.failure
            run = made.run
            if failure is not None and not run.calls and never_sent(failure):
                # Ember was asked nothing, and nothing was paid: the tap was not
                # investigated, and is said not to have been. It stays claimed.
                run = replace(run, outcome=Outcome(status="unconfirmed", reason="not_investigated"))
            with self._holding:
                if failure is not None:
                    # A closing desk is no news about Ember, and neither is a
                    # request too large to send.
                    if not isinstance(failure, EmberClosedError | InputTooLargeError):
                        self._paid.hold_after(failure, self._hold)
                elif made.run.final is not None:
                    # Ember answered, and was paid: an answer that is no form
                    # counts toward leaving it alone.
                    self._paid.count_answer(self._hold, usable=made.usable)
                # The run and what the desk remembers are saved together, and in
                # the order the desk came to remember it.
                self._runs.write(run, state=self._hold.hold_state())
        if failure is not None and not isinstance(failure, EmberClosedError):
            # Only the kind of error: its text can repeat what was being read.
            logger.warning("An investigation was cut short (%s).", type(failure).__name__)
        return run

    def close(self) -> None:
        """Admit no more paid work, and claim no more: a tap that has not been
        claimed yet is left alone. A run whose paid request is out is still paid
        for; `drain` waits for it."""
        self._closing.set()
        self._paid.close()

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the runs that are being made to be
        stored, and say whether they have been."""
        return self._paid.drain(timeout)

    def stop(self, paid_timeout: float) -> bool:
        """Close the desk, wait for the runs that are out, end the workers, and
        close the investigations file. Says whether nothing paid is still out and
        every worker has ended. It blocks."""
        self.close()
        drained = self.drain(paid_timeout)
        # The taps still waiting are left alone: nothing was claimed for them.
        # Under the lock taps are taken in under, and after the desk was closed:
        # none is taken in once this is done.
        with self._tracking:
            with contextlib.suppress(queue.Empty):
                while True:
                    self._waiting.get_nowait()
            self._queued.clear()
        for _ in self._threads:
            with contextlib.suppress(queue.Full):
                self._waiting.put_nowait(None)
        ended = True
        for thread in self._threads:
            thread.join(_JOIN_SECONDS)
            ended = ended and not thread.is_alive()
        self._runs.close()
        return drained and ended

    def _work(self) -> None:
        """Look into the taps that are handed in, one after another, until the
        desk is closed."""
        while True:
            tap_id = self._waiting.get()
            if tap_id is None or self._closing.is_set():
                if tap_id is not None:
                    # Left alone: nothing was claimed for it.
                    with self._tracking:
                        self._queued.discard(tap_id)
                return
            failed: str | None = None
            try:
                self.investigate(tap_id)
            except Exception as error:
                if isinstance(error, RunsError) and self._closing.is_set():
                    # The file was closed under the run: the server is shutting down.
                    return
                failed = type(error).__name__
            finally:
                # Its turn has come and gone, however it ended: it waits no longer.
                with self._tracking:
                    self._queued.discard(tap_id)
            if failed is not None:
                logger.error("A tap could not be looked into (%s).", failed)
