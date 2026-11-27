"""Scoring a scale run: which of DataSF's calls Beacon has an incident for.

The incidents are the incident engine's, read from the run's evidence store as the
server reads its own (`Engine.at`), at moments through the day and after it, because
the engine looks back twelve hours and no further. An incident that is read at
several of those moments is counted once, as its fullest reading.

**How an incident is taken to be a record's** (`fits`), which is plain code and the
same for generated audio and for recordings:

- both are medical, or neither is: a medical response is never matched to a fire;
- the incident opened no earlier than `EARLY` before DataSF says the call was
  received, and no later than `LATE` after it says the last unit was available
  again (or `OPEN_FOR` after the dispatch, when it does not say); and
- the incident names a unit DataSF lists for the call, or has a point within
  `NEAR_M` of DataSF's.

Each record takes one incident and each incident one record, the pairs closest in
time first (`match`). What is left over is told apart as far as code can:

- a record is `merged` when it has no incident of its own and some incident that
  names one of its units, or lies near it, has a radio call within the record's
  time. That is taken to mean Beacon heard the call and put it in another call's
  incident, as it does for a second call at one block and for a call that names
  units and no place; nothing checks it, and an incident that was only near by at
  the time is counted the same;
- any other record without an incident is `missed`;
- an incident that fits a record and is not that record's own is `split`: Beacon
  made more than one incident of one call, as it does when a unit is heard from
  after its incident has gone quiet; and
- an incident that fits no record is `extra`.

What the rule cannot know: two calls that share a unit and overlap in time can be
swapped, a call DataSF does not list cannot be told from a wrong incident, and
nothing here checks what an incident *says* beyond its kind and its place.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from beacon.evidence import EvidenceStore
from beacon.incident import Engine
from beacon.incident.model import Incident
from beacon.incident.places import Blocks
from beacon.radio.vocabulary import MEDICAL_EVENT
from beacon.scale.datasf import Record

EARLY = timedelta(minutes=2)
LATE = timedelta(minutes=5)
OPEN_FOR = timedelta(minutes=60)
NEAR_M = 300.0
# How far apart the moments are at which the engine is read, and for how long after
# the last dispatch: less than the engine's twelve hours, so nothing is passed by.
STEP = timedelta(hours=6)
AFTER = timedelta(hours=12)


@dataclass(frozen=True)
class Reading:
    """The incidents of a run, each once, and the ids each was read under."""

    incidents: tuple[Incident, ...]
    # Every id an incident was read under at some moment, to the id it is kept by.
    kept_as: dict[str, str]
    # Whether any reading left rows out (`Snapshot.partial`).
    partial: bool


@dataclass(frozen=True)
class Pair:
    record: Record
    incident: Incident
    # From DataSF's point to the incident's, in metres, when both have one.
    away_m: float | None
    # Whether the incident's category is the record's kind.
    same_kind: bool


@dataclass(frozen=True)
class Matched:
    """What matching a day's records to a run's incidents came to."""

    found: tuple[Pair, ...]
    merged: tuple[Record, ...]
    missed: tuple[Record, ...]
    split: tuple[Incident, ...]
    extra: tuple[Incident, ...]
    # For each merged record, by its number, the incident it was taken to be
    # inside: the first, in the order they opened, that has a call of its time.
    inside: tuple[tuple[str, str], ...] = ()

    def incident_of(self, number: str) -> str | None:
        """The id of the incident a record was found as, or was taken to be inside,
        or None for a record that was missed."""
        for pair in self.found:
            if pair.record.number == number:
                return pair.incident.id
        return dict(self.inside).get(number)


def read_incidents(store: EvidenceStore, blocks: Blocks, records: Sequence[Record]) -> Reading:
    """Every incident the engine makes of the run's rows, from the first dispatch
    of the day to `AFTER` past the last."""
    engine = Engine(store, blocks)
    if not records:
        return Reading((), {}, False)
    first = min(record.dispatched_at for record in records)
    last = max(record.dispatched_at for record in records) + AFTER
    read: list[Incident] = []
    partial = False
    moment = first + STEP
    while True:
        snapshot = engine.at(min(moment, last))
        partial = partial or snapshot.partial
        read += snapshot.incidents
        if moment >= last:
            break
        moment += STEP
    # The fullest reading of each incident is kept. A reading that shares a row
    # with one that is kept is the same incident, read at another moment.
    read.sort(key=lambda incident: (-len(incident.evidence_ids), incident.opened_at, incident.id))
    kept: list[Incident] = []
    owner: dict[int, str] = {}
    kept_as: dict[str, str] = {}
    for incident in read:
        same = next((owner[row] for row in incident.evidence_ids if row in owner), None)
        if same is None:
            kept.append(incident)
            same = incident.id
            for row in incident.evidence_ids:
                owner[row] = same
        kept_as.setdefault(incident.id, same)
    kept.sort(key=lambda incident: (incident.opened_at, incident.id))
    return Reading(tuple(kept), kept_as, partial)


def fits(record: Record, incident: Incident) -> bool:
    """Whether an incident can be the record's, by the rule this module states."""
    if record.medical != (incident.category == MEDICAL_EVENT):
        return False
    first, last = _span(record)
    return first <= incident.opened_at <= last and _same_call(record, incident)


