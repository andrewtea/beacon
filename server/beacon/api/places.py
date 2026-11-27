import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from beacon.api.body import read_body
from beacon.api.incidents import IncidentSummary, summary_of
from beacon.api.taps import MODE_HEADER
from beacon.incident import Engine
from beacon.places import (
    MAX_BODY_BYTES,
    NOT_AN_AREA,
    NOTHING_NEAR,
    REACH_M,
    SHAPE,
    Admission,
    PlacesError,
    near,
    parse,
    read_places,
)

router = APIRouter()

# How long a request about places may take to arrive. It is two kilobytes at most.
RECEIVE_SECONDS = 5.0
_TOO_LARGE = "A request about places is at most two kilobytes."
_TOO_SLOW = "A request about places has to arrive within five seconds. Send it again."
_BUSY = "Beacon is busy with other requests about places. Try again in a moment."
_NO_MODE = (
    "A request about places says which mode its sender was shown, in the Beacon-Mode "
    "header: live or mock. This one does not, and nothing of it was read."
)
_OTHER_MODE = (
    "This server is not in the mode the sender was shown: it runs on real sources and "
    "the sender was shown the invented scenario, or the other way round. Nothing of "
    "the request was read. Look at what Beacon shows now, and ask again."
)


class NearOut(BaseModel):
    """An open incident Beacon has within a kilometre of a place."""

    # When `incident.point_source` is not null, the distance is measured from that
    # row's point, which is a 511 closure's: show its credit and its confidence
    # beside `text`. The card's own confidence is its radio calls'.
    incident: IncidentSummary
    # Between the point Beacon has for the incident and the place as it was sent,
    # rounded to 50 m. Zero is nearer than 25 m.
    distance_m: int
    # The distance in a sentence, which says so when it is measured from a point of
    # 511's, and nothing else: not whether the place is in the area of an order or
    # an alert. Show it as it is.
    text: str


class PlaceOut(BaseModel):
    """What Beacon has near one place. The place itself is not repeated: an answer
    is matched to its place by its position in the list."""

    # Nearest first. A medical response, an incident whose record is closed, and
    # one Beacon has no point for are not listed.
    near: list[NearOut]
    # When nothing is listed, the sentence that says so, and that it is no
    # all-clear. It is the same whatever was left out. Null when something is.
    text: str | None


class PlacesAnswer(BaseModel):
    # `mock` when the evidence is the invented scenario's.
    mode: Literal["live", "mock"]
    as_of: datetime
    partial: bool
    # How far from a place an incident is listed, in metres.
    reach_m: int
    # What a distance is and is not: show it with every answer. A distance says
    # nothing of whether a place is inside the area of an official order or alert.
    caveat_text: str
    # One for each place that was sent, in the order they were sent.
    places: list[PlaceOut]


@router.post("/places/check")
async def check(request: Request) -> PlacesAnswer:
    """What Beacon has near each of up to five places, for an app that keeps the
    places itself. Nothing of the request is kept: no row, no file, no log line.

    The places are sent in the body, and not in the address, which is what gets
    written down by whatever a request passes through. The body is read here and
    not by FastAPI, so that nothing of it is repeated in an error. Like a tap, the
    request names the mode its sender was shown (`Beacon-Mode`), and a server in
    the other mode refuses it unread: a place can be a home, and is not sent to
    the invented scenario's server by somebody who was shown a real one.

    Only so many checks are worked on at once (`Admission`): one more is answered
    503, with its body unread."""
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if engine is None:
        raise HTTPException(503, "This server is not reading incidents.")
    shown = request.headers.get(MODE_HEADER)
    if shown not in ("live", "mock"):
        raise HTTPException(400, _NO_MODE)
    if shown != engine.mode:
        raise HTTPException(409, _OTHER_MODE)
    # A place among the checks being worked on is held from before the body is
    # read until the answer is made, so that only so many bodies are in memory and
    # only so many answers are waited for at once. Nobody waits for one.
    admission: Admission = request.app.state.place_checks
    give_back = admission.admit()
    if give_back is None:
        raise HTTPException(503, _BUSY, headers={"Retry-After": "1"})
    try:
        body = await read_body(
            request,
            most=MAX_BODY_BYTES,
            seconds=RECEIVE_SECONDS,
            too_large=_TOO_LARGE,
            too_slow=_TOO_SLOW,
        )
        # Off the event loop: a reading of the incidents can take a moment.
        work = asyncio.get_running_loop().run_in_executor(
            None, _answer, request, engine, body, give_back
        )
    except BaseException:
        give_back()
        raise
    try:
        # If this request is called off, the work goes on to its end, and it is
        # the work that gives the place back.
        return await asyncio.shield(work)
    except PlacesError as error:
        refused = error
    raise HTTPException(refused.status, str(refused))


def _answer(
    request: Request, engine: Engine, body: bytes, give_back: Callable[[], None]
) -> PlacesAnswer:
    try:
        return _read_and_answer(request, engine, body)
    finally:
        give_back()


def _read_and_answer(request: Request, engine: Engine, body: bytes) -> PlacesAnswer:
    sent = parse(body)
    # `places` and nothing else: a name a person gave a place is not Beacon's to
    # be sent.
    if sent.keys() != {"places"}:
        raise PlacesError(422, SHAPE)
    points = read_places(sent["places"])
    # One reading for the whole answer.
    snapshot = engine.snapshot()
    places = []
    for point in points:
        found = near(point, snapshot.incidents)
        places.append(
            PlaceOut(
                near=[
                    NearOut(
                        incident=summary_of(request, item.incident),
                        distance_m=item.distance_m,
                        text=item.text,
                    )
                    for item in found
                ],
                text=None if found else NOTHING_NEAR,
            )
        )
    return PlacesAnswer(
        mode=engine.mode,
        as_of=snapshot.as_of,
        partial=snapshot.partial,
        reach_m=int(REACH_M),
        caveat_text=NOT_AN_AREA,
        places=places,
    )
