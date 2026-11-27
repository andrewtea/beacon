import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from beacon.api.body import read_body
from beacon.api.taps import MODE_HEADER
from beacon.evidence import Point
from beacon.incident import Engine
from beacon.places import MAX_PLACES, REACH_M, Admission, PlacesError, read_places
from beacon.places.read import ELSEWHERE
from beacon.push import (
    ABOUT,
    KEPT,
    OFF,
    Push,
    SubscriptionsError,
    checked,
    notice_of,
)

router = APIRouter()

# The most a request about notifications may hold: an address of at most a
# kilobyte and five places.
MAX_BODY_BYTES = 4_096
RECEIVE_SECONDS = 5.0
_TOO_LARGE = "A request about notifications is at most four kilobytes."
_TOO_SLOW = "A request about notifications has to arrive within five seconds. Send it again."
_BUSY = "Beacon is busy with other requests about notifications. Try again in a moment."
_NO_MODE = (
    "A request to be notified says which mode its sender was shown, in the "
    "Beacon-Mode header: live or mock. This one does not, and nothing of it was read."
)
_OTHER_MODE = (
    "This server is not in the mode the sender was shown: it runs on real sources and "
    "the sender was shown the invented scenario, or the other way round. Nothing of "
    "the request was read. Look at what Beacon shows now, and ask again."
)
SUBSCRIBE_SHAPE = (
    "A request to be notified is a JSON object with endpoint, the address your "
    "browser's push service gave this device, and places: a list of one to five "
    "places, each with lat and lon in degrees and nothing else."
)
UNSUBSCRIBE_SHAPE = (
    "A request to stop notifications is a JSON object with endpoint, the address "
    "your browser's push service gave this device, and nothing else."
)
NOT_A_PUSH_SERVICE = (
    "Beacon sends notifications only through the push services of Chrome, Edge, "
    "Firefox, and Safari, and this address is not one of theirs."
)
FULL = "Beacon keeps as many addresses to notify as it can, and can take no more just now."
_CLOSED = "This server is stopping, and takes no more requests about notifications."


class NoticeOut(BaseModel):
    """What a device shows when a push arrives. A push carries no words, so the
    app keeps these for its service worker."""

    title: str
    text: str


class PushInfo(BaseModel):
    """What the app needs to ask a person whether they want notifications."""

    # `mock` when the incidents are the invented scenario's.
    mode: Literal["live", "mock"]
    # Whether this server sends notifications. When it does not, `text` says so
    # and the rest is null.
    enabled: bool
    text: str | None
    # The key a browser subscribes with (`applicationServerKey`), in URL-safe
    # base64. The mock store and the live store have a key each.
    public_key: str | None
    # What Beacon keeps to notify a device, when it notifies, and who learns
    # what. Show it before a person turns notifications on.
    about_text: str | None
    # The words to show when a push arrives from this server: a push carries
    # none, so the app keeps these on the device before it subscribes. On the
    # mock store they say that the incident is invented and a stand-in decided.
    notice: NoticeOut | None
    # How far from a place an incident may be for a notification, in metres, and
    # how many places a device may name.
    reach_m: int
    max_places: int
    # How many days a subscription is kept after it was last renewed.
    kept_days: int


class Subscribed(BaseModel):
    mode: Literal["live", "mock"]
    # When the subscription is deleted if it is not renewed before.
    kept_until: datetime


@router.get("/push")
async def info(request: Request) -> PushInfo:
    """Whether this server sends notifications, and what a person is told before
    they turn them on. It reads nothing and keeps nothing."""
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if engine is None:
        raise HTTPException(503, "This server is not reading incidents.")
    push: Push | None = getattr(request.app.state, "push", None)
    notice = None if push is None else notice_of(push.mode)
    return PushInfo(
        mode=engine.mode,
        enabled=push is not None,
        text=None if push is not None else OFF,
        public_key=None if push is None else push.vapid.public_key,
        about_text=None if push is None else ABOUT,
        notice=None if notice is None else NoticeOut(title=notice.title, text=notice.text),
        reach_m=int(REACH_M),
        max_places=MAX_PLACES,
        kept_days=KEPT.days,
    )


