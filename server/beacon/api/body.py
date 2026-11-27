"""Reading the body of a request that holds something private.

A route that takes what a person sends reads its body itself, with this, and not
through FastAPI: nothing about the body goes through a reader that would repeat it
in an error, no more of it is held than the route takes, and a sender who sends
nothing is not waited for.
"""

import asyncio

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

# What a sender that went away before its request had arrived is answered. Nobody
# reads it: it is here so that the request ends as one that was handled, and
# nothing is logged about an error nobody caught.
_GONE = "The request ended before all of it had arrived."


async def read_body(
    request: Request, *, most: int, seconds: float, too_large: str, too_slow: str
) -> bytes:
    """The request's body. Answers 413, with `too_large`, as soon as it is longer
    than `most` bytes, 408, with `too_slow`, when it has not arrived whole within
    `seconds`, and 400 when its sender went away before it had."""
    declared = request.headers.get("content-length", "").strip()
    if declared.isascii() and declared.isdigit():
        # Compared as text first: a header can be longer than a number may be.
        digits = declared.lstrip("0")
        if len(digits) > len(str(most)) or int(digits or "0") > most:
            raise HTTPException(413, too_large)
    body = bytearray()
    late = gone = False
    try:
        async with asyncio.timeout(seconds):
            async for piece in request.stream():
                # Before it is copied, so no more is held than the route takes.
                if len(body) + len(piece) > most:
                    raise HTTPException(413, too_large)
                body += piece
    except TimeoutError:
        late = True
    except ClientDisconnect:
        gone = True
    # Raised out here, so that neither error is left under the one that answers.
    if late:
        raise HTTPException(408, too_slow)
    if gone:
        raise HTTPException(400, _GONE)
    return bytes(body)
