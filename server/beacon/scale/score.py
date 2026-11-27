"""Scoring a scale run: which of DataSF's calls Beacon has an incident for.

The incidents are the incident engine's, read from the run's evidence store as the
server reads its own (`Engine.at`), at moments from the run's first radio row to its
last, because the engine looks back twelve hours and no further. The moments are
the rows' own and not DataSF's: a call heard long after the day's last dispatch is
read too. An incident that is read at several of those moments is counted once, as
its fullest reading, and two readings are of one incident when they share a row or
are joined by readings between them that do: an incident that goes on for longer
than the engine looks back is never read whole, and is still one incident. It is
then scored by its fullest reading, which lacks the calls the engine no longer saw.

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

- a record is `merged` when it has no incident of its own and some incident that is
  another record's own, of the same medical-or-not kind, that
  names one of its units, or lies near it, has a radio call within the record's
  time. That is taken to mean Beacon heard the call and put it in another call's
  incident, as it does for a second call at one block and for a call that names
  units and no place; nothing checks it, and an incident that was only near by at
  the time is counted the same;
- any other record without an incident is `missed`;
- an incident that fits a record and was paired with none is `split`. That is what
  Beacon leaves when it makes more than one incident of one call, as it does when a
  unit is heard from after its incident has gone quiet; the rule cannot tell that
  from an incident that merely fits a call by its time and a unit or its place; and
- an incident that fits no record is `extra`.

What the rule cannot know: two calls that share a unit and overlap in time can be
swapped, a call DataSF does not list cannot be told from a wrong incident, and
nothing here checks what an incident *says* beyond its kind and its place.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from beacon.evidence import EvidenceStore
from beacon.incident import Engine, incident_id
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


@dataclass(frozen=True)
class Reading:
    """The incidents of a run, each once, and the ids each was read under."""

    incidents: tuple[Incident, ...]
    # Every id an incident can be read under at some moment (one for each of its
    # rows, which can be the first of them in view), to the id it is kept by.
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


def read_incidents(store: EvidenceStore, blocks: Blocks) -> Reading:
    """Every incident the engine makes of the run's radio rows, read at moments
    from the first row's time to the last row's, which the last reading is at."""
    engine = Engine(store, blocks)
    oldest = store.search(kinds=["radio"], limit=1)
    newest = store.search(kinds=["radio"], newest_first=True, limit=1)
    if not oldest or not newest:
        return Reading((), {}, False)
    first, last = oldest[0].occurred_at, newest[0].occurred_at
    read: list[Incident] = []
    partial = False
    moment = first + STEP
    while True:
        # Both ends of a reading are included, so the one at `last` has the last row.
        snapshot = engine.at(min(moment, last))
        partial = partial or snapshot.partial
        read += snapshot.incidents
        if moment >= last:
            break
        moment += STEP
    # Readings that share a row are of one incident, read at different moments, and
    # so are readings that are joined by others between them: the rows are put into
    # sets that way, and each set is one incident.
    joined: dict[int, int] = {}

    def group(row: int) -> int:
        while joined.setdefault(row, row) != row:
            joined[row] = joined[joined[row]]
            row = joined[row]
        return row

    for incident in read:
        for row in incident.evidence_ids[1:]:
            one, other = sorted((group(incident.evidence_ids[0]), group(row)))
            joined[other] = one
    readings: dict[object, list[Incident]] = {}
    for incident in read:
        rows = incident.evidence_ids
        # An incident with no row, which the engine does not make, is its own.
        readings.setdefault(group(rows[0]) if rows else incident.id, []).append(incident)
    kept: list[Incident] = []
    kept_as: dict[str, str] = {}
    for same in readings.values():
        # The fullest reading of each incident is kept.
        fullest = min(
            same,
            key=lambda incident: (-len(incident.evidence_ids), incident.opened_at, incident.id),
        )
        kept.append(fullest)
        for incident in same:
            kept_as[incident.id] = fullest.id
        # An incident is named by the row of its that was stored first, of the rows
        # in view, so at a moment between two readings it can have a name that none
        # of them has: a tap's investigation is at such a moment. Every name a row
        # of the incident can give it is the incident's.
        for row in sorted({row for incident in same for row in incident.evidence_ids}):
            evidence = store.get(row)
            if evidence is not None:
                kept_as.setdefault(incident_id(evidence), fullest.id)
    kept.sort(key=lambda incident: (incident.opened_at, incident.id))
    return Reading(tuple(kept), kept_as, partial)


def fits(record: Record, incident: Incident) -> bool:
    """Whether an incident can be the record's, by the rule this module states."""
    if record.medical != (incident.category == MEDICAL_EVENT):
        return False
    first, last = _span(record)
    return first <= incident.opened_at <= last and _same_call(record, incident)


def _heard_in(record: Record, incident: Incident) -> bool:
    """Whether an incident of the record's medical-or-not kind that names one of the
    record's units, or lies near it, has a radio call within the record's time."""
    if record.medical != (incident.category == MEDICAL_EVENT):
        return False
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
    for _, _, number, taken_id, record, incident in candidates:
        if number in taken_records or taken_id in taken_incidents:
            continue
        taken_records.add(number)
        taken_incidents.add(taken_id)
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
        # Only an incident that is some other call's own can hold this one: an
        # incident that is nobody's is counted as that, and never as both.
        holder = next(
            (of for of in in_order if of.id in taken_incidents and _heard_in(record, of)), None
        )
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
