"""Replayed taps: what an investigation of a tap costs, asked many times over.

A replayed tap is a question the scale run puts to the investigator
(`beacon.investigate.investigate_tap`): a place and a moment of the run's day, read
against the run's evidence as it stood at that moment. **It is not stored as a
tap.** Nothing in the run's evidence store says a person tapped: a tap row is what a
phone sent, and these come from a script. So a replayed tap adds nothing to an
incident's count of taps, and one replayed tap is never evidence for another.

Two kinds are planned from DataSF's records (`plan`), by rule and with no chance in
it, so the same records give the same taps:

- **near a call**: at DataSF's point for a call that is no medical one, rounded to
  about a block as the app rounds a tap, `AFTER_DISPATCH` after the call was
  dispatched. A medical call gets none: the investigator is told only that a
  medical response is near, and matches no tap to one.
- **away from every call DataSF records there then**: at a call's point, at a
  moment that, for every call DataSF lists within `CLEAR_M` of it, is more than
  `CLEAR_FOR` before the call was received or more than `CLEAR_FOR` after the last
  time DataSF records for it: its dispatch, a unit on scene, or its last unit
  available (`_last_recorded`). That is what the record says and no more: a call
  whose record ends at its dispatch can have gone on long after, and a call DataSF
  does not list is not known of at all.

**A replayed tap is investigated once, over every invocation.** It is claimed in the
run's own investigations file before Ember is asked and the claim is never given up,
as the server's desk does, so a run that is stopped goes on with the taps nobody
claimed. A tap is named by what it asks (`_replay`), so a run given again with
another `--taps` investigates only the taps that are new to it; an investigation
the plan no longer holds stays in the file, paid for, and is not counted. Each
investigation is held to the tap's own cap by the Ember client
(`TAP_BUDGET_NANOUSD`, which `investigate_tap` passes with every call), and a paid
replay pays under the scope `scale:taps`; a tap is not begun, and so not claimed,
when that scope has less left than one tap may cost.

**When Ember refuses or fails, the replay stops there and leaves Ember alone**, as
the server's desk does and by the same code (`beacon.ingest.paid.PaidDesk`): for ten
minutes, or as long as Ember asked when that is longer, counted from the failure and
kept in the investigations file, so that the command given again sooner asks
nothing. Paid answers that are no form, three in a row, hold it the same way. A
claim is stamped with one fixed moment (`_CLAIMED_AT`), because the investigations
file deletes claims a day old by the stamp of a new one, and a run's claims are its
account of what it paid for.
"""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from beacon.ember import (
    BudgetExceededError,
    EmberClient,
    EmberClosedError,
    EmberError,
    InputTooLargeError,
    LedgerReplacedError,
)
from beacon.evidence import EvidenceStore, Point
from beacon.incident import Engine
from beacon.incident.places import Blocks
from beacon.ingest.paid import Hold, PaidDesk, read_hold
from beacon.ingest.runner import PollerHeldError
from beacon.investigate import TAP_BUDGET_NANOUSD, Run, Runs, RunsError, investigate_tap
from beacon.investigate.loop import Outcome, never_sent
from beacon.investigate.tools import Scene
from beacon.scale.datasf import Record
from beacon.scale.run import TAPS_SCOPE

AFTER_DISPATCH = timedelta(minutes=3)
CLEAR_M = 1000.0
CLEAR_FOR = timedelta(minutes=60)
# One tap in this many is placed away from every call.
AWAY_EVERY = 5
MAX_TAPS = 300
# Why a replay stopped, from a closed list: it is leaving Ember alone after a
# refusal or a failure (`held`); paid answers that were no form ran on (`unusable`);
# its scope has less left than one tap may cost, or the spending stop refused
# (`budget`); the Ember ledger was replaced under it (`ledger_replaced`); Ember
# failed or was closed (`ember`); the investigations file could not be written
# (`store`).
Stopped = Literal["held", "unusable", "budget", "ledger_replaced", "ember", "store"]
# The moment every claim of a replay is stamped with. The investigations file
# deletes what was claimed more than a day before the moment a new claim is stamped
# with, which is right for a person's tap and wrong for a run's account of what it
# paid for: a run that is gone on with days later would find its claims gone and pay
# again. With one stamp for all, none is ever older than another, and none is deleted.
_CLAIMED_AT = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Replay:
    """One replayed tap: where and when, and what it was placed by."""

    # Its name in the investigations file: a digest of the three fields below.
    id: str
    at: datetime
    point: Point
    # The DataSF call it was placed at, or None for one placed away from every call.
    near: str | None


