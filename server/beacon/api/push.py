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
    PERMIT_LIFE,
    PERMIT_SHAPE,
    SPENT,
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
    "A request about notifications says which mode its sender was shown, in the "
    "Beacon-Mode header: live or mock. This one does not, and nothing of it was read."
)
_OTHER_MODE = (
    "This server is not in the mode the sender was shown: it runs on real sources and "
    "the sender was shown the invented scenario, or the other way round. Nothing of "
    "the request was read, and nothing was kept or deleted. Look at what Beacon shows "
    "now, and ask again."
)
SUBSCRIBE_SHAPE = (
    "A request to be notified is a JSON object with endpoint, the address your "
    "browser's push service gave this device; places, a list of one to five "
    "places, each with lat and lon in degrees and nothing else; and permit, the "
    "permit this server gave for the request."
)
UNSUBSCRIBE_SHAPE = (
    "A request to stop notifications is a JSON object with endpoint, the address "
    "your browser's push service gave this device, and, when requests to be "
    "notified are still out, permits: a list of their permits."
)
PERMIT_REQUEST_SHAPE = (
    "A request for a permit is a JSON object with revoke: a list of the permits "
    "of earlier requests, which can be empty."
)
# The most permits one request names: far more than a device ever has out.
MAX_NAMED_PERMITS = 16
NO_PERMITS = (
    "Beacon has as many requests to be notified under way as it takes at once. Try "
    "again in a moment."
)
PERMIT_SPENT = (
    "This request to be notified came without a permit that is still good: the "
    "permit was used, withdrawn, or its time is over. Nothing of the request was "
    "kept."
)
# The status a request with such a permit is refused with. Not 409, which says
# that the server is in another mode.
PERMIT_SPENT_STATUS = 410
NOT_A_PUSH_SERVICE = (
    "Beacon sends notifications only through the push services of Chrome, Edge, "
    "Firefox, and Safari, and this address is not one of theirs."
)
FULL = "Beacon keeps as many addresses to notify as it can, and can take no more just now."
_CLOSED = "This server is stopping, and takes no more requests about notifications."
_NOT_LOOKED = (
    "This server has not opened the file notifications' addresses are kept in, so it "
    "has looked for nothing and deleted nothing. Ask again."
)


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
    # what. Show it before a person turns notifications on. Its paragraphs are
    # parted by a blank line.
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


class Permit(BaseModel):
    mode: Literal["live", "mock"]
    # What one request to be notified names, once, to have its address kept.
    permit: str
    # How long it is good for, in seconds.
    seconds: int


class Subscribed(BaseModel):
    mode: Literal["live", "mock"]
    # When the subscription is deleted if it is not renewed before.
    kept_until: datetime


@router.get("/push")
async def info(request: Request) -> PushInfo:
    """Whether this server sends notifications, and what a person is told before
    they turn them on. It reads nothing and keeps nothing."""
    engine = _engine(request)
    push: Push | None = getattr(request.app.state, "push", None)
    vapid = None if push is None or not push.sends else push.vapid
    notice = None if vapid is None else notice_of(engine.mode)
    return PushInfo(
        mode=engine.mode,
        enabled=vapid is not None,
        text=None if vapid is not None else OFF,
        public_key=None if vapid is None else vapid.public_key,
        about_text=None if vapid is None else ABOUT,
        notice=None if notice is None else NoticeOut(title=notice.title, text=notice.text),
        reach_m=int(REACH_M),
        max_places=MAX_PLACES,
        kept_days=KEPT.days,
    )


