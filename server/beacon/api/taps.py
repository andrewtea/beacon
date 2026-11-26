import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from beacon.api.incidents import IncidentSummary, summary_of
from beacon.evidence import Evidence
from beacon.incident import Engine, Snapshot
from beacon.investigate import KEPT, Accounting, InvestigationDesk, Outcome, Run
from beacon.investigate import card as words
from beacon.taps import MAX_CLIP_BYTES, MAX_PHOTO_BYTES, SOURCE, Tap, TapError, Taps
from beacon.taps.media import is_tap_id

router = APIRouter()

# The most a tap's request may hold: its photo and its clip in base64, which is four
# characters for three bytes, and room for the rest.
MAX_BODY_BYTES = (MAX_PHOTO_BYTES + MAX_CLIP_BYTES) * 4 // 3 + 100_000

_TOO_LARGE = "A tap is at most ten megabytes, with its photo and its clip."
# How long a tap's body may take to arrive. A phone on a poor connection sends ten
# megabytes in less; a sender who sends nothing holds a place for no longer.
RECEIVE_SECONDS = 20.0
_TOO_SLOW = "A tap has to arrive within twenty seconds. Send it again."
_NO_SUCH_TAP = "There is no such tap."
# For how long a tap's answer is read. A run is kept for `KEPT`, and a tap that is
# older is answered as one Beacon does not have, not as one it did not look into.
ANSWERED = KEPT - timedelta(hours=1)
# How long a tap that has no run yet is said to be waiting for one, and how long a
# run that was begun is said to be under way. After that it is said not to have
# been made: the server was stopped under it, or it was never handed in.
WAITING = timedelta(minutes=2)
RUNNING = timedelta(minutes=5)
# For how long after an unconfirmed tap, and how near it, Beacon lists what began
# since it looked.
WATCH = timedelta(minutes=30)
WATCH_M = 1_000.0


class TapReceipt(BaseModel):
    # Names the tap from here on: in the investigator's answer, and in its trace.
    id: str
    # When the tap arrived, by the server's clock.
    received_at: datetime
    # `mock` when Beacon is running on the invented scenario: whatever it answers
    # this tap with is then a stand-in's, and is to be labeled so.
    mode: Literal["live", "mock"]
    # Whether a photo and a clip were kept with it, and until when.
    photo: bool
    clip: bool
    media_kept_until: datetime | None
    # Whether the tap was handed to the investigator. Read the answer at
    # `GET /api/taps/{id}`: it is `pending` until the investigator has it.
    investigating: bool = False


class TapOut(BaseModel):
    id: str
    received_at: datetime
    photo: bool
    clip: bool


class GroundOut(BaseModel):
    # What the match rests on: `near`, `going_on`, `heard_in_clip`,
    # `seen_in_photo`, `aircraft_overhead`, `other_taps`. Code checked each one.
    kind: str
    text: str


class MatchOut(BaseModel):
    """Why the investigator took the tap to be about the incident. The choice is a
    model's; the grounds are the ones code found to hold."""

    # How far to trust the match, which code works out: `medium` or `low`, never more.
    confidence: Literal["medium", "low"]
    text: str
    grounds: list[GroundOut]
    # How many grounds the investigator gave that did not hold.
    grounds_dropped: int


