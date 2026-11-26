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
from beacon.incident import Engine, open_engine
from beacon.ingest import IngestConfigError, build
from beacon.ingest.setup import PAID_SECONDS, STOP_SECONDS, Ingest
from beacon.radio import core_dump_problem, forbid_core_dumps
from beacon.story import StoryDesk, open_stories
from beacon.taps import Taps, open_taps

_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})

INGEST_VARIABLE = "BEACON_INGEST"
# How often the photos and clips whose hour is over are deleted.
SWEEP_SECONDS = 60.0
# How often the incidents are looked over for one whose story is due.
STORY_SECONDS = 15.0
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


async def _tell_stories(desk: StoryDesk, engine: Engine, stop: asyncio.Event) -> None:
    """Until `stop` is set, have the stories that are due written, every
    `STORY_SECONDS`. A pass that fails is logged by its kind and tried again."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(lambda: desk.refresh_all(engine.snapshot()))
        except Exception as error:
            logger.error("The incidents' stories could not be written (%s).", type(error).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), STORY_SECONDS)


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
    stories: StoryDesk | None = None
    telling: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    # Whatever of this is begun is ended below, however far the beginning got.
    try:
        taps = open_taps(os.environ)
        app.state.taps = taps
        # The incidents are read from the same evidence store the taps go to,
        # which is there whether or not this server runs the pollers.
        engine = open_engine(os.environ, taps.store)
        app.state.incidents = engine
        # What Ember writes about an incident: a stand-in's on the mock store, and
        # on the live store written only under a budget somebody set.
        stories = open_stories(os.environ, engine)
        if stories is not None:
            app.state.stories = stories
            telling = asyncio.create_task(_tell_stories(stories, engine, stop))
        sweeping = asyncio.create_task(_sweep_tap_media(taps, stop))
        if ingest is not None:
            app.state.ingest = ingest
            ingest.start()
        yield
    finally:
        stop.set()
        # Before anything is waited for, nothing new that costs money is admitted:
        # not by the story desk, whose pass may be under way on a thread, and not by
        # the pollers. What is waited for below can take minutes.
        _close_paid(stories, ingest)
        # The pollers are stopped while the rest is, so the wait for a paid answer
        # of theirs and the wait for the story desk's run side by side.
        stopping = asyncio.ensure_future(_stop_ingest(ingest))
        # Each step in a `finally` of its own, so that one that fails does not keep
        # the ones after it from running.
        try:
            # Waited for, not cancelled: a sweep that is under way runs on a thread,
            # which a cancellation would leave running after the taps are closed.
            if sweeping is not None:
                await sweeping
        finally:
            try:
                await _stop_stories(stories, telling)
            finally:
                try:
                    if taps is not None:
                        taps.close()
                finally:
                    await stopping


def _close_paid(stories: StoryDesk | None, ingest: Ingest | None) -> None:
    """Admit no more paid work, anywhere in the server, without waiting for
    anything: a call to Ember that had not sent its paid request is refused from
    here on and costs nothing. The pollers are closed whatever came of closing the
    story desk."""
    closers = [] if stories is None else [stories.close]
    closers += [] if ingest is None else [ingest.close_paid]
    for close in closers:
        try:
            close()
        except Exception as error:
            logger.error("Paid work could not be closed (%s).", type(error).__name__)


async def _stop_stories(stories: StoryDesk | None, telling: asyncio.Task[None] | None) -> None:
    """Stop the story desk: wait for the story whose paid request is out, close the
    stories file, and give the pass that was under way `STOP_SECONDS` to end."""
    if stories is None:
        return
    # Off the event loop. The desk was closed already, so nothing new is asked.
    if not await asyncio.to_thread(stories.stop, PAID_SECONDS):
        logger.warning(
            "A story was still being made when the server shut down. If its paid "
            "Ember request was out, it can still be sent and paid for: it is in the "
            "Ember ledger, and the story is lost."
        )
    if telling is None:
        return
    _done, pending = await asyncio.wait({telling}, timeout=STOP_SECONDS)
    if pending:
        # It can ask Ember nothing and write nothing: the desk and its file are
        # closed. The server does not wait for it any longer.
        telling.cancel()
        await asyncio.wait({telling}, timeout=STOP_SECONDS)
        logger.warning(
            "The pass that has stories made had not ended when the server shut down. "
            "It can ask Ember nothing more and write nothing."
        )


async def _stop_ingest(ingest: Ingest | None) -> None:
    """Stop the pollers. Off the event loop, and not returned from until they can
    ask no source and store no row: nothing of theirs outlives the server. It is
    safe for pollers that were never started."""
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