@router.post("/push/permit")
async def permit(request: Request) -> Permit:
    """A permit for one request to be notified, good once and for two minutes.

    **It is what makes a deletion final.** A request to be notified can reach
    this server after its sender gave up on it and was told that its address is
    deleted, and it would put the address back. So a request to be notified
    names a permit, which is used up in the write that keeps the address, and a
    request to delete names the permits of the requests its sender has out,
    which are taken away in the write that deletes. `revoke` does the same for
    the permits of this sender's earlier requests, so an older list of places
    cannot follow a newer one. A permit is a random number beside no address and
    no place. Like the other requests about notifications, this one names the
    mode its sender was shown and is in no log."""
    _shown(request)
    push: Push | None = getattr(request.app.state, "push", None)
    if push is None or not push.sends:
        raise HTTPException(503, OFF)
    sending = push
    return await _admitted(request, lambda body: _permit(sending, body))


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
    unread.

    It names a permit (`POST /api/push/permit`), and keeps nothing without one
    that is still good: `410` then, which is what a request gets that comes after
    its sender had the address deleted, or after a later request of its."""
    _shown(request)
    push: Push | None = getattr(request.app.state, "push", None)
    if push is None or not push.sends:
        raise HTTPException(503, OFF)
    sending = push
    return await _admitted(request, lambda body: _subscribe(sending, body))


@router.post("/push/unsubscribe", status_code=204)
async def unsubscribe(request: Request) -> Response:
    """Delete the subscription kept under an address, and what was sent to it.

    **`204` means that this mode's file does not hold the address**: it was
    deleted, or was never there. Any other answer means nothing was deleted, and
    the app asks again: `409` from a server in the other mode than the request
    names (`Beacon-Mode`), whose file is another one, and `503` from a server
    that is busy or stopping. The answer is the same whether or not there was a
    subscription, so it tells nobody whether an address is kept. A server that
    sends no notifications deletes as any other does, and one that has not the
    file open answers `503`: it never says `204` without having looked.

    **And the address is not put back by a request that was already on its
    way**, when the sender names that request's permit in `permits`: the permits
    are taken away in the write that deletes, and a request to be notified keeps
    nothing without its permit."""
    _shown(request)
    push: Push | None = getattr(request.app.state, "push", None)
    if push is None:
        raise HTTPException(503, _NOT_LOOKED)
    kept = push
    await _admitted(request, lambda body: _unsubscribe(kept, body))
    return Response(status_code=204)


def _engine(request: Request) -> Engine:
    engine: Engine | None = getattr(request.app.state, "incidents", None)
    if engine is None:
        raise HTTPException(503, "This server is not reading incidents.")
    return engine


def _shown(request: Request) -> None:
    """Refuse a request that does not name the mode this server is in, before any
    of it is read."""
    shown = request.headers.get(MODE_HEADER)
    if shown not in ("live", "mock"):
        raise HTTPException(400, _NO_MODE)
    if shown != _engine(request).mode:
        raise HTTPException(409, _OTHER_MODE)


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


def _parse(
    body: bytes,
    fields: frozenset[str],
    shape: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    """The JSON object of a request's body, with exactly `fields` and any of
    `optional`. A body with more structure than such a request has is not parsed.
    Raises PlacesError, whose text is fixed and repeats nothing."""
    found: Any = None
    # An address holds no bracket or brace, so these are all the body's own.
    if body.count(b"{") <= MAX_PLACES + 1 and body.count(b"[") <= 1:
        # A decoder's error quotes what it could not read. It is handled and gone
        # before anything is raised.
        with contextlib.suppress(ValueError, RecursionError):
            found = json.loads(body.decode("utf-8"))
    if not isinstance(found, Mapping) or not fields <= found.keys() <= fields | optional:
        raise PlacesError(422, shape)
    return found


def _permits(sent: Any, shape: str) -> tuple[str, ...]:
    """The permits a request names. Raises PlacesError for anything that is not a
    short list of things that look like permits."""
    if (
        not isinstance(sent, list)
        or len(sent) > MAX_NAMED_PERMITS
        or not all(isinstance(each, str) and PERMIT_SHAPE.fullmatch(each) for each in sent)
    ):
        raise PlacesError(422, shape)
    return tuple(sent)


def _permit(push: Push, body: bytes) -> Permit:
    sent = _parse(body, frozenset({"revoke"}), PERMIT_REQUEST_SHAPE)
    revoke = _permits(sent["revoke"], PERMIT_REQUEST_SHAPE)
    made = push.subscriptions.permit(datetime.now(UTC), revoke)
    if made is None:
        raise PlacesError(503, NO_PERMITS)
    return Permit(mode=push.mode, permit=made, seconds=int(PERMIT_LIFE.total_seconds()))


def _subscribe(push: Push, body: bytes) -> Subscribed:
    sent = _parse(body, frozenset({"endpoint", "places", "permit"}), SUBSCRIBE_SHAPE)
    if not isinstance(sent["endpoint"], str):
        raise PlacesError(422, SUBSCRIBE_SHAPE)
    (permit,) = _permits([sent["permit"]], SUBSCRIBE_SHAPE)
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
    kept_until = push.subscriptions.put(endpoint, points, datetime.now(UTC), permit=permit)
    if kept_until is None:
        raise PlacesError(503, FULL)
    if kept_until == SPENT:
        raise PlacesError(PERMIT_SPENT_STATUS, PERMIT_SPENT)
    return Subscribed(mode=push.mode, kept_until=kept_until)


def _unsubscribe(push: Push, body: bytes) -> None:
    sent = _parse(body, frozenset({"endpoint"}), UNSUBSCRIBE_SHAPE, optional=frozenset({"permits"}))
    permits = _permits(sent.get("permits", []), UNSUBSCRIBE_SHAPE)
    # An address Beacon would not have kept is not looked for.
    endpoint = checked(sent["endpoint"])
    if endpoint is not None or permits:
        push.subscriptions.remove(endpoint, permits)
