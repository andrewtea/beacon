"""ASGI entry point: `uvicorn beacon.main:app`."""

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from beacon import __version__
from beacon.api import health, sources
from beacon.ingest import IngestConfigError, build

_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})

INGEST_VARIABLE = "BEACON_INGEST"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the pollers on a thread of their own for as long as the server runs,
    unless `BEACON_INGEST=0`. One server process is one set of pollers: a second
    process would ask every source twice as often."""
    setting = os.environ.get(INGEST_VARIABLE, "1")
    if setting not in ("0", "1"):
        raise IngestConfigError(f"{INGEST_VARIABLE} must be 1 or 0, not {setting!r}.")
    if setting == "0":
        yield
        return
    ingest = build(os.environ)
    stop = threading.Event()
    thread = threading.Thread(target=ingest.run, args=(stop,), name="beacon-ingest", daemon=True)
    app.state.ingest = ingest
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


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
    app.include_router(health.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    return app


app = create_app()