class TapAnswer(BaseModel):
    """The answer to a tap, as it stands now."""

    # `mock` when the evidence is the invented scenario's.
    mode: Literal["live", "mock"]
    as_of: datetime
    partial: bool
    tap: TapOut
    # `pending` while the investigator has not answered. `incident` when it took
    # the tap to be about one. `unconfirmed` otherwise: Beacon does not guess.
    status: Literal["pending", "incident", "unconfirmed"]
    # Why it is unconfirmed: `nothing_near`, `not_looked`, `not_sure`,
    # `not_grounded`, `no_answer`, `cut_short`, `not_investigated`. Null otherwise.
    reason: str | None
    # The status in a sentence, written by code.
    text: str
    investigated_at: datetime | None
    # Which kind of Ember client investigated: `live`, or `fake` for a stand-in.
    model: str | None
    # True when a stand-in investigated or what it looked at is a stand-in's. Show
    # `stand_in_text` with the answer then.
    stand_in: bool
    stand_in_text: str | None
    # The incident the tap was taken to be about, as it is now, with its crisis
    # mode. `incident` is null and `incident_gone` true when Beacon no longer has
    # it under that id.
    incident_id: str | None
    incident: IncidentSummary | None
    incident_gone: bool
    incident_gone_text: str | None
    match: MatchOut | None
    # The other incidents the investigator's tools found near the tap, as they are
    # now. It did not take the tap to be about them, and `nearby_text` says so.
    nearby: list[IncidentSummary]
    nearby_text: str | None
    # Whether Beacon still lists what begins near the tap: for half an hour after
    # an unconfirmed tap. `since` are the incidents that began nearby after the
    # investigator looked. Nobody investigated whether one is what the tap was
    # about, and `since_text` says so: show it with them.
    watching: bool
    since: list[IncidentSummary]
    since_text: str | None
    # What the investigation cost, and how many calls to Ember it made.
    cost_usd: float
    ember_calls: int


class EmberCallOut(BaseModel):
    """One call to Muse Ember: `live`, or `fake` for a stand-in's answer."""

    source: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int


class CallOut(BaseModel):
    """One call of a tool: what the investigator asked for and why, and what came
    back. The texts are written by code from the call's own fields."""

    tool: str
    # What the investigator said it was thinking, from a closed list, and the
    # incident that was its lead.
    thinking: str | None
    thinking_text: str | None
    lead: str | None
    why: str | None
    args: dict[str, Any]
    # False when Beacon ran nothing: the call named no tool or an argument was
    # not on its list.
    ok: bool
    text: str
    # What came back, as ids, counts, and tags, and the same in a sentence.
    returned: dict[str, Any]
    returned_text: str
    # The call to Ember a clip or a photo was given to. Null for the other tools.
    ember: EmberCallOut | None


class RoundOut(BaseModel):
    n: int
    # The request to Ember that came back with this round's calls.
    ember: EmberCallOut
    calls: list[CallOut]


class OutcomeOut(BaseModel):
    status: Literal["incident", "unconfirmed"]
    reason: str | None
    text: str
    incident_id: str | None
    grounds: list[str]
    grounds_dropped: int
    confidence: Literal["medium", "low"] | None
    # Whether the investigator decided on crisis mode and the decision stood, the
    # signals it rests on, and whether it decided on it and code refused it.
    crisis: bool
    crisis_signals: list[str]
    crisis_refused: bool


