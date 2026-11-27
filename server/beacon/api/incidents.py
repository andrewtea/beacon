from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, model_validator

from beacon.incident import Engine, Incident
from beacon.incident.build import (
    CREDITS,
    STATUS_WORDS,
    label_of,
    weakest,
)
from beacon.investigate import Cited, Runs
from beacon.investigate import card as words
from beacon.mapping import NO_MAP_OF_MEDICAL, NO_POINT, Feature, drawn
from beacon.radio.vocabulary import MEDICAL_EVENT
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


class CreditPartOut(_Read):
    text: str
    # Where these words link to, or null for words that are no link.
    url: str | None


class CreditOut(_Read):
    # Words a source's terms require beside anything shown from it, and their link.
    text: str
    url: str
    # The same words in pieces, for a credit whose terms ask for several links
    # (adsb.lol's: the database, its license, and how Beacon's rows differ). Show
    # the pieces in order, each with its own link, in place of `text` linked to
    # `url`. Empty for a credit that is one link.
    parts: list[CreditPartOut] = []


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

    @model_validator(mode="after")
    def _no_ember_in_a_stand_ins_label(self) -> "SourceOut":
        """A row a stand-in for Ember wrote is not labeled "as Ember heard it",
        wherever the label is from: a story that was kept holds the labels its
        sources had when it was written."""
        if self.model == "fake":
            self.label = label_of(self.source, self.model)
        return self


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


class CitedOut(BaseModel):
    """A signal a decision on crisis mode rested on, as the incident had it when
    the decision was made."""

    kind: str
    confidence: Literal["official", "high", "medium", "low"]
    # The evidence rows the signal rested on then. Empty for taps, which are
    # counted and not listed.
    evidence_ids: list[int]
    # How the incident has a signal of that kind now: `same` when it rests on those
    # rows still, `changed` when it rests on others, `gone` when it has none.
    now: Literal["same", "changed", "gone"]
    # The credit the feed requires that the rows it rested on are from, beside
    # what is shown of it: 511's for a closure, and adsb.lol's for an aircraft
    # adsb.lol answered with. It is here, and not only on the signal the incident
    # has now, because that signal can be gone while this is still shown.
    credit: CreditOut | None


class CrisisOut(BaseModel):
    """Crisis mode for an incident. Beacon's investigator turns it on when it looks
    into a tap and decides that the incident's signals add up to more than an
    everyday call; code holds the decision to signals the engine found. The first
    such decision is the one kept: a later run that decides otherwise turns nothing
    off. It is the investigator's decision and no authority's, and `text` says so.
    It tells nobody what to do."""

    # Whether it is on now: it was turned on, and Beacon's record of the incident
    # is open (the incident's `open`).
    active: bool
    decided_at: datetime
    text: str
    # Who decided: `model` for Muse Ember, `stand_in` for the stand-in that works
    # by rule. `decided_by_text` says it in a sentence: show it with `text`.
    decided_by: Literal["model", "stand_in"]
    decided_by_text: str
    # The signals the decision rested on, as they were then.
    cited: list[CitedOut]
    # The signals of those kinds the incident has now, each with its sources, and
    # a sentence that says that is what they are. They are not the decision's
    # grounds: those are `cited`.
    signals: list[SignalOut]
    signals_text: str
    # Which kind of Ember client decided: `live`, or `fake` for a stand-in.
    model: str | None
    # True when a stand-in decided, or the incident is built from a stand-in's rows.
    stand_in: bool
    stand_in_text: str | None


class RadioOut(BaseModel):
    """What an incident's `title`, `status`, `place`, `units`, `alarm`, and
    `opened_at` rest on: the radio calls it is made of. They are what a card of
    the incident shows, so a card shows this with them."""

    # The least confidence among the calls: the facts are trusted no more than
    # the weakest call under them.
    confidence: Literal["official", "high", "medium", "low"]
    # One source for each call.
    sources: list[SourceOut]


