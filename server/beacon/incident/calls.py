"""Grouping radio calls into incidents: which calls are about the same thing.

Plain code decides what belongs together, so that it can be tested, and no model is
asked. A radio row is what one call said, in a closed vocabulary: a category, a
status, units, and sometimes a street and its hundred block. `group_calls` reads the
calls in the order they were made and puts each one with an incident, opens a new
incident for it, or leaves it out. The rules, in the order they apply:

- **A call that says nothing is left out**: one nobody could make out, housekeeping
  (`administrative`), and one that names no unit and no street.
- **Medical calls are grouped only with medical calls.** A medical response is its
  crews and its block and nothing more, so it never takes on a fire's timeline, and
  a fire never takes on a patient's call, even at the same block. It has no status
  either, so it is never "cleared": it is open until it has gone quiet.
- **A place is what an incident is.** A call that names a street joins the incident
  at that street and block, and never one at another place, whatever units it
  shares with it: a unit that is sent on from one call to the next names both.
- **Units tie the rest together.** A call that names no street joins the incident
  whose units it names, which is how most calls are tied: `E36 on scene` gives no
  address. When no incident has its units, it opens one with no place yet, which the
  first call to name those units with a street then gives its place.
- **An incident goes quiet.** One that has not been heard from for `IDLE` takes no
  more calls, and a later call at its block opens a new incident. So does a new
  dispatch to a block whose incident was cleared.

What this cannot know: a street or a unit is kept only when it is on a list
(`beacon.radio.Known`), so with no lists every call names nothing and no incident is
opened at all, and nothing checks that Ember heard a call right. Two incidents at
one block within `IDLE` of each other are taken for one.

The fold is a function of the calls it is given and of nothing else, so it gives the
same incidents whenever it is run over the same rows. A call that is stored late is
put where its time puts it.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from beacon.evidence import Evidence

# How long an incident that has not been heard from still takes calls.
IDLE = timedelta(minutes=45)
# A call that clears some units clears the incident when no other unit of it was
# named this recently; otherwise the incident is `clearing`.
RECENT = timedelta(minutes=20)

Status = Literal[
    "dispatched",
    "en_route",
    "on_scene",
    "working",
    "under_control",
    "clearing",
    "cleared",
    "cancelled",
    "unknown",
]

# How far along each status is. A call that reports an earlier one than the incident
# has reached does not take the incident back.
_PROGRESS = {"unknown": 0, "dispatched": 1, "en_route": 2, "on_scene": 3, "working": 4}
_PROGRESS["under_control"] = 5
_OVER = frozenset({"cleared", "cancelled"})

# Which category names an incident whose calls gave several, most telling first.
_CATEGORY_RANK = (
    "fire",
    "hazmat",
    "rescue",
    "collision",
    "alarm",
    "utility",
    "service",
    "other",
)


@dataclass
class CallGroup:
    """The calls of one incident, and what they add up to."""

    medical: bool
    rows: list[Evidence] = field(default_factory=list)
    street: str | None = None
    block: int | None = None
    cross_street: str | None = None
    category: str = "other"
    status: Status = "unknown"
    alarm: int | None = None
    # Every unit a call of the incident named, with when it was last named, and the
    # ones a call has since cleared or cancelled.
    named: dict[str, datetime] = field(default_factory=dict)
    released: set[str] = field(default_factory=set)

    @property
    def opened_at(self) -> datetime:
        return self.rows[0].occurred_at

    @property
    def last_at(self) -> datetime:
        return max(row.occurred_at for row in self.rows)

    @property
    def over(self) -> bool:
        """Whether the last word on the radio was that it is cleared or cancelled."""
        return self.status in _OVER

    @property
    def active(self) -> set[str]:
        """The units that were named and not since cleared."""
        return set(self.named) - self.released

    def _fits(self, call: "_Call") -> bool:
        """Whether a call that names a street is at this incident's place."""
        if self.street != call.street:
            return False
        return self.block is None or call.block is None or self.block == call.block

    def _take(self, row: Evidence, call: "_Call") -> None:
        self.rows.append(row)
        if call.street is not None and self.street is None:
            self.street = call.street
        if self.street == call.street:
            self.block = self.block if self.block is not None else call.block
            self.cross_street = self.cross_street or call.cross_street
        if not self.medical:
            if _CATEGORY_RANK.index(call.category) < _CATEGORY_RANK.index(self.category):
                self.category = call.category
            if call.alarm is not None:
                self.alarm = max(self.alarm or 0, call.alarm)
        for unit in call.units:
            self.named[unit] = call.at
        if call.status in _OVER:
            self.released |= set(call.units)
            still_here = {
                unit
                for unit, at in self.named.items()
                if unit not in self.released and call.at - at <= RECENT
            }
            self.status = "clearing" if still_here else call.status  # type: ignore[assignment]
            return
        self.released -= set(call.units)
        if call.status == "unknown":
            return
        was = self.status
        if was not in _PROGRESS or _PROGRESS[call.status] > _PROGRESS[was]:
            self.status = call.status  # type: ignore[assignment]