class TapRun(BaseModel):
    """The investigator's run for one tap: every round, every call, and what each
    cost. It holds nothing the tap sent and nothing a source said."""

    mode: Literal["live", "mock"]
    tap_id: str
    # `pending` while a run is under way or about to be. `none` when none was made
    # and none will be. `lost` when one was begun and nothing was kept of it: the
    # server was stopped under it, and what it had asked Ember is in the ledger.
    state: Literal["none", "pending", "lost", "finished"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # The moment of the evidence the run looked at.
    evidence_as_of: datetime | None = None
    model: str | None = None
    stand_in: bool = False
    stand_in_text: str | None = None
    rounds: list[RoundOut] = []
    # The request that came back with the answer. Null when there was none.
    final: EmberCallOut | None = None
    outcome: OutcomeOut | None = None
    # Why the run ended before its answer, when it did: `budget`, or the kind of
    # error Ember raised.
    cut_short: str | None = None
    cut_short_text: str | None = None
    # Every call to Ember together.
    ember_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


@router.post("/taps", status_code=201)
async def tap(request: Request) -> TapReceipt:
    """Take a tap. The body is JSON, and is read here and not by FastAPI: a tap can
    hold a place, a photo, and a clip, so nothing about it goes through a reader
    that would repeat it in an error, and no more of it is read than a tap may be."""
    taps: Taps | None = getattr(request.app.state, "taps", None)
    if taps is None:
        raise HTTPException(503, "This server is not taking taps.")
    try:
        # A place among the taps being received is held from before the body is
        # read until the tap is taken or refused, so that only so many are in
        # memory at once.
        give_back = taps.admit()
        try:
            body = await _body(request)
            # Off the event loop: the body is parsed, its media is decoded, and its
            # files and its row are written.
            desk: InvestigationDesk | None = getattr(request.app.state, "investigations", None)
            work = asyncio.get_running_loop().run_in_executor(
                None, _take, taps, desk, body, give_back
            )
        except BaseException:
            give_back()
            raise
        # If this request is called off, the work goes on to its end, and it is
        # the work that gives the place back.
        taken, investigating = await asyncio.shield(work)
    except TapError as error:
        refused = error
    else:
        return TapReceipt(
            id=taken.id,
            received_at=taken.received_at,
            mode=taken.mode,
            photo=taken.photo,
            clip=taken.clip,
            media_kept_until=taken.media_kept_until,
            investigating=investigating,
        )
    headers = None if refused.retry_after is None else {"Retry-After": str(refused.retry_after)}
    raise HTTPException(refused.status, str(refused), headers=headers)


def _take(
    taps: Taps, desk: InvestigationDesk | None, body: bytes, give_back: Callable[[], None]
) -> tuple[Tap, bool]:
    """Take a tap and hand it to the investigator, and say whether it was taken
    there. Handed in, and not waited for: the answer is read with the tap's id. A
    tap whose request was called off is handed in all the same."""
    try:
        taken = taps.take_body(body)
    finally:
        give_back()
    return taken, desk is not None and desk.submit(taken.id)


async def _body(request: Request) -> bytes:
    """The request's body. Answers 413 as soon as it is longer than a tap may be,
    and 408 when it has not arrived whole within `RECEIVE_SECONDS`."""
    declared = request.headers.get("content-length", "").strip()
    if declared.isascii() and declared.isdigit():
        # Compared as text first: a header can be longer than a number may be.
        digits = declared.lstrip("0")
        if len(digits) > len(str(MAX_BODY_BYTES)) or int(digits or "0") > MAX_BODY_BYTES:
            raise HTTPException(413, _TOO_LARGE)
    body = bytearray()
    late = False
    try:
        async with asyncio.timeout(RECEIVE_SECONDS):
            async for piece in request.stream():
                # Before it is copied, so no more is held than a tap may be.
                if len(body) + len(piece) > MAX_BODY_BYTES:
                    raise HTTPException(413, _TOO_LARGE)
                body += piece
    except TimeoutError:
        late = True
    if late:
        raise HTTPException(408, _TOO_SLOW)
    return bytes(body)


def _tap_row(request: Request, tap_id: str) -> tuple[Taps, Evidence, Snapshot]:
    """The row of a tap, for the one who has its id, and one reading of the
    incidents to answer from. A tap's id is what its sender was given, and nothing
    Beacon shows of an incident names a tap."""
    taps: Taps | None = getattr(request.app.state, "taps", None)
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if taps is None or engine is None:
        raise HTTPException(503, "This server is not taking taps.")
    row = taps.store.find(SOURCE, tap_id) if is_tap_id(tap_id) else None
    if row is None or row.kind != "tap" or row.point is None:
        raise HTTPException(404, _NO_SUCH_TAP)
    snapshot = engine.snapshot()
    if snapshot.as_of - row.occurred_at > ANSWERED:
        raise HTTPException(404, _NO_SUCH_TAP)
    return taps, row, snapshot


def _kept(
    request: Request, row: Evidence, now: datetime
) -> tuple[Run | None, Literal["finished", "pending", "lost", "none"]]:
    """A tap's run and how it stands. With no run: `pending` when one is still to
    come, because the tap was handed in a moment ago or its run is being made;
    `lost` when one was begun and never kept, because the server was stopped under
    it; `none` when none was made and none will be."""
    desk: InvestigationDesk | None = getattr(request.app.state, "investigations", None)
    found = None if desk is None else desk.runs.get(row.ref)
    if found is None:
        waiting = desk is not None and desk.active and now - row.occurred_at < WAITING
        return None, "pending" if waiting else "none"
    claimed, run = found
    if run is not None:
        return run, "finished"
    return None, "pending" if now - claimed < RUNNING else "lost"


@router.get("/taps/{tap_id}")
def tap_answer(request: Request, tap_id: str) -> TapAnswer:
    """The answer to a tap, for whoever has its id: the incident the investigator
    took it to be about, as that incident is now, or that it is unconfirmed, or
    that the investigator has not answered yet. Reading it asks Ember nothing."""
    # One reading for the whole answer.
    taps, row, snapshot = _tap_row(request, tap_id)
    run, state = _kept(request, row, snapshot.as_of)
    outcome: Outcome | None
    if run is not None:
        outcome = run.outcome
    elif state == "pending":
        outcome = None
    else:
        # Nothing is kept of a run that was never made or never ended: it is said
        # anew. One that was begun may have asked Ember something, so it is not
        # said not to have been made.
        reason = "cut_short" if state == "lost" else "not_investigated"
        outcome = Outcome(status="unconfirmed", reason=reason)
    matched = (
        None
        if outcome is None or outcome.incident_id is None
        else snapshot.get(outcome.incident_id)
    )
    unconfirmed = outcome is not None and outcome.status == "unconfirmed"
    watching = unconfirmed and snapshot.as_of - row.occurred_at <= WATCH
    since = _since(request, row, run, snapshot) if watching else []
    nearby = [] if run is None else _nearby(request, run, snapshot)
    stand_in = (run is not None and run.stand_in) or (matched is not None and matched.stand_in)
    return TapAnswer(
        mode=taps.store.mode,
        as_of=snapshot.as_of,
        partial=snapshot.partial,
        tap=TapOut(
            id=row.ref,
            received_at=row.occurred_at,
            photo=row.data.get("photo") is not None,
            clip=row.data.get("clip") is not None,
        ),
        status="pending" if outcome is None else outcome.status,
        reason=None if outcome is None else outcome.reason,
        text=words.PENDING if outcome is None else words.status_text(outcome),
        investigated_at=None if run is None else run.finished_at,
        model=None if run is None else run.model,
        stand_in=stand_in,
        stand_in_text=words.STAND_IN if stand_in else None,
        incident_id=None if outcome is None else outcome.incident_id,
        incident=None if matched is None else summary_of(request, matched),
        incident_gone=_gone(outcome, matched),
        incident_gone_text=words.INCIDENT_GONE if _gone(outcome, matched) else None,
        match=None if run is None or outcome is None else _match(run, outcome, row, matched),
        nearby=nearby,
        nearby_text=words.NEARBY if nearby else None,
        watching=watching,
        since=since,
        since_text=words.SINCE if since else None,
        cost_usd=0.0 if run is None else run.cost_usd,
        ember_calls=0 if run is None else len(run.calls),
    )


def _gone(outcome: Outcome | None, matched: object) -> bool:
    return outcome is not None and outcome.incident_id is not None and matched is None


def _match(run: Run, outcome: Outcome, row: Evidence, matched: Any) -> MatchOut | None:
    if outcome.status != "incident" or outcome.confidence is None:
        return None
    distance = (
        None
        if matched is None or matched.point is None or row.point is None
        else matched.point.distance_m(row.point)
    )
    return MatchOut(
        confidence=outcome.confidence,
        text=words.MATCHED,
        grounds=[
            GroundOut(kind=ground, text=words.ground_text(ground, run, distance))
            for ground in outcome.grounds
        ],
        grounds_dropped=outcome.grounds_dropped,
    )


def _nearby(request: Request, run: Run, snapshot: Snapshot) -> list[IncidentSummary]:
    """The incidents a run's tools found near the tap, nearest first, other than
    the one the tap was taken to be about, as they are in this reading."""
    ids = [
        incident_id
        for round_ in run.rounds
        for call in round_.calls
        if call.tool == "nearby_incidents"
        for incident_id in call.returned.get("incidents") or []
        if incident_id != run.outcome.incident_id
    ]
    found = [snapshot.get(incident_id) for incident_id in dict.fromkeys(ids)]
    return [summary_of(request, incident) for incident in found if incident is not None]


def _since(
    request: Request, row: Evidence, run: Run | None, snapshot: Snapshot
) -> list[IncidentSummary]:
    """The incidents that began near an unconfirmed tap after the investigator
    looked, or after the tap when nobody looked. Found by code, by place and time;
    no one investigated whether one is what the tap was about."""
    looked = row.occurred_at if run is None or run.as_of is None else run.as_of
    return [
        summary_of(request, incident)
        for incident in snapshot.incidents
        if incident.opened_at > looked
        and incident.point is not None
        and row.point is not None
        and incident.point.distance_m(row.point) <= WATCH_M
    ]


@router.get("/taps/{tap_id}/run")
def tap_run(request: Request, tap_id: str) -> TapRun:
    """The investigator's run for a tap, for whoever has its id: each round, the
    tools it called and why, what each returned, and each call's tokens, time, and
    cost. Reading it asks Ember nothing."""
    taps, row, snapshot = _tap_row(request, tap_id)
    run, state = _kept(request, row, snapshot.as_of)
    mode = taps.store.mode
    if run is None:
        return TapRun(mode=mode, tap_id=row.ref, state=state)
    calls = run.calls
    return TapRun(
        mode=mode,
        tap_id=row.ref,
        state="finished",
        started_at=run.started_at,
        finished_at=run.finished_at,
        evidence_as_of=run.as_of,
        model=run.model,
        stand_in=run.stand_in,
        stand_in_text=words.STAND_IN if run.stand_in else None,
        rounds=[
            RoundOut(
                n=round_.n,
                ember=_ember(round_.ember),
                calls=[
                    CallOut(
                        tool=call.tool,
                        thinking=call.thinking,
                        thinking_text=words.thinking_text(call),
                        lead=call.lead,
                        why=call.why,
                        args=dict(call.args),
                        ok=call.ok,
                        text=words.call_text(call),
                        returned=dict(call.returned),
                        returned_text=words.returned_text(call),
                        ember=None if call.ember is None else _ember(call.ember),
                    )
                    for call in round_.calls
                ],
            )
            for round_ in run.rounds
        ],
        final=None if run.final is None else _ember(run.final),
        outcome=OutcomeOut(
            status=run.outcome.status,
            reason=run.outcome.reason,
            text=words.status_text(run.outcome),
            incident_id=run.outcome.incident_id,
            grounds=list(run.outcome.grounds),
            grounds_dropped=run.outcome.grounds_dropped,
            confidence=run.outcome.confidence,
            crisis=run.outcome.crisis,
            crisis_signals=list(run.outcome.crisis_signals),
            crisis_refused=run.outcome.crisis_refused,
        ),
        cut_short=run.cut_short,
        cut_short_text=words.cut_short_text(run),
        ember_calls=len(calls),
        input_tokens=sum(call.input_tokens for call in calls),
        cached_tokens=sum(call.cached_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        reasoning_tokens=sum(call.reasoning_tokens for call in calls),
        cost_usd=run.cost_usd,
        latency_ms=sum(call.latency_ms for call in calls),
    )


def _ember(call: Accounting) -> EmberCallOut:
    return EmberCallOut(
        source=call.source,
        input_tokens=call.input_tokens,
        cached_tokens=call.cached_tokens,
        output_tokens=call.output_tokens,
        reasoning_tokens=call.reasoning_tokens,
        cost_usd=call.cost_usd,
        latency_ms=call.latency_ms,
    )
