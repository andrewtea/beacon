"""ASGI entry point: `uvicorn beacon.main:app`."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from beacon import __version__
from beacon.api import health, incidents, sources, taps
from beacon.incident import open_engine
from beacon.ingest import IngestConfigError, build
from beacon.radio import core_dump_problem, forbid_core_dumps
from beacon.taps import Taps, open_taps

_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})

INGEST_VARIABLE = "BEACON_INGEST"
# How often the photos and clips whose hour is over are deleted.
SWEEP_SECONDS = 60.0
# The routes that take something private. No log line and no telemetry is written
# about a request to one: add a route here when it takes what a person sends.
PRIVATE_PATHS = ("/api/taps",)

logger = logging.getLogger("beacon")


class ServerSetupError(Exception):
    """The server cannot be run on this host as it is. The text says what to change."""


def _is_private(path: object) -> bool:
    """Whether a request's path is one of `PRIVATE_PATHS` or under one, wherever
    the app is mounted: the path a server hands over begins with whatever the app
    is served under (`root_path`), so it is the end of the path that is compared.
    A path that only ends like a private one is taken for one, which costs a log
    line and nothing else."""
    return isinstance(path, str) and any(
        path.endswith(private) or private + "/" in path for private in PRIVATE_PATHS
    )


class _NoPrivateLines(logging.Filter):
    """Leaves the private routes out of uvicorn's access log. A line of that log
    names the address a request came from and the second it came at, which for a
    tap is the one record that could say who tapped: Beacon keeps nothing else
    that can."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            line = record.getMessage()
        except Exception:
            # A line that cannot be put together cannot be shown not to be a tap's.
            return False
        return not any(private in line for private in PRIVATE_PATHS)


class _Nothing(logging.Filter):
    """Lets no line through. It is on uvicorn's log of ASGI messages, which is
    written at its `trace` level and has, for every request, the address it came
    from, its headers' size, and its whole scope, on lines that do not all name the
    route. Nothing Beacon needs is in it."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


class _NoTrace(logging.Filter):
    """Lets nothing through below `DEBUG`. It is on uvicorn's own log, which at its
    `trace` level has a line for every connection that is made and lost, with the
    address it came from, and no word of which route it was for."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.DEBUG


_NO_PRIVATE_LINES = _NoPrivateLines()
_NOTHING = _Nothing()
_NO_TRACE = _NoTrace()


def _not_traced(scope: MutableMapping[str, Any]) -> bool:
    """Whether FastAPI's telemetry leaves a request alone. Its spans carry the
    address a request came from, its browser, and its query, and its logs carry
    exception messages and tracebacks."""
    return _is_private(scope.get("path"))


async def _sweep_tap_media(taps: Taps, stop: asyncio.Event) -> None:
    """Delete the photos and clips whose hour is over, now and every
    `SWEEP_SECONDS` after, until `stop` is set. Now, for what outstayed its hour
    while the server was down. A file is never handed out past its hour either way
    (`Taps.media`). A sweep that is under way when `stop` is set is finished."""
    while not stop.is_set():
        try:
            swept = await asyncio.to_thread(taps.sweep)
        except Exception as error:
            # Only the kind of error: its text can name a file, which is a tap's id.
            logger.error("The taps' media could not be swept (%s).", type(error).__name__)
        else:
            if swept.failed:
                logger.error(
                    "%d files of taps' media are past their time and could not be "
                    "deleted. They are handed to nobody. Look at the taps directory.",
                    swept.failed,
                )
            if swept.unlisted:
                logger.error(
                    "%d directories of taps' media could not be listed, and nothing in "
                    "them was swept. Look at the taps directory.",
                    swept.unlisted,
                )
            if swept.unchecked:
                logger.error(
                    "The evidence store could not say whether %d files of taps' media "
                    "have their rows. They are left until it can, or their hour is over.",
                    swept.unchecked,
                )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SWEEP_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Take taps for as long as the server runs, and run the pollers on a thread of
    their own, unless `BEACON_INGEST=0`. One server process is one set of pollers: a
    second process would ask every source twice as often."""
    setting = os.environ.get(INGEST_VARIABLE, "1")
    if setting not in ("0", "1"):
        raise IngestConfigError(f"{INGEST_VARIABLE} must be 1 or 0, not {setting!r}.")
    # This process holds what people send with a tap in memory, the place as the
    # phone gave it included, from the moment the HTTP server has read a request:
    # before any route is asked. So before a request is served, it is made a
    # process of which no core is written, to a file or to a collector. A process
    # that cannot be marked so still writes no core file, and where cores go to a
    # collector, which that does not stop, the server does not start.
    if not forbid_core_dumps():
        if core_dump_problem() is not None:
            raise ServerSetupError(
                "Beacon does not start on this host as it is. It could not mark itself "
                "as a process of which no core dump is made (prctl PR_SET_DUMPABLE), "
                "and the host hands core dumps to a collector (kernel.core_pattern "
                "begins with | or @, or cannot be read), which a crash would hand what "
                "a tap sent. Set kernel.core_pattern to a file name, such as core. "
                "Until then, `python -m beacon.taps` deletes the media of earlier taps "
                "whose hour is over."
            )
        logger.warning(
            "This server could not mark itself as a process of which no core dump is "
            "made. Its core size limit of nothing keeps a crash from writing a core "
            "file, and this host hands cores to no collector."
        )
    # The pollers first: a setting of theirs that is refused leaves no store behind.
    ingest = build(os.environ) if setting == "1" else None
    taps: Taps | None = None
    sweeping: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    # Whatever of this is begun is ended below, however far the beginning got.
    try:
        taps = open_taps(os.environ)
        app.state.taps = taps
        # The incidents are read from the same evidence store the taps go to,
        # which is there whether or not this server runs the pollers.
        app.state.incidents = open_engine(os.environ, taps.store)
        sweeping = asyncio.create_task(_sweep_tap_media(taps, stop))
        if ingest is not None:
            app.state.ingest = ingest
            ingest.start()
        yield
    finally:
        # Each step in a `finally` of its own, so that one that fails does not keep
        # the ones after it from running.
        try:
            if sweeping is not None:
                # Told to stop and waited for, not cancelled: a sweep that is
                # under way runs on a thread, which a cancellation would leave
                # running after the taps are closed.
                stop.set()
                await sweeping
        finally:
            try:
                if taps is not None:
                    taps.close()
            finally:
                # Off the event loop, and not returned from until the pollers can
                # ask no source and store no row: nothing of theirs outlives the
                # server. It is safe for pollers that were never started.
                if ingest is not None and not await asyncio.to_thread(ingest.stop):
                    logger.warning(
                        "The pollers had not stopped when the server shut down. If a "
                        "paid Ember request was out, it can still be sent and paid "
                        "for: it is in the Ember ledger, and its row is lost."
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
        # FastAPI traces requests by itself once OpenTelemetry is set up in the
        # process. The private routes are left out of it.
        telemetry={"exclude": _not_traced},
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    # The same filters however many apps are made: a logger holds a filter once.
    logging.getLogger("uvicorn.access").addFilter(_NO_PRIVATE_LINES)
    logging.getLogger("uvicorn.asgi").addFilter(_NOTHING)
    logging.getLogger("uvicorn.error").addFilter(_NO_TRACE)
    app.include_router(health.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(taps.router, prefix="/api")
    return app


app = create_app()
