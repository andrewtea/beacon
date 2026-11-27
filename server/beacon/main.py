"""ASGI entry point: `uvicorn beacon.main:app`."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from beacon import __version__, front
from beacon.api import health, incidents, places, push, scale, sources, taps
from beacon.datadir import DataLock
from beacon.ember.client import default_data_dir
from beacon.incident import Engine, open_engine
from beacon.ingest import IngestConfigError, build
from beacon.ingest.setup import PAID_SECONDS, STOP_SECONDS, Ingest
from beacon.investigate import InvestigationDesk, Runs, RunsError, open_investigations
from beacon.places import Admission
from beacon.push import Push, SubscriptionsError, open_push
from beacon.radio import core_dump_problem, forbid_core_dumps
from beacon.story import StoryDesk, open_stories
from beacon.taps import Taps, open_taps

_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})

INGEST_VARIABLE = "BEACON_INGEST"
# The setting that names the directory of the built app (`web/dist`), which the
# server then serves from its own origin, beside `/api`. Unset, it serves the API
# alone, and something else serves the app.
WEB_VARIABLE = "BEACON_WEB_DIR"
# How often the photos and clips whose hour is over are deleted.
SWEEP_SECONDS = 60.0
# How often the incidents are looked over for one whose story is due.
STORY_SECONDS = 15.0
# The routes that take something private. No log line and no telemetry is written
# about a request to one: add a route here when it takes what a person sends.
PRIVATE_PATHS = ("/api/taps", "/api/places", "/api/push")
# How often the incidents are looked over for a device that is due a push.
PUSH_SECONDS = 15.0

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


async def _sweep_runs(runs: Runs, stop: asyncio.Event) -> None:
    """Delete the investigations that are older than they are kept, now and every
    `SWEEP_SECONDS` after, until `stop` is set: they go when their day is over,
    whether or not another tap comes."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(runs.sweep, datetime.now(UTC))
        except RunsError:
            # The file was closed: the server is shutting down.
            return
        except Exception as error:
            logger.error("The investigations could not be swept (%s).", type(error).__name__)
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


async def _send_pushes(sending: Push, engine: Engine, stop: asyncio.Event) -> None:
    """Until `stop` is set, every `PUSH_SECONDS`: delete the subscriptions whose
    time is over, and send the pushes that are due when this server sends any. A
    pass is logged in counts and by the kind of its error, never with an address,
    a place, or an incident."""
    while not stop.is_set():
        try:
            done = await asyncio.to_thread(lambda: sending.work(engine.snapshot, datetime.now(UTC)))
        except SubscriptionsError:
            # The file was closed: the server is shutting down.
            return
        except Exception as error:
            logger.error("The pushes that are due could not be sent (%s).", type(error).__name__)
        else:
            if done.unswept:
                logger.warning(
                    "%d push subscriptions files could not be swept of what ran out. "
                    "The next pass tries again.",
                    done.unswept,
                )
            if not done.quiet:
                # A refusal is the operator's to look at (a contact a push
                # service does not take, say), so it is a warning, which is
                # printed where a pass that went well is not.
                logger.log(
                    logging.WARNING if done.failed else logging.INFO,
                    "Pushes: %d sent, %d addresses gone and deleted, %d to try again, "
                    "%d refused%s.",
                    done.sent,
                    done.gone,
                    done.again,
                    done.failed,
                    ", and it stopped before it had looked at every subscription"
                    if done.more
                    else "",
                )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), PUSH_SECONDS)


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
    # Before the first store is opened, and until the last worker has stopped: this
    # process has the data directory's stores, alone when it runs the pollers. A
    # process that tells the invented story again (`beacon.serve`) holds the lock
    # already, over every telling, and hands it over.
    given: DataLock | None = getattr(app.state, "data_lock", None)
    if given is not None and not (given.held and (given.exclusive or setting == "0")):
        raise ServerSetupError("The lock this server was handed is not one it can run under.")
    ours = None
    if given is None:
        ours = DataLock(default_data_dir(os.environ), shared=setting == "0")
    try:
        async with _running(app, setting):
            yield
    finally:
        # Given up only when everything that was begun is known to have ended. A
        # worker that did not stop in the time it was given may still have a store
        # open, so the lock is then kept until the process is gone, which is when
        # the operating system gives it up: nothing else opens a store under it.
        if ours is not None and getattr(app.state, "stopped_whole", None) is not False:
            ours.close()