@dataclass(frozen=True)
class Replayed:
    """What one invocation's replay came to."""

    # Investigations made in this invocation.
    made: int
    # Why the replay stopped before every tap had been claimed, or None when it
    # did not stop.
    stopped: "Stopped | None"


def plan(records: Sequence[Record], count: int) -> list[Replay]:
    """Up to `count` replayed taps for a day's records, each a different question,
    spread evenly over the day's calls in the order they were dispatched, from the
    first to the last, one in `AWAY_EVERY` of them away from every call."""
    count = max(0, min(count, MAX_TAPS))
    placed = sorted(
        (record for record in records if not record.medical and record.point is not None),
        key=lambda record: (record.dispatched_at, record.number),
    )
    if not placed or not count:
        return []
    last = len(placed) - 1
    if count >= len(placed):
        chosen = placed
    elif count == 1:
        # One tap is at the middle of the day's calls.
        chosen = [placed[last // 2]]
    else:
        # The first call, the last, and the ones at even steps between them, each
        # step rounded to the nearest call: with fewer taps than calls a step is
        # more than one call, so no call is chosen twice.
        between = count - 1
        chosen = [placed[(2 * index * last + between) // (2 * between)] for index in range(count)]
    replays: list[Replay] = []
    # A tap is named by what it asks, and one question is put once: two controls
    # of one moment can find the same place away from every call, and are then one
    # tap, so that a plan can hold fewer taps than were asked for and never the
    # same one twice.
    named: set[str] = set()
    for index, record in enumerate(chosen):
        assert record.point is not None
        at = record.dispatched_at + AFTER_DISPATCH
        near: str | None = record.number
        point = record.point
        if index % AWAY_EVERY == AWAY_EVERY - 1:
            away = _away_from_all(records, placed, at, index)
            if away is None:
                continue
            point, near = away, None
        tap = _replay(at, _rounded(point), near)
        if tap.id not in named:
            named.add(tap.id)
            replays.append(tap)
    return replays


def _replay(at: datetime, point: Point, near: str | None) -> Replay:
    """A replayed tap, named by what it asks: its moment, its place, and what it was
    placed by. A claim is kept under the name for good, so the name of one question
    is never another's: a plan of another size gives a tap it shares with this one
    the same name, and every other tap a new one."""
    asked = f"{at.isoformat()}|{point.lat}|{point.lon}|{near or 'away'}"
    return Replay(
        id=f"replay-{hashlib.sha256(asked.encode()).hexdigest()[:16]}",
        at=at,
        point=point,
        near=near,
    )


def replay(
    replays: Sequence[Replay],
    ember: EmberClient,
    store: EvidenceStore,
    blocks: Blocks,
    runs: Runs,
    *,
    wall_clock: Callable[[], datetime] | None = None,
) -> Replayed:
    """Investigate every replayed tap that nobody has claimed, until something
    stops it. `ember` is closed when it returns. `wall_clock` is the time a hold is
    counted by: the real time, unless a test moves it."""
    engine = Engine(store, blocks)
    made = 0
    stopped: Stopped | None = None
    try:
        # What every desk that pays Ember keeps to (`beacon.ingest.paid`): the hold
        # after a refusal or a failure, kept in the investigations file so that the
        # command given again does not shorten it, and the count of paid answers
        # that were no form.
        desk = PaidDesk(
            ember, poller="replay", items="taps", scope=TAPS_SCOPE, wall_clock=wall_clock
        )
        hold = Hold(*read_hold(runs.state()))
        for tap in replays:
            try:
                if runs.get(tap.id) is not None:
                    # Claimed before, by this run or one that was stopped: it is not
                    # paid for a second time, whether or not its run was kept.
                    continue
                try:
                    desk.check(hold)
                except PollerHeldError:
                    stopped = "held"
                    break
                if _short(ember):
                    # Not begun, and so not claimed: an investigation that its
                    # scope could cut short part of the way is not started.
                    stopped = "budget"
                    break
                if not runs.claim(tap.id, _CLAIMED_AT):
                    continue
                scene = Scene(
                    tap_id=tap.id,
                    at=tap.at,
                    point=tap.point,
                    has_photo=False,
                    has_clip=False,
                    # As things stood at the tap's own moment.
                    snapshot=engine.at(tap.at),
                    store=store,
                    media=_no_media,
                )
                investigation = investigate_tap(ember, scene)
                run, failure = investigation.run, investigation.failure
                if failure is not None and not run.calls and never_sent(failure):
                    # Ember was asked nothing and nothing was paid: the tap was not
                    # investigated, and is said not to have been. It stays claimed.
                    run = replace(
                        run, outcome=Outcome(status="unconfirmed", reason="not_investigated")
                    )
                ended: Stopped | None = None
                if failure is not None:
                    ended = _ended_by(failure)
                    if not isinstance(failure, EmberClosedError | InputTooLargeError):
                        desk.hold_after(failure, hold)
                    elif isinstance(failure, InputTooLargeError):
                        # A request too large to send is no news about Ember.
                        ended = None
                elif run.final is not None and (
                    desk.count_answer(hold, usable=investigation.usable) is not None
                ):
                    ended = "unusable"
                # The run and the hold together, as the server's desk writes them.
                runs.write(run, state=hold.hold_state())
            except RunsError:
                stopped = "store"
                break
            made += 1
            if ended is not None:
                # Nothing more is asked of Ember in this invocation, or in the next
                # one before the hold is over.
                stopped = ended
                break
    finally:
        ember.close()
    return Replayed(made=made, stopped=stopped)


def held_for(runs: Runs, wall_clock: Callable[[], datetime] | None = None) -> float | None:
    """How many seconds the replay's hold on Ember still has to run, or None when
    none is on. The hold is the run's, not the replay's alone: the command asks
    before it has anything heard as well, so that a failure on a tap is not followed
    by a paid request for a call."""
    held_until, _ = read_hold(runs.state())
    if held_until is None:
        return None
    now = datetime.now(UTC) if wall_clock is None else wall_clock()
    left = (held_until - now).total_seconds()
    return left if left > 0 else None


def _ended_by(failure: EmberError) -> Stopped:
    if isinstance(failure, LedgerReplacedError):
        return "ledger_replaced"
    if isinstance(failure, BudgetExceededError):
        return "budget"
    return "ember"


def _short(ember: EmberClient) -> bool:
    """Whether a paying client's scope has less left than one tap may cost."""
    scope = ember.scope
    if ember.mode != "live" or scope is None:
        return False
    left = scope.budget_nanousd - ember.scope_spend(scope.label).spent_nanousd
    return left < TAP_BUDGET_NANOUSD


Standing = Literal["made", "claimed", "waiting"]


def kept(replays: Sequence[Replay], runs: Runs) -> list[tuple[Replay, Standing, Run | None]]:
    """How each replayed tap stands in the investigations file: its run is `made`,
    it was `claimed` and no run was kept (the process died meanwhile), or it is
    still `waiting` for a replay."""
    standing: list[tuple[Replay, Standing, Run | None]] = []
    for tap in replays:
        found = runs.get(tap.id)
        if found is None:
            standing.append((tap, "waiting", None))
        elif found[1] is None:
            standing.append((tap, "claimed", None))
        else:
            standing.append((tap, "made", found[1]))
    return standing


def _away_from_all(
    records: Sequence[Record], placed: Sequence[Record], at: datetime, index: int
) -> Point | None:
    """A call's point, rounded as a tap is, such that for every call of the day
    within `CLEAR_M` of it, `at` is more than `CLEAR_FOR` before the call was
    received or more than `CLEAR_FOR` after the last time DataSF records for it,
    looked for from a place in the list that depends on `index` alone."""
    for offset in range(len(placed)):
        given = placed[(index * 7 + offset) % len(placed)].point
        assert given is not None
        # The place the tap will be at, which rounding can move by tens of metres.
        point = _rounded(given)
        if not any(
            other.point is not None
            and other.point.distance_m(point) <= CLEAR_M
            and other.received_at - CLEAR_FOR <= at <= _last_recorded(other) + CLEAR_FOR
            for other in records
        ):
            return point
    return None


def _last_recorded(record: Record) -> datetime:
    """The last time DataSF records for a call: the latest of its dispatch, a unit
    on scene, and its last unit available, of those the record has. It is not when
    the call ended, which a record that lacks the later times does not say."""
    later = (record.on_scene_at, record.cleared_at)
    return max(
        [record.dispatched_at, *(at for at in later if at is not None)],
    )


def _rounded(point: Point) -> Point:
    """To three decimals of a degree, about a block, as the app rounds a tap."""
    return Point(round(point.lat, 3), round(point.lon, 3))


def _no_media(tap_id: str, kind: str) -> bytes | None:
    return None
