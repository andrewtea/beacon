import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from beacon.taps import MAX_CLIP_BYTES, MAX_PHOTO_BYTES, Tap, TapError, Taps

router = APIRouter()

# The most a tap's request may hold: its photo and its clip in base64, which is four
# characters for three bytes, and room for the rest.
MAX_BODY_BYTES = (MAX_PHOTO_BYTES + MAX_CLIP_BYTES) * 4 // 3 + 100_000

_TOO_LARGE = "A tap is at most ten megabytes, with its photo and its clip."
# How long a tap's body may take to arrive. A phone on a poor connection sends ten
# megabytes in less; a sender who sends nothing holds a place for no longer.
RECEIVE_SECONDS = 20.0
_TOO_SLOW = "A tap has to arrive within twenty seconds. Send it again."


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
            work = asyncio.get_running_loop().run_in_executor(None, _take, taps, body, give_back)
        except BaseException:
            give_back()
            raise
        # If this request is called off, the work goes on to its end, and it is
        # the work that gives the place back.
        taken = await asyncio.shield(work)
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
        )
    headers = None if refused.retry_after is None else {"Retry-After": str(refused.retry_after)}
    raise HTTPException(refused.status, str(refused), headers=headers)


def _take(taps: Taps, body: bytes, give_back: Callable[[], None]) -> Tap:
    try:
        return taps.take_body(body)
    finally:
        give_back()


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