@router.post("/push/subscribe")
async def subscribe(request: Request) -> Subscribed:
    """Keep, or renew, the subscription of one device: the address its browser's
    push service gave it, and the places it wants to be notified about, which take
    the place of any it named before.

    This is the one thing Beacon keeps that is tied to a device, and it keeps only
    that: the address, and each place rounded to about a block. The body is read
    here and not by FastAPI, so that nothing of it is repeated in an error, and no
    log line is written about the request. Like a tap, it names the mode its
    sender was shown (`Beacon-Mode`), and a server in the other mode refuses it
    unread."""
    push = _push(request)
    shown = request.headers.get(MODE_HEADER)
    if shown not in ("live", "mock"):
        raise HTTPException(400, _NO_MODE)
    if shown != push.mode:
        raise HTTPException(409, _OTHER_MODE)
    return await _admitted(request, lambda body: _subscribe(push, body))


@router.post("/push/unsubscribe", status_code=204)
async def unsubscribe(request: Request) -> Response:
    """Delete the subscription kept under an address, and what was sent to it. The
    answer is the same whether or not there was one."""
    push = _push(request)
    await _admitted(request, lambda body: _unsubscribe(push, body))
    return Response(status_code=204)


def _push(request: Request) -> Push:
    push: Push | None = getattr(request.app.state, "push", None)
    if push is None:
        raise HTTPException(503, OFF)
    return push


async def _admitted[T](request: Request, work: Callable[[bytes], T]) -> T:
    """Read the request's body and do `work` on it, off the event loop, as one of
    the few requests about notifications that are worked on at once: one more is
    answered 503 with its body unread. A refusal is answered with its fixed
    sentence."""
    admission: Admission = request.app.state.push_requests
    give_back = admission.admit()
    if give_back is None:
        raise HTTPException(503, _BUSY, headers={"Retry-After": "1"})

    def working(body: bytes) -> T:
        try:
            return work(body)
        finally:
            give_back()

    try:
        body = await read_body(
            request,
            most=MAX_BODY_BYTES,
            seconds=RECEIVE_SECONDS,
            too_large=_TOO_LARGE,
            too_slow=_TOO_SLOW,
        )
        doing = asyncio.get_running_loop().run_in_executor(None, working, body)
    except BaseException:
        give_back()
        raise
    refused: PlacesError | None = None
    closed = False
    try:
        # If this request is called off, the work goes on to its end, and it is
        # the work that gives the place back.
        return await asyncio.shield(doing)
    except PlacesError as error:
        refused = error
    except SubscriptionsError:
        closed = True
    # Raised out here, so that nothing is left under the error that answers.
    if closed or refused is None:
        raise HTTPException(503, _CLOSED)
    raise HTTPException(refused.status, str(refused))


def _parse(body: bytes, fields: frozenset[str], shape: str) -> Mapping[str, Any]:
    """The JSON object of a request's body, with exactly `fields`. A body with more
    structure than such a request has is not parsed. Raises PlacesError, whose text
    is fixed and repeats nothing."""
    found: Any = None
    # An address holds no bracket or brace, so these are all the body's own.
    if body.count(b"{") <= MAX_PLACES + 1 and body.count(b"[") <= 1:
        # A decoder's error quotes what it could not read. It is handled and gone
        # before anything is raised.
        with contextlib.suppress(ValueError, RecursionError):
            found = json.loads(body.decode("utf-8"))
    if not isinstance(found, Mapping) or found.keys() != fields:
        raise PlacesError(422, shape)
    return found


def _subscribe(push: Push, body: bytes) -> Subscribed:
    sent = _parse(body, frozenset({"endpoint", "places"}), SUBSCRIBE_SHAPE)
    if not isinstance(sent["endpoint"], str):
        raise PlacesError(422, SUBSCRIBE_SHAPE)
    endpoint = checked(sent["endpoint"])
    if endpoint is None:
        raise PlacesError(422, NOT_A_PUSH_SERVICE)
    refused: PlacesError | None = None
    points: tuple[Point, ...] = ()
    try:
        # The same list of places as a check of them takes.
        points = read_places(sent["places"])
    except PlacesError as error:
        refused = error
    if refused is not None:
        # Under this request's own sentence when it is the shape that is wrong.
        elsewhere = str(refused) == ELSEWHERE
        raise PlacesError(refused.status, ELSEWHERE if elsewhere else SUBSCRIBE_SHAPE)
    kept_until = push.subscriptions.put(endpoint, points, datetime.now(UTC))
    if kept_until is None:
        raise PlacesError(503, FULL)
    return Subscribed(mode=push.mode, kept_until=kept_until)


def _unsubscribe(push: Push, body: bytes) -> None:
    sent = _parse(body, frozenset({"endpoint"}), UNSUBSCRIBE_SHAPE)
    # An address Beacon would not have kept is not looked for.
    endpoint = checked(sent["endpoint"])
    if endpoint is not None:
        push.subscriptions.remove(endpoint)
