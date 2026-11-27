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
- **away from every call**: at a call's point, at a moment when DataSF lists no
  call within `CLEAR_M` of it for `CLEAR_FOR` either side.

**A replayed tap is investigated once, over every invocation.** It is claimed in the
run's own investigations file before Ember is asked and the claim is never given up,
as the server's desk does, so a run that is stopped goes on with the taps nobody
claimed. A tap is named by what it asks (`_replay`), so a run given again with
another `--taps` investigates only the taps that are new to it; an investigation
the plan no longer holds stays in the file, paid for, and is not counted. Each
investigation is held to the tap's own cap by the Ember client
(`TAP_BUDGET_NANOUSD`, which `investigate_tap` passes with every call), and a paid
replay pays under the scope `scale:taps`. When Ember refuses or fails, the replay
stops there: nothing more is asked in this invocation.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from beacon.ember import EmberClient, InputTooLargeError
from beacon.evidence import EvidenceStore, Point
from beacon.incident import Engine
from beacon.incident.places import Blocks
from beacon.investigate import Run, Runs, RunsError, investigate_tap
from beacon.investigate.tools import Scene
from beacon.scale.datasf import Record

AFTER_DISPATCH = timedelta(minutes=3)
CLEAR_M = 1000.0
CLEAR_FOR = timedelta(minutes=60)
# One tap in this many is placed away from every call.
AWAY_EVERY = 5
MAX_TAPS = 300


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
    # Why the replay stopped before every tap had been claimed, by the name of the
    # class of Ember's error, `store` when the investigations file could not be
    # written, or None when it did not stop.
    stopped: str | None


def plan(records: Sequence[Record], count: int) -> list[Replay]:
    """Up to `count` replayed taps for a day's records, spread evenly over the
    day's calls, one in `AWAY_EVERY` of them away from every call."""
    count = max(0, min(count, MAX_TAPS))
    placed = [record for record in records if not record.medical and record.point is not None]
    if not placed or not count:
        return []
    step = max(1, len(placed) // count)
    chosen = placed[::step][:count]
    replays: list[Replay] = []
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
        replays.append(_replay(at, _rounded(point), near))
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
) -> Replayed:
    """Investigate every replayed tap that nobody has claimed. `ember` is closed
    when it returns."""
    engine = Engine(store, blocks)
    made = 0
    stopped: str | None = None
    try:
        for tap in replays:
            try:
                if not runs.claim(tap.id, datetime.now(UTC)):
                    # Claimed before, by this run or one that was stopped: it is not
                    # paid for a second time, whether or not its run was kept.
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
                runs.write(investigation.run)
            except RunsError:
                stopped = "store"
                break
            made += 1
            failure = investigation.failure
            if failure is not None and not isinstance(failure, InputTooLargeError):
                # Ember refused or failed. Nothing more is asked of it here.
                stopped = type(failure).__name__
                break
    finally:
        ember.close()
    return Replayed(made=made, stopped=stopped)


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
    """A call's point that no call of the day is within `CLEAR_M` of for
    `CLEAR_FOR` either side of `at`, looked for from a place in the list that
    depends on `index` alone."""
    for offset in range(len(placed)):
        point = placed[(index * 7 + offset) % len(placed)].point
        assert point is not None
        if not any(
            other.point is not None
            and other.point.distance_m(point) <= CLEAR_M
            and other.received_at - CLEAR_FOR <= at <= _until(other) + CLEAR_FOR
            for other in records
        ):
            return point
    return None


def _until(record: Record) -> datetime:
    if record.cleared_at is not None and record.cleared_at >= record.dispatched_at:
        return record.cleared_at
    return record.dispatched_at


def _rounded(point: Point) -> Point:
    """To three decimals of a degree, about a block, as the app rounds a tap."""
    return Point(round(point.lat, 3), round(point.lon, 3))


def _no_media(tap_id: str, kind: str) -> bytes | None:
    return None