@asynccontextmanager
async def _running(app: FastAPI, setting: str) -> AsyncIterator[None]:
    """Everything a server opens and runs, begun in order and ended whatever
    became of the beginning. `_lifespan` holds the stores' lock around it."""
    # The pollers first: a setting of theirs that is refused leaves no store behind.
    ingest = build(os.environ) if setting == "1" else None
    taps: Taps | None = None
    sweeping: asyncio.Task[None] | None = None
    stories: StoryDesk | None = None
    telling: asyncio.Task[None] | None = None
    investigations: InvestigationDesk | None = None
    runs: Runs | None = None
    sweeping_runs: asyncio.Task[None] | None = None
    sending: Push | None = None
    pushing: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    # Whatever of this is begun is ended below, however far the beginning got.
    try:
        # The scale run whose results this server shows, when it names one. Only
        # its results are read: the run is made by a command, apart from the server.
        app.state.scale = scale.open_published(os.environ)
        taps = open_taps(os.environ)
        app.state.taps = taps
        # The incidents are read from the same evidence store the taps go to,
        # which is there whether or not this server runs the pollers.
        engine = open_engine(os.environ, taps.store)
        if app.state.input == "invented" and engine.mode != "mock":
            # Two invented places are of no use to a server on real sources, and
            # it is not to take a visitor's own through a host nobody has checked.
            raise ServerSetupError(front.NOT_WITH_A_REAL_SOURCE)
        app.state.incidents = engine
        # What Ember writes about an incident: a stand-in's on the mock store, and
        # on the live store written only under a budget somebody set.
        stories = open_stories(os.environ, engine)
        if stories is not None:
            app.state.stories = stories
            telling = asyncio.create_task(_tell_stories(stories, engine, stop))
        # What Ember finds out about a tap: a stand-in's on the mock store, and on
        # the live store found out only under a budget somebody set. What was found
        # out before is read either way.
        runs, investigations = open_investigations(os.environ, engine, taps)
        app.state.runs = runs
        if investigations is not None:
            app.state.investigations = investigations
        sweeping_runs = asyncio.create_task(_sweep_runs(runs, stop))
        sweeping = asyncio.create_task(_sweep_tap_media(taps, stop))
        # Notifications, for the devices that asked for them: sent when a decision
        # that was made turned crisis mode on near a place a device named. It reads
        # the decisions and asks Ember nothing.
        sending = open_push(os.environ, engine, runs)
        app.state.push = sending
        if sending.off is not None:
            logger.warning(
                "This server sends no notifications (%s). The subscriptions an "
                "earlier one kept are deleted when their devices ask and when "
                "their time is over.",
                sending.off,
            )
        if sending.unswept:
            logger.warning(
                "The other mode's push subscriptions file could not be opened by "
                "this Beacon, so what it holds is not deleted when its time is over."
            )
        # Run on a server that sends nothing too: it deletes what ran out.
        pushing = asyncio.create_task(_send_pushes(sending, engine, stop))
        if ingest is not None:
            app.state.ingest = ingest
            ingest.start()
        yield
    finally:
        # Whether everything that was begun is known to have ended. It is said to
        # have only at the very end, when every step below said so of its own.
        app.state.stopped_whole = False
        whole: list[bool] = []
        stop.set()
        # Before anything is waited for, nothing new that costs money is admitted:
        # not by the story desk, whose pass may be under way on a thread, not by the
        # investigator, whose runs are, and not by the pollers. What is waited for
        # below can take minutes.
        _close_paid(stories, investigations, ingest)
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
                whole.append(await _stop_stories(stories, telling))
            finally:
                try:
                    try:
                        # Before the investigations file is closed: a pass reads
                        # the decisions in it.
                        whole.append(await _stop_pushes(sending, pushing))
                    finally:
                        # Before the taps are closed: a run reads a tap's photo
                        # and clip.
                        whole.append(
                            await _stop_investigations(investigations, runs, sweeping_runs)
                        )
                finally:
                    try:
                        if taps is not None:
                            taps.close()
                    finally:
                        whole.append(await stopping)
        # Reached only when no step raised: one that did leaves this unsaid.
        app.state.stopped_whole = len(whole) == 4 and all(whole)


