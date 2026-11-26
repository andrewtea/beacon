from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from beacon.incident import Engine, Incident

router = APIRouter()

# An incident that is over stays on the list for this long after its last row.
LISTED_AFTER = timedelta(hours=2)


class _Read(BaseModel):
    """Read from the engine's own objects, field by field."""

    model_config = ConfigDict(from_attributes=True)


class PointOut(_Read):
    lat: float
    lon: float


class CreditOut(_Read):
    # Words a source's terms require beside anything shown from it, and their link.
    text: str
    url: str


class SourceOut(_Read):
    kind: str
    source: str
    label: str
    confidence: Literal["official", "high", "medium", "low"]
    at: datetime
    # The evidence row. Null for taps, which are counted and not listed.
    evidence_id: int | None
    # Where a person can read the original, when it is public.
    url: str | None
    stand_in: bool
    # Which kind of Ember client wrote the row, when a model did: live, replay, fake.
    model: str | None
    credit: CreditOut | None


class EntryOut(_Read):
    at: datetime
    kind: str
    text: str
    confidence: Literal["official", "high", "medium", "low"]
    sources: list[SourceOut]
    details: dict[str, Any]


class OfficialOut(_Read):
    kind: Literal["alert", "notice"]
    type: str
    state: str
    ended_by: int | None
    issued_at: datetime
    until: datetime | None
    issuer: str | None
    headline: str | None
    # An alert's instruction, in the authority's own words. Beacon adds none.
    instruction: str | None
    # True for a notice: Beacon has a model's reading of the document at
    # `source.url`, and shows none of its words yet.
    reading: bool
    source: SourceOut
    # The fields of an alert left out because the stored text may have been cut:
    # the alert has more, at `source.url`.
    incomplete: list[str]


class SignalOut(_Read):
    kind: str
    text: str
    confidence: Literal["official", "high", "medium", "low"]
    # The rows the signal rests on, each with its link and its credit.
    sources: list[SourceOut]


class TrackOut(_Read):
    icao24: str
    callsign: str | None
    category: str | None
    first_seen: datetime
    last_seen: datetime
    positions: int
    nearest_m: float
    lowest_m: float | None
    highest_m: float | None
    circling: bool
    low: bool


class IncidentSummary(_Read):
    id: str
    category: str
    title: str
    status: str
    open: bool
    opened_at: datetime
    updated_at: datetime
    place: str | None
    point: PointOut | None
    point_from: str | None
    # For a point taken from a 511 closure, the row it is from, with the credit
    # 511's data agreement requires beside anything shown from it. Show the credit
    # with the point. Null for a point from the list of blocks.
    point_source: SourceOut | None
    streets: list[str]
    units: list[str]
    alarm: int | None
    # Taps, not people. `taps_more` says there were more than were counted.
    taps: int
    taps_more: bool
    # Whether more positions of aircraft were near it than were read.
    aircraft_more: bool
    signals: list[SignalOut]
    # True when any row the incident is built from is a stand-in's: the invented
    # scenario, or a fake Ember. It is then to be labeled as one.
    stand_in: bool


class IncidentDetail(IncidentSummary):
    timeline: list[EntryOut]
    official: list[OfficialOut]
    aircraft: list[TrackOut]


class Incidents(BaseModel):
    # `mock` when the evidence is the invented scenario's.
    mode: Literal["live", "mock"]
    as_of: datetime
    # Whether there was more evidence than the engine reads at once: an incident
    # can then lack its oldest lines, count fewer taps than there were, or show an
    # old alert as `unknown`.
    partial: bool
    incidents: list[IncidentSummary]


class IncidentAnswer(BaseModel):
    mode: Literal["live", "mock"]
    as_of: datetime
    partial: bool
    incident: IncidentDetail


def _engine(request: Request) -> Engine:
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if engine is None:
        raise HTTPException(503, "This server is not reading incidents.")
    return engine


@router.get("/incidents")
def incidents(request: Request, everything: bool = Query(False, alias="all")) -> Incidents:
    """The incidents that are going on, and the ones that ended in the last two
    hours, newest first. With `all=true`, every incident the engine has."""
    engine = _engine(request)
    # One reading for the whole answer: its incidents, its moment, and its flag.
    snapshot = engine.snapshot()
    listed = [
        incident
        for incident in snapshot.incidents
        if everything or incident.open or snapshot.as_of - incident.updated_at <= LISTED_AFTER
    ]
    return Incidents(
        mode=engine.mode,
        as_of=snapshot.as_of,
        partial=snapshot.partial,
        incidents=[IncidentSummary.model_validate(incident) for incident in listed],
    )


@router.get("/incidents/{incident_id}")
def incident(request: Request, incident_id: str) -> IncidentAnswer:
    """One incident with its timeline, every line with its source and confidence."""
    engine = _engine(request)
    snapshot = engine.snapshot()
    found: Incident | None = snapshot.get(incident_id)
    if found is None:
        raise HTTPException(404, "There is no such incident.")
    return IncidentAnswer(
        mode=engine.mode,
        as_of=snapshot.as_of,
        partial=snapshot.partial,
        incident=IncidentDetail.model_validate(found),
    )