class IncidentSummary(_Read):
    id: str
    category: str
    title: str
    status: str
    # The status in a sentence, as what the radio said: show it as it is. Null for
    # a medical response, of which no status is shown, and when the radio gave none.
    status_text: str | None = None
    # Whether Beacon's record of it is open: its radio has not cleared it and was
    # heard from in the last 45 minutes, or an alert for its place is in force. A
    # record that is closed says nothing of what is happening at the place now:
    # the radio can simply have gone quiet. Label it as the record's state.
    open: bool
    # When its first radio call came, and when the newest line of its timeline
    # is from, which can be a line from any source.
    opened_at: datetime
    updated_at: datetime
    # The radio calls its title, status, place, units, alarm, and first call rest
    # on. Null when none of its calls is among the lines that were read.
    radio: RadioOut | None = None
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
    # Taps, not people. `taps_more` says there were more than were counted. The
    # count is from taps and not from the radio: the timeline has a line for it,
    # with its own source and confidence.
    taps: int
    taps_more: bool
    # Whether more positions of aircraft were near it than were read.
    aircraft_more: bool
    signals: list[SignalOut]
    conflicts: list[ConflictOut]
    # True when any row the incident is built from is a stand-in's: the invented
    # scenario, or a fake Ember. It is then to be labeled as one.
    stand_in: bool
    # Null until the investigator has decided on crisis mode for it.
    crisis: CrisisOut | None = None


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


class PositionOut(BaseModel):
    lat: float
    lon: float
    at: datetime


class FeatureOut(BaseModel):
    """One thing on the map of an incident: what a source said, and where it is.
    Show `text`, the confidence, and the sources with it, as with a line of the
    timeline: a source's `credit` is 511's, and goes wherever the feature is shown."""

    # `place` is the point Beacon has for the incident; `traffic_event` a 511 event
    # as its version in force has it; `aircraft` an aircraft of the timeline's;
    # `alert_area` the area an authority drew for an alert that is in force here.
    kind: Literal["place", "traffic_event", "aircraft", "alert_area"]
    # Written by code. For a traffic event, an aircraft, and an alert it is the
    # line the timeline has for it, word for word.
    text: str
    at: datetime
    confidence: Literal["official", "high", "medium", "low"]
    sources: list[SourceOut]
    # A place and a traffic event are a point.
    point: PointOut | None
    # An aircraft is the positions it was seen at, oldest first.
    path: list[PositionOut]
    # An alert's area is polygons, each a list of closed rings: its outline first,
    # then the holes in it. A place is in the area when it is inside the outline
    # of some polygon and in none of that polygon's holes. Draw each polygon with
    # its own holes: all the rings read together by parity are another area.
    polygons: list[list[list[PointOut]]]
    # What is not drawn of it, in a sentence: show it with `text`. Null otherwise.
    note: str | None


class MapAnswer(BaseModel):
    """The map of one incident. It shows that incident and nothing else: no tap,
    no other incident, and no notice, which has no place."""

    mode: Literal["live", "mock"]
    as_of: datetime
    partial: bool
    incident: IncidentSummary
    # The place first, when there is one. Empty for an incident with no point.
    features: list[FeatureOut]
    # Why nothing is drawn, in a sentence, when nothing is. Null otherwise.
    text: str | None


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


def crisis_of(request: Request, incident: Incident) -> CrisisOut | None:
    """The decision that turned crisis mode on for an incident, with what it
    rested on then and what the incident has of it now. Reading it asks Ember
    nothing, and needs no desk: a decision that was made is read whether or not
    taps are still being investigated."""
    runs: Runs | None = getattr(request.app.state, "runs", None)
    if runs is None or incident.category == MEDICAL_EVENT:
        return None
    decision = runs.crisis(incident.id)
    if decision is None:
        return None
    kinds = {cited.kind for cited in decision.cited}
    now = {
        signal.kind: {source.evidence_id for source in signal.sources} - {None}
        for signal in incident.signals
    }
    having = [signal for signal in incident.signals if signal.kind in kinds]
    by_model = decision.model == "live"
    return CrisisOut(
        active=incident.open,
        decided_at=decision.decided_at,
        text=words.CRISIS,
        decided_by="model" if by_model else "stand_in",
        decided_by_text=words.investigator_text(decision.model or "fake") or "",
        cited=[
            CitedOut(
                kind=cited.kind,
                confidence=cited.confidence,  # type: ignore[arg-type]
                evidence_ids=list(cited.evidence_ids),
                now="gone"
                if cited.kind not in now
                else "same"
                if set(cited.evidence_ids) <= now[cited.kind]
                else "changed",
                credit=cited_credit(cited),
            )
            for cited in decision.cited
        ],
        signals=[SignalOut.model_validate(signal) for signal in having],
        signals_text=words.CRISIS_NOW if having else words.CRISIS_NONE_NOW,
        model=decision.model,
        stand_in=decision.stand_in or incident.stand_in or not by_model,
        stand_in_text=words.stand_in_text(
            decision.model or "fake", evidence=decision.stand_in or incident.stand_in
        ),
    )