def _close_paid(
    stories: StoryDesk | None, investigations: InvestigationDesk | None, ingest: Ingest | None
) -> None:
    """Admit no more paid work, anywhere in the server, without waiting for
    anything: a call to Ember that had not sent its paid request is refused from
    here on and costs nothing. Each is closed whatever came of closing the ones
    before it."""
    closers = [] if stories is None else [stories.close]
    closers += [] if investigations is None else [investigations.close]
    closers += [] if ingest is None else [ingest.close_paid]
    for close in closers:
        try:
            close()
        except Exception as error:
            logger.error("Paid work could not be closed (%s).", type(error).__name__)


async def _stop_stories(stories: StoryDesk | None, telling: asyncio.Task[None] | None) -> bool:
    """Stop the story desk: wait for the story whose paid request is out, close the
    stories file, and give the pass that was under way `STOP_SECONDS` to end.
    True when nothing of it was left running."""
    if stories is None:
        return True
    whole = True
    # Off the event loop. The desk was closed already, so nothing new is asked.
    if not await asyncio.to_thread(stories.stop, PAID_SECONDS):
        whole = False
        logger.warning(
            "A story was still being made when the server shut down. If its paid "
            "Ember request was out, it can still be sent and paid for: it is in the "
            "Ember ledger, and the story is lost."
        )
    if telling is None:
        return whole
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
        return False
    return whole


async def _stop_investigations(
    investigations: InvestigationDesk | None,
    runs: Runs | None,
    sweeping: asyncio.Task[None] | None,
) -> bool:
    """Stop the investigator: wait for the runs whose paid requests are out, end
    its threads, and close the investigations file, which is closed as well when
    there is no investigator and it was only read. True when nothing of it was
    left running."""
    try:
        # A sweep that is under way is finished, as the sweep of the taps' media is.
        if sweeping is not None:
            await sweeping
    finally:
        if investigations is None and runs is not None:
            runs.close()
    if investigations is None:
        return True
    # Off the event loop. The desk was closed already, so nothing new is asked.
    if not await asyncio.to_thread(investigations.stop, PAID_SECONDS):
        logger.warning(
            "A tap was still being investigated when the server shut down. If a paid "
            "Ember request of its was out, it can still be sent and paid for: it is in "
            "the Ember ledger, and the tap's answer says its investigation was cut "
            "short and that its totals may be incomplete."
        )
        return False
    return True


async def _stop_pushes(sending: Push | None, pushing: asyncio.Task[None] | None) -> bool:
    """Stop sending pushes: end the one that is out, keep no more subscriptions,
    and give the pass that was under way `STOP_SECONDS` to end. A push that was
    out when its answer was cut off is tried again by a later server, once its
    claim is old enough (`push.store.CLAIM_STALE`), whether or not the push
    service had taken it: sending is best effort. True when nothing of it was
    left running."""
    if sending is None:
        return True
    sending.close()
    if pushing is None:
        return True
    _done, pending = await asyncio.wait({pushing}, timeout=STOP_SECONDS)
    if pending:
        # It can send nothing and write nothing: its client and its file are closed.
        pushing.cancel()
        await asyncio.wait({pushing}, timeout=STOP_SECONDS)
        logger.warning("The pass that sends pushes had not ended when the server shut down.")
        return False
    return True


