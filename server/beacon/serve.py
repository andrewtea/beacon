"""`python -m beacon.serve`: the server as a deployment runs it.

    python -m beacon.serve --host 0.0.0.0 --port 8000

It is `uvicorn beacon.main:app` with three things a deployment wants:

- **No access log.** A line of that log names the address a request came from and
  the second it came at. `beacon.main` leaves the private routes out of it; this
  entry point writes none of it, for any route, and reads no forwarded address.
- **The built app from the same origin**, when `BEACON_WEB_DIR` names it
  (`beacon.main.create_app`).
- **The invented story told again when it is over**, when `BEACON_MOCK_REPEAT=1`.
  A mock store plays its story once, for about 105 minutes, and a server that was
  begun days ago then shows an invented fire that ended days ago. With the
  setting, the server stops when the story has been played to its end, the mock
  store's files are deleted (`begin_again`), and the server starts on a new store
  whose story began twenty minutes before, all in this one process.

**What `begin_again` deletes, and what it does not.** The mock evidence store with
every tap sent to it, the stand-in's investigations of those taps, and the
stand-in's stories: `MOCK_FILES`. Nothing in them was paid for, and none of it is a
record of anything that happened. It never touches a live store's files, the Ember
ledger, a scale run, or the push subscriptions, which are devices' and have their
own lifetimes. A tap's photo or clip is left to the sweep that deletes it when its
hour is over, as on any server. It deletes only while it holds the lock a running
server holds on the data directory's taps, so never under a server.

It is for the invented scenario only: with `BEACON_LIVE_SOURCES` naming a source,
or with the pollers off, `BEACON_MOCK_REPEAT=1` is refused.
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI

from beacon.ember.client import default_data_dir
from beacon.ember.errors import EmberConfigError
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.ingest.setup import (
    _AFTER_THE_STORY,
    _SCENARIO_STATE,
    MOCK_HEAD_START,
    IngestConfigError,
    live_sources,
)
from beacon.scenario import DURATION
from beacon.taps.media import MediaLockedError, TapMedia

REPEAT_VARIABLE = "BEACON_MOCK_REPEAT"
INGEST_VARIABLE = "BEACON_INGEST"

# The mock store's files that are deleted when its story is begun again, each a
# directory under the data directory and a file in it, in the order they go: the
# evidence store last, because it says whether the story is over, so that a
# deletion that was cut short is finished by the next one.
MOCK_FILES = (
    ("investigate", "mock.sqlite3"),
    ("story", "mock.sqlite3"),
    ("evidence", "mock.sqlite3"),
)
# How long a story is played for, from its start to the last moment anything of it
# changes. The story then has nothing more to tell.
PLAYED_FOR = DURATION + _AFTER_THE_STORY
# How often a running server is asked whether its story is over.
CHECK_SECONDS = 15.0

# What the app says of a server that tells the story again, in the server's words.
REPEATS = (
    "On this server the invented scenario is told again from its beginning when it "
    f"is over, about every {round((PLAYED_FOR - MOCK_HEAD_START).total_seconds() / 60)} "
    "minutes. Each time, the taps sent before and their answers are deleted. A photo "
    "or a clip sent with a tap is deleted when its hour is over, as on any server."
)

logger = logging.getLogger("beacon")


class ServeError(Exception):
    """The server cannot be run as asked. The text says what to change."""


def repeats(env: Mapping[str, str]) -> bool:
    """Whether the invented story is told again when it is over.

    Raises ServeError for a setting that is not 0 or 1, and for 1 on a server that
    does not play the invented story; IngestConfigError as `live_sources` does."""
    setting = env.get(REPEAT_VARIABLE, "0")
    if setting not in ("0", "1"):
        raise ServeError(f"{REPEAT_VARIABLE} must be 1 or 0.")
    if setting == "0":
        return False
    if live_sources(env):
        raise ServeError(
            f"{REPEAT_VARIABLE}=1 is for the invented scenario: with a real source "
            "named, nothing is told again and no store is deleted."
        )
    if env.get(INGEST_VARIABLE, "1") != "1":
        raise ServeError(f"{REPEAT_VARIABLE}=1 needs the pollers, which play the story.")
    return True


def over_at(start: datetime) -> datetime:
    """When a story that began at `start` has been played to its end."""
    return start + PLAYED_FOR


def begin_again(env: Mapping[str, str], now: datetime) -> bool:
    """Delete the mock store's files when its story is over at `now`, so that the
    server that starts next begins the story again, and say whether it did.

    False, with nothing deleted, when there is no mock store or its story is not
    over. Only `MOCK_FILES` are deleted: see the module's docstring.

    Raises MediaLockedError when a server is running on the data directory,
    EvidenceStoreError when the mock store cannot be read as one, EmberConfigError
    for a relative `BEACON_DATA_DIR`, and OSError or sqlite3.Error when a file could
    not be deleted, which leaves the evidence store, and so the next try, in place."""
    data = default_data_dir(env)
    if not (data / MOCK_FILES[-1][0] / MOCK_FILES[-1][1]).is_file():
        return False
    # The lock a running server holds: nothing is deleted under one.
    held = TapMedia(data / "taps", "mock")
    try:
        store = EvidenceStore(data / MOCK_FILES[-1][0] / MOCK_FILES[-1][1], "mock")
        try:
            start = store.state(_SCENARIO_STATE).get("start")
        finally:
            store.close()
        if not isinstance(start, str) or now < over_at(datetime.fromisoformat(start)):
            return False
        for directory, name in MOCK_FILES:
            _delete(data / directory / name)
        return True
    finally:
        held.close()


def _delete(path: Path) -> None:
    """Delete a SQLite file with what SQLite keeps beside it. Its write-ahead log
    is emptied into it first, so that no log is ever left beside a file that is
    gone, where a new file of the same name would find it."""
    if path.is_file():
        with closing(sqlite3.connect(path, timeout=5.0)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    for each in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        each.unlink(missing_ok=True)


class _Server(Protocol):
    started: bool
    should_exit: bool

    async def serve(self) -> None: ...


async def _serve(
    server: _Server, app: FastAPI, *, repeat: bool, clock: Callable[[], datetime]
) -> bool:
    """Serve until the server is told to stop, or, with `repeat`, until the story
    it plays is over, and say whether it ended because the story was over."""
    serving = asyncio.ensure_future(server.serve())
    over = False
    try:
        while not serving.done():
            if repeat and server.started and not over:
                ingest = getattr(app.state, "ingest", None)
                scenario = getattr(ingest, "scenario", None)
                if scenario is not None and clock() >= over_at(scenario.start):
                    # As a signal would: requests under way are answered, and the
                    # app's own shutdown closes every file.
                    over = True
                    server.should_exit = True
            await asyncio.wait({serving}, timeout=CHECK_SECONDS)
    finally:
        if not serving.done():
            server.should_exit = True
        await serving
    return over


def run(
    env: Mapping[str, str],
    make_server: Callable[[FastAPI], _Server],
    *,
    clock: Callable[[], datetime] | None = None,
    make_app: Callable[[], FastAPI] | None = None,
) -> int:
    """Run the server, again and again while its story is told again. Returns the
    process's exit status: 0 when the server stopped as asked, 3 when it could not
    start.

    Raises ServeError and IngestConfigError as `repeats` does."""
    from beacon.main import create_app

    clock = (lambda: datetime.now(UTC)) if clock is None else clock
    make_app = create_app if make_app is None else make_app
    repeat = repeats(env)
    while True:
        if repeat:
            try:
                begin_again(env, clock())
            except (MediaLockedError, EvidenceStoreError, OSError, sqlite3.Error):
                # The story is then served as far as it was told, and not begun
                # again by this process: asking over and over would stop the
                # server each time.
                logger.error(
                    "The invented story could not be begun again: its files could not "
                    "be deleted. This server goes on with the story as it stands."
                )
                repeat = False
        # An app for each telling, so that nothing of the one before is read.
        app = make_app()
        app.state.repeats_text = REPEATS if repeat else None
        server = make_server(app)
        over = asyncio.run(_serve(server, app, repeat=repeat, clock=clock))
        if not server.started:
            return 3
        if not over:
            return 0
        logger.info("The invented story is over. It is begun again.")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="python -m beacon.serve", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env.get("PORT") or 8000))
    options = parser.parse_args(argv)

    def make_server(app: FastAPI) -> Any:
        return uvicorn.Server(
            uvicorn.Config(
                app,
                host=options.host,
                port=options.port,
                # Nothing is written down about who asked for what, and no
                # address a proxy forwards is read.
                access_log=False,
                proxy_headers=False,
            )
        )

    try:
        return run(env, make_server)
    except (ServeError, IngestConfigError, EmberConfigError) as error:
        print(f"The server was not started. {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