def _heard_in(record: Record, incident: Incident) -> bool:
    """Whether an incident that names one of the record's units, or lies near it,
    has a radio call within the record's time."""
    if not _same_call(record, incident):
        return False
    first, last = _span(record)
    return any(
        first <= entry.at <= last
        for entry in incident.timeline
        if entry.kind in ("radio", "medical")
    )


def _span(record: Record) -> tuple[datetime, datetime]:
    """The time a record's call can have been heard in."""
    until = record.cleared_at
    if until is None or until < record.dispatched_at:
        until = record.dispatched_at + OPEN_FOR
    return record.received_at - EARLY, until + LATE


def _same_call(record: Record, incident: Incident) -> bool:
    if set(record.units) & set(incident.units):
        return True
    away = _away_m(record, incident)
    return away is not None and away <= NEAR_M


def match(records: Sequence[Record], incidents: Sequence[Incident]) -> Matched:
    """Pair the records with the incidents, one to one, closest in time first."""
    candidates: list[tuple[float, int, str, str, Record, Incident]] = []
    fitted: set[str] = set()
    for record in records:
        for incident in incidents:
            if fits(record, incident):
                gap = abs((incident.opened_at - record.dispatched_at).total_seconds())
                shared = len(set(record.units) & set(incident.units))
                candidates.append((gap, -shared, record.number, incident.id, record, incident))
                fitted.add(incident.id)
    candidates.sort(key=lambda candidate: candidate[:4])
    taken_records: set[str] = set()
    taken_incidents: set[str] = set()
    found: list[Pair] = []
    for _, _, number, incident_id, record, incident in candidates:
        if number in taken_records or incident_id in taken_incidents:
            continue
        taken_records.add(number)
        taken_incidents.add(incident_id)
        found.append(
            Pair(
                record=record,
                incident=incident,
                away_m=_away_m(record, incident),
                same_kind=record.medical or incident.category == record.kind,
            )
        )
    found.sort(key=lambda pair: (pair.record.dispatched_at, pair.record.number))
    left = [record for record in records if record.number not in taken_records]
    in_order = sorted(incidents, key=lambda incident: (incident.opened_at, incident.id))
    inside: dict[str, str] = {}
    for record in left:
        holder = next((of for of in in_order if _heard_in(record, of)), None)
        if holder is not None:
            inside[record.number] = holder.id
    return Matched(
        found=tuple(found),
        merged=tuple(record for record in left if record.number in inside),
        missed=tuple(record for record in left if record.number not in inside),
        inside=tuple(inside.items()),
        split=tuple(
            incident
            for incident in incidents
            if incident.id in fitted and incident.id not in taken_incidents
        ),
        extra=tuple(incident for incident in incidents if incident.id not in fitted),
    )


def first_heard(incident: Incident) -> datetime | None:
    """When the first radio call of an incident was made, by its timeline."""
    times = [entry.at for entry in incident.timeline if entry.kind in ("radio", "medical")]
    return min(times, default=None)


def _away_m(record: Record, incident: Incident) -> float | None:
    if record.point is None or incident.point is None:
        return None
    return record.point.distance_m(incident.point)