async def _stop_ingest(ingest: Ingest | None) -> bool:
    """Stop the pollers. Off the event loop, and not returned from until they can
    ask no source and store no row: nothing of theirs outlives the server. It is
    safe for pollers that were never started. True when they stopped whole."""
    if ingest is not None and not await asyncio.to_thread(ingest.stop):
        logger.warning(
            "The pollers had not stopped when the server shut down. If a paid Ember "
            "request was out, it can still be sent and paid for: it is in the Ember "
            "ledger, and its row is lost."
        )
        return False
    return True


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


class _Web(StaticFiles):
    """The built app's files. A file under `assets/` has a digest of what it holds
    in its name and is kept by a browser for good. Every other file (the page, the
    service workers, the manifest) is asked about each time, so that a browser has
    the app, and the worker that shows a notification, as they were last deployed."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        kept = Path(path).parts[:1] == ("assets",) and response.status_code == 200
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if kept else "no-cache"
        )
        return response


class _WebMount(Mount):
    """Where the built app's files are served: every path that is not `/api` or
    under it. A mount at `/` would otherwise match every request, and the router
    answers with a full match before it looks at anything else, so a request to
    `/api` that no route takes whole (another method, a trailing slash, a path
    nobody has) would be answered as a file that is not there, and a file named
    `api/...` in the directory would be served. Those requests are not this
    mount's at all: the API answers them as it does with no app beside it."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        # The path as the router reads it: without whatever the app is served under.
        path, under = scope.get("path", ""), scope.get("root_path", "")
        if under and path.startswith(under + "/"):
            path = path[len(under) :]
        if path == "/api" or path.startswith("/api/"):
            return Match.NONE, {}
        return super().matches(scope)


def _web_directory() -> Path | None:
    """The directory of the built app that `BEACON_WEB_DIR` names, or None.

    Raises ServerSetupError when it names no built app."""
    named = os.environ.get(WEB_VARIABLE)
    if not named:
        return None
    directory = Path(named)
    if not (directory / "index.html").is_file():
        raise ServerSetupError(
            f"{WEB_VARIABLE} names no built app: there is no index.html in it. Build "
            "the app (pnpm build in web/) and name web/dist."
        )
    return directory


def _input() -> front.Input:
    """What this server takes from a visitor, by `BEACON_FRONT_END` (`beacon.front`).

    Raises ServerSetupError for a setting that is neither word."""
    try:
        return front.input_of(os.environ)
    except front.FrontError as error:
        refused = str(error)
    raise ServerSetupError(refused)


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
        # A path with a slash too many is not sent on to the route's own address.
        # The redirect would be made with the scheme this server sees, which behind
        # a host that ends TLS is http, and a client that followed it would send
        # the path, which for a tap's routes holds its id, in the clear.
        redirect_slashes=False,
        # FastAPI traces requests by itself once OpenTelemetry is set up in the
        # process. The private routes are left out of it.
        telemetry={"exclude": _not_traced},
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    # The same filters however many apps are made: a logger holds a filter once.
    logging.getLogger("uvicorn.access").addFilter(_NO_PRIVATE_LINES)
    logging.getLogger("uvicorn.asgi").addFilter(_NOTHING)
    logging.getLogger("uvicorn.error").addFilter(_NO_TRACE)
    # How many requests about saved places are worked on at once.
    app.state.place_checks = Admission()
    # And how many about notifications.
    app.state.push_requests = Admission()
    app.include_router(health.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(places.router, prefix="/api")
    app.include_router(push.router, prefix="/api")
    app.include_router(scale.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(taps.router, prefix="/api")
    # Read when the app is made, so that a setting that is no word stops the server.
    app.state.input = _input()
    web = _web_directory()
    if web is not None:
        # After every route, and for no path under /api (`_WebMount`), so that
        # nothing under /api is ever a file's. The app names its views in the URL's
        # fragment, so only `/` is its page.
        app.router.routes.append(_WebMount("/", app=_Web(directory=web, html=True), name="web"))
    return app


app = create_app()
