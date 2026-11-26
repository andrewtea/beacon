from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from beacon.incident import Engine, Incident
from beacon.story import StoryDesk

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
    # An alert's instruction, in the authority's own words. Beacon adds none. For
    # a notice (`reading` is true), Ember's reading of the document's instruction,
    # when the redaction pass let it through: show it as Beacon's reading, beside
    # the link, and never as what to do.
    instruction: str | None
    # A notice's summary and the areas it names, as Ember read them. Empty for an alert.
    summary: str | None
    areas: list[str]
    # True for a notice: `issuer`, `headline`, `instruction`, `summary`, and
    # `areas` are a model's reading of the document at `source.url`, which is the
    # authority speaking.
    reading: bool
    source: SourceOut
    # The fields that are not all here because there was more than Beacon keeps:
    # say that the original has more, at `source.url`.
    incomplete: list[str]
    # The fields of a notice the redaction pass did not let through, which are
    # left out whole: say that the document has more.
    withheld: list[str]


class SignalOut(_Read):
    kind: str
    text: str
    confidence: Literal["official", "high", "medium", "low"]
    # The rows the signal rests on, each with its link and its credit.
    sources: list[SourceOut]


class ConflictOut(_Read):
    # Radio traffic and an official source that do not agree. The official source
    # holds, and `text` says so and why: show it with the incident.
    kind: str
    text: str
    # As far as the weakest row it rests on is trusted.
    confidence: Literal["official", "high", "medium", "low"]
    official: list[SourceOut]
    radio: list[SourceOut]


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
    conflicts: list[ConflictOut]
    # True when any row the incident is built from is a stand-in's: the invented
    # scenario, or a fake Ember. It is then to be labeled as one.
    stand_in: bool


class ClaimOut(_Read):
    # What the sentence is about: `status`, `alarm`, `units`, a detail a call gave
    # (`evacuation`, `knocked_down`, ...), `closure`, `aircraft`, or `taps`.
    kind: str
    # Written by code from the rows it rests on. No word of it is a model's.
    text: str
    # When the newest line it rests on is from: a status is the status as of then.
    at: datetime
    # The least confidence among the lines it rests on, and those lines' sources.
    confidence: Literal["official", "high", "medium", "low"]
    sources: list[SourceOut]


class StoryOut(_Read):
    """The few points Ember chose from the incident's radio, closure, aircraft,
    and tap lines, each checked against its rows and worded by code. It says
    nothing of alerts or notices: those are in `official`."""

    written_at: datetime
    # Whether it was written from the lines the incident has now. False while a
    # newer one is due: the timeline then has lines the story has not read.
    current: bool = False
    claims: list[ClaimOut]
    # How many of the points Ember gave were left out because the lines they cite
    # do not show them, and how many of the incident's older lines Ember was not
    # given.
    left_out: int
    lines: int
    lines_left_out: int
    # Which kind of Ember client chose the points: `live`, or `fake` for a stand-in.
    model: str
    # True when a stand-in chose them or a line under it is a stand-in's: label it.
    stand_in: bool
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int


class IncidentDetail(IncidentSummary):
    timeline: list[EntryOut]
    official: list[OfficialOut]
    aircraft: list[TrackOut]
    # Null when no story has been written: the incident is then its lines.
    story: StoryOut | None = None


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
    detail = IncidentDetail.model_validate(found)
    desk: StoryDesk | None = getattr(request.app.state, "stories", None)
    # Reading a story asks Ember nothing: only the desk's own pass writes one.
    told = None if desk is None else desk.latest(found)
    if told is not None:
        story, current = told
        detail.story = StoryOut.model_validate(story).model_copy(update={"current": current})
    return IncidentAnswer(
        mode=engine.mode,
        as_of=snapshot.as_of,
        partial=snapshot.partial,
        incident=detail,
    )