def cited_credit(cited: Cited) -> CreditOut | None:
    """The credit that goes with a signal a decision cited: that of the feed its
    rows were from, which the decision kept the name of (`Cited.credits`). Nothing
    is concluded from the signal's kind about an aircraft's feed: one of the
    invented scenario, or of a store from before Beacon asked adsb.lol, carries no
    notice of adsb.lol's. Reading it reads no evidence row.

    A closure always carries 511's credit, kept or not: the only rows a closure
    rests on are 511 events, 511's agreement asks for the credit, and a decision
    kept before feeds were written down names none."""
    feeds = {*cited.credits, *(("511",) if cited.kind == "closure" else ())}
    for feed, credit in CREDITS.items():
        if feed in feeds:
            return CreditOut.model_validate(credit)
    return None


def status_text_of(incident: Incident) -> str | None:
    """An incident's status in a sentence. The status is the furthest along that a
    call on the radio gave, so it is said as what the radio said, and by nobody in
    particular: a radio row keeps no speaker. None for a medical response, which
    is its crews and its block, and for an incident whose calls gave no status."""
    if incident.category == MEDICAL_EVENT or incident.status == "unknown":
        return None
    if incident.status == "clearing":
        # A call gave a status of cleared or cancelled while a unit it did not
        # name had been named lately. Whether the call cleared units, and which,
        # is not kept: a call that cancels can name a street and no unit. So this
        # says that such a call was made, and nothing of what it was about.
        return "A clearance or a cancellation was reported on the radio."
    words_for = STATUS_WORDS.get(incident.status)
    return None if words_for is None else f"The radio said: {words_for}."


def radio_of(incident: Incident) -> RadioOut | None:
    """The radio calls an incident's card rests on, from the lines of its timeline
    that are calls, each of which has its row's source and confidence."""
    calls = [entry for entry in incident.timeline if entry.kind in ("radio", "medical")]
    if not calls:
        return None
    return RadioOut(
        confidence=weakest([entry.confidence for entry in calls]),
        sources=[SourceOut.model_validate(source) for entry in calls for source in entry.sources],
    )


def summary_of(request: Request, incident: Incident) -> IncidentSummary:
    """An incident as the list has it, with its status in words, the calls it
    rests on, and its crisis mode."""
    summary = IncidentSummary.model_validate(incident)
    summary.status_text = status_text_of(incident)
    summary.radio = radio_of(incident)
    summary.crisis = crisis_of(request, incident)
    return summary


def _engine(request: Request) -> Engine:
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if engine is None:
        raise HTTPException(503, "This server is not reading incidents.")
    return engine


@router.get("/incidents")
def incidents(request: Request, everything: bool = Query(False, alias="all")) -> Incidents:
    """The incidents whose record is open, and the ones whose newest line is from
    the last two hours, newest first. With `all=true`, every incident the engine has."""
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
        incidents=[summary_of(request, incident) for incident in listed],
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
    detail.status_text = status_text_of(found)
    detail.radio = radio_of(found)
    detail.crisis = crisis_of(request, found)
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


def _feature_out(feature: Feature) -> FeatureOut:
    return FeatureOut(
        kind=feature.kind,
        text=feature.text,
        at=feature.at,
        confidence=feature.confidence,
        sources=[SourceOut.model_validate(source) for source in feature.sources],
        point=None if feature.point is None else PointOut.model_validate(feature.point),
        path=[
            PositionOut(lat=seen.point.lat, lon=seen.point.lon, at=seen.at) for seen in feature.path
        ],
        polygons=[
            [[PointOut.model_validate(point) for point in ring] for ring in rings]
            for rings in feature.polygons
        ],
        note=feature.note,
    )


@router.get("/incidents/{incident_id}/map")
def incident_map(request: Request, incident_id: str) -> MapAnswer:
    """What Beacon can place of one incident, each thing with its sentence, its
    confidence, and its sources. Opened from the incident's card: there is no map
    of every incident. A medical response has none, and is answered 404."""
    engine = _engine(request)
    snapshot = engine.snapshot()
    found: Incident | None = snapshot.get(incident_id)
    if found is None:
        raise HTTPException(404, "There is no such incident.")
    if found.category == MEDICAL_EVENT:
        raise HTTPException(404, NO_MAP_OF_MEDICAL)
    # A row is never changed, so one read by its id after the reading is the row
    # the reading had.
    features = drawn(found, engine.store.get)
    return MapAnswer(
        mode=engine.mode,
        as_of=snapshot.as_of,
        partial=snapshot.partial,
        incident=summary_of(request, found),
        features=[_feature_out(feature) for feature in features],
        text=None if features else NO_POINT,
    )
