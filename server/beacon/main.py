"""ASGI entry point: `uvicorn beacon.main:app`."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from beacon import __version__
from beacon.api import health, sources, taps
from beacon.ingest import IngestConfigError, build
from beacon.taps import Taps, open_taps

_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})

INGEST_VARIABLE = "BEACON_INGEST"
# How often the photos and clips whose hour is over are deleted.
SWEEP_SECONDS = 60.0

logger = logging.getLogger("beacon")


class _NoTapLines(logging.Filter):
    """Leaves the tap route out of uvicorn's access log. A line of that log names
    the address a request came from and the second it came at, which for a tap is
    the one record that could say who tapped: Beacon keeps nothing else that can."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "/api/taps" not in record.getMessage()
        except Exception:
            # A line that cannot be put together cannot be shown not to be a tap's.
            return False


_NO_TAP_LINES = _NoTapLines()


async def _sweep_tap_media(taps: Taps) -> None:
    """Delete the photos and clips whose hour is over, now and every
    `SWEEP_SECONDS` after. Now, for what outstayed its hour while the server was
    down. A file is never handed out past its hour either way (`Taps.media`)."""
    while True:
        try:
            await asyncio.to_thread(taps.sweep)
        except Exception as error:
            # Only the kind of error: its text can name a file, which is a tap's id.
            logger.error("The taps' media could not be swept (%s).", type(error).__name__)
        await asyncio.sleep(SWEEP_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Take taps for as long as the server runs, and run the pollers on a thread of
    their own, unless `BEACON_INGEST=0`. One server process is one set of pollers: a
    second process would ask every source twice as often."""
    setting = os.environ.get(INGEST_VARIABLE, "1")
    if setting not in ("0", "1"):
        raise IngestConfigError(f"{INGEST_VARIABLE} must be 1 or 0, not {setting!r}.")
    # The pollers first: a setting of theirs that is refused leaves no store behind.
    ingest = build(os.environ) if setting == "1" else None
    try:
        taps = open_taps(os.environ)
    except BaseException:
        if ingest is not None:
            ingest.stop()
        raise
    app.state.taps = taps
    sweeping = asyncio.create_task(_sweep_tap_media(taps))
    if ingest is not None:
        app.state.ingest = ingest
        ingest.start()
    try:
        yield
    finally:
        sweeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeping
        taps.close()
        # Off the event loop, and not returned from until the pollers can ask no
        # source and store no row: nothing of theirs outlives the server.
        if ingest is not None and not await asyncio.to_thread(ingest.stop):
            logger.warning(
                "The pollers had not stopped when the server shut down. If a paid Ember "
                "request was out, it can still be sent and paid for: it is in the Ember "
                "ledger, and its row is lost."
            )


async def _validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
    """FastAPI's own reply to a request that fails validation repeats the rejected
    input, which for a tap can be a location, a photo, or a clip from the microphone.
    This one says which part of the request failed and pydantic's code for how, and
    nothing the client sent."""
    problems = []
    for problem in error.errors():
        where = problem["loc"][0] if problem["loc"] else None
        problems.append(f"{where if where in _REQUEST_PARTS else 'request'}: {problem['type']}")
    detail = "The request cannot be used (" + "; ".join(dict.fromkeys(problems)) + ")."
    return JSONResponse({"detail": detail}, status_code=422)


def create_app() -> FastAPI:
    # Every route lives under /api so the built PWA can be served from the same origin.
    app = FastAPI(
        title="Beacon",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        # No OAuth flows, and the default callback route would sit outside /api.
        swagger_ui_oauth2_redirect_url=None,
        lifespan=_lifespan,
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    # The same filter however many apps are made: a logger holds a filter once.
    logging.getLogger("uvicorn.access").addFilter(_NO_TAP_LINES)
    app.include_router(health.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(taps.router, prefix="/api")
    return app


app = create_app()
