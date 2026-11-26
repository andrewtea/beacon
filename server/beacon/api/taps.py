import asyncio
import contextlib
import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from beacon.taps import MAX_CLIP_BYTES, MAX_PHOTO_BYTES, TapError

router = APIRouter()

# The most a tap's request may hold: its photo and its clip in base64, which is four
# characters for three bytes, and room for the rest.
MAX_BODY_BYTES = (MAX_PHOTO_BYTES + MAX_CLIP_BYTES) * 4 // 3 + 100_000

_TOO_LARGE = "A tap is at most ten megabytes, with its photo and its clip."
_NOT_JSON = "A tap is a JSON object."


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
    taps = getattr(request.app.state, "taps", None)
    if taps is None:
        raise HTTPException(503, "This server is not taking taps.")
    try:
        # A place among the taps being received is held from before the body is
        # read, so that only so many bodies are in memory at once.
        with taps.receiving():
            payload = _json(await _body(request))
            taken = await asyncio.to_thread(taps.take, payload)
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


async def _body(request: Request) -> bytes:
    """The request's body, or a 413 as soon as it is longer than a tap may be."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, _TOO_LARGE)
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, _TOO_LARGE)
    return bytes(body)


def _json(body: bytes) -> Any:
    # A decoder's error quotes what it could not read. This one says nothing, and is
    # raised with that one handled and gone.
    with contextlib.suppress(ValueError, RecursionError):
        return json.loads(body)
    raise HTTPException(422, _NOT_JSON)