@dataclass(frozen=True)
class _Call:
    at: datetime
    medical: bool
    category: str
    status: str
    units: tuple[str, ...]
    alarm: int | None
    street: str | None
    block: int | None
    cross_street: str | None


def group_calls(rows: Iterable[Evidence]) -> list[CallGroup]:
    """The incidents the radio rows among `rows` tell of, in the order they were
    opened. Rows of another kind are passed over."""
    groups: list[CallGroup] = []
    radio = [row for row in rows if row.kind == "radio"]
    for row in sorted(radio, key=lambda row: (row.occurred_at, row.id)):
        call = _read(row)
        if call is None:
            continue
        live = [
            group
            for group in groups
            if group.medical == call.medical and call.at - group.last_at <= IDLE
        ]
        group = _placed(live, call) if call.street is not None else _by_units(live, call)
        if group is None:
            if call.street is None and call.status in _OVER:
                # Units cleared from something nobody heard begin.
                continue
            group = CallGroup(medical=call.medical, category=call.category)
            groups.append(group)
        if call.street is not None:
            # A unit is at one place at a time: named at this one, it has left the
            # others, and a later call that names only the unit belongs here.
            for other in live:
                if other is not group:
                    other.released |= set(call.units) & set(other.named)
        group._take(row, call)
    return groups


def _placed(live: list[CallGroup], call: _Call) -> CallGroup | None:
    """The incident a call that names a street belongs to, or None for a new one."""
    # A new dispatch to a place whose incident is over is a new incident.
    here = [
        group
        for group in live
        if group._fits(call) and not (group.over and call.status == "dispatched")
    ]
    if here:
        return max(here, key=lambda group: group.last_at)
    # An incident that has no place yet, whose units this call names, gets this one.
    return _by_units([group for group in live if group.street is None], call)


def _by_units(live: list[CallGroup], call: _Call) -> CallGroup | None:
    """The incident that has the most of a call's units, and of those the one heard
    from last, or None when none has any."""
    units = set(call.units)
    sharing = [group for group in live if group.active & units]
    if not sharing:
        return None
    return max(sharing, key=lambda group: (len(group.active & units), group.last_at))


def _read(row: Evidence) -> _Call | None:
    """What a radio row says, or None for a row that ties to nothing: one that was
    not made out, housekeeping, and one with no unit and no street. The row's data
    was checked when it was stored, and is checked again for its shape only."""
    data = row.data
    category = data.get("category")
    if data.get("clarity") == "unintelligible" or category in ("unintelligible", "administrative"):
        return None
    medical = category == "medical"
    if not medical and category not in _CATEGORY_RANK:
        return None
    units = data.get("units")
    units = (
        tuple(unit for unit in units if isinstance(unit, str)) if isinstance(units, list) else ()
    )
    street = data.get("street") if isinstance(data.get("street"), str) else None
    if not units and street is None:
        return None
    block, cross, alarm, status = (
        data.get("block"),
        data.get("cross_street"),
        data.get("alarm"),
        data.get("status"),
    )
    if medical:
        # Of a medical call only the crews and the block are read, whatever the row
        # holds: ingest stores no more, and nothing here relies on that.
        status, alarm = "unknown", None
    return _Call(
        at=row.occurred_at,
        medical=medical,
        category="medical" if medical else category,
        status=status
        if isinstance(status, str) and (status in _PROGRESS or status in _OVER)
        else "unknown",
        units=units,
        alarm=alarm if isinstance(alarm, int) and not isinstance(alarm, bool) else None,
        street=street,
        block=block if isinstance(block, int) and not isinstance(block, bool) and street else None,
        cross_street=cross if isinstance(cross, str) and street else None,
    )
