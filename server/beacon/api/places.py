import asyncio
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from beacon.api.body import read_body
from beacon.api.incidents import CreditOut, IncidentSummary, summary_of
from beacon.api.taps import MODE_HEADER
from beacon.incident import Engine
from beacon.incident.build import CREDIT_511
from beacon.places import (
    MAX_BODY_BYTES,
    NOT_AN_AREA,
    NOTHING_NEAR,
    REACH_M,
    SHAPE,
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

    incident: IncidentSummary
    # Between the point Beacon has for the incident and the place as it was sent,
    # rounded to 50 m. Zero is nearer than 25 m.
    distance_m: int
    # The distance in a sentence, and nothing else: not whether the place is in
    # the area of an order or an alert. Show it as it is.
    text: str


class PlaceOut(BaseModel):
    """What Beacon has near one place. The place itself is not repeated: an answer
    is matched to its place by its position in the list."""

    # Nearest first. A medical response, an incident whose record is closed, and
    # one Beacon has no place for are not listed.
    near: list[NearOut]
    # When nothing is listed, the sentence that says so, and that it is no
    # all-clear. Null when something is.
    text: str | None
    # 511's credit, when the place Beacon has for one of `near` is a 511
    # closure's: the distance is measured from it. Show it with the place's answer.
    credit: CreditOut | None


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
    the invented scenario's server by somebody who was shown a real one."""
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if engine is None:
        raise HTTPException(503, "This server is not reading incidents.")
    shown = request.headers.get(MODE_HEADER)
    if shown not in ("live", "mock"):
        raise HTTPException(400, _NO_MODE)
    if shown != engine.mode:
        raise HTTPException(409, _OTHER_MODE)
    body = await read_body(
        request,
        most=MAX_BODY_BYTES,
        seconds=RECEIVE_SECONDS,
        too_large=_TOO_LARGE,
        too_slow=_TOO_SLOW,
    )
    try:
        # Off the event loop: a reading of the incidents can take a moment.
        return await asyncio.to_thread(_answer, request, engine, body)
    except PlacesError as error:
        refused = error
    raise HTTPException(refused.status, str(refused))


def _answer(request: Request, engine: Engine, body: bytes) -> PlacesAnswer:
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
        from_511 = any(item.incident.point_from == "closure" for item in found)
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
                credit=CreditOut.model_validate(CREDIT_511) if from_511 else None,
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
