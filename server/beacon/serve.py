"""`python -m beacon.serve`: the server as a deployment runs it.

    python -m beacon.serve --host 0.0.0.0 --port 8000

It is `uvicorn beacon.main:app` with three things a deployment wants:

- **No access log.** A line of that log names the address a request came from and
  the second it came at. `beacon.main` leaves the private routes out of it; this
  entry point writes none of it, for any route, and reads no forwarded address.
  It takes no WebSocket either (`ws="none"`): Beacon has none, and uvicorn's
  handshake writes the path of one it refuses to its other log.
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
stand-in's stories: `MOCK_FILES`. Nothing in them was paid for. It never touches a
live store's files, the Ember ledger, a scale run, or the push subscriptions, which
are devices' and have their own lifetimes. A tap's photo or clip is left to the
server's sweep, which keeps one for an hour at most and deletes one sooner once
its tap is gone.

**Under what it deletes.** This process holds the data directory's stores alone
(`beacon.datadir.DataLock`) from before the first store is opened until the
process ends, over every telling and every deletion between them, so no other
server and no `python -m beacon.ingest` has a store open then or opens one
meanwhile. Before anything is changed, each file is read through a connection that
cannot write and has to say it is the mock store's, in a shape this Beacon knows:
a file that says it is a live store's, or one a newer Beacon wrote, is nobody's to
delete, and with one there nothing is deleted at all. A file whose write-ahead log
SQLite could not empty, because something still has it open, is not deleted
either.

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

from beacon import files
from beacon.datadir import DataLock, DataLockedError
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
from beacon.investigate import store as investigations
from beacon.scenario import DURATION
from beacon.story import store as stories
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
# How each of them says it is the mock store's: the table its mode is written in,
# and the shapes this Beacon knows it in. The evidence store says no shape.
_SAYS = {
    "investigate": ("meta", investigations.SHAPES),
    "story": ("meta", stories.SHAPES),
    "evidence": ("store_meta", frozenset({0})),
}
# How long a story is played for, from its start to the last moment anything of it
# changes. The story then has nothing more to tell.
PLAYED_FOR = DURATION + _AFTER_THE_STORY
# The status the process ends with when a telling that was over did not stop
# whole. `__main__` then ends the process without waiting for a thread: only the
# invented story is ever told again, and nothing on the mock store is paid for.
LEFT_RUNNING = 4
# How often a running server is asked whether its story is over.
CHECK_SECONDS = 15.0

# What the app says of a server that tells the story again, in the server's words.
_EVERY_MINUTES = round((PLAYED_FOR - MOCK_HEAD_START).total_seconds() / 60)
REPEATS = (
    "On this server the invented scenario is told again from its beginning when it "
    f"is over, about every {_EVERY_MINUTES} minutes. Each time, the taps sent before "
    f"and their answers are deleted, so an answer is kept here for about {_EVERY_MINUTES} "
    "minutes at most, not for the 23 hours another server can keep one. A photo or a "
    "clip sent with a tap is deleted by the server's sweep: within about an hour, as "
    "on any server, and within minutes once its tap has been deleted."
)
# How long SQLite is waited for when a file's log is emptied before it is deleted.
_WAIT_SECONDS = 5.0

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


class NotTheMockStoresError(Exception):
    """A file where one of the mock store's is kept does not say it is the mock
    store's, in a shape this Beacon knows. The text is fixed."""


class StillOpenError(Exception):
    """SQLite could not empty a file's write-ahead log into it: something still
    has the file open. The text is fixed."""


def begin_again(env: Mapping[str, str], now: datetime, lock: DataLock) -> bool:
    """Delete the mock store's files when its story is over at `now`, so that the
    server that starts next begins the story again, and say whether it did.

    `lock` is the data directory's, held alone by the caller, who has no store
    open: see the module's docstring. False, with nothing deleted, when there is
    no mock store or its story is not over. Only `MOCK_FILES` are deleted, and
    none of them before every one that is there was seen to be the mock store's.

    Raises NotTheMockStoresError and sqlite3.Error for a file that does not say it
    is the mock store's, StillOpenError for one something has open,
    MediaLockedError when something has the taps' media, EvidenceStoreError when
    the mock store cannot be read as one, EmberConfigError for a relative
    `BEACON_DATA_DIR`, and OSError when a file could not be deleted, which leaves
    the evidence store, and so the next try, in place."""
    data = default_data_dir(env)
    if not (lock.held and lock.exclusive and lock.data == data):
        raise ValueError("The story is begun again only by who holds the stores alone.")
    evidence = data / MOCK_FILES[-1][0] / MOCK_FILES[-1][1]
    if not evidence.is_file():
        return False
    there = [data / directory / name for directory, name in MOCK_FILES]
    there = [path for path in there if path.is_file()]
    # Before any file is written to: each says whose it is, read-only.
    for path in there:
        _mock_stores(path)
    # The lock of whoever sweeps the taps' media (`python -m beacon.taps`).
    held = TapMedia(data / "taps", "mock")
    try:
        store = EvidenceStore(evidence, "mock")
        try:
            start = store.state(_SCENARIO_STATE).get("start")
        finally:
            store.close()
        if not isinstance(start, str) or now < over_at(datetime.fromisoformat(start)):
            return False
        # Every log is emptied before any file goes: one that is busy stops it all.
        for path in there:
            _settle(path)
        for directory, name in MOCK_FILES:
            _delete(data / directory / name)
        return True
    finally:
        held.close()


def _mock_stores(path: Path) -> None:
    """See that the file says it is the mock store's, in a shape this Beacon
    knows, or is a file nothing was ever written to. It writes nothing.

    Raises NotTheMockStoresError when it is not, and sqlite3.Error when it cannot
    be read as a SQLite file."""
    meta, shapes = _SAYS[path.parent.name]
    said = files.look(path, meta=meta)
    if not said.tables and said.shape == 0:
        return
    if said.mode != "mock" or said.shape not in shapes:
        raise NotTheMockStoresError(
            f"The file kept as the mock store's under {path.parent.name}/ does not say "
            "it is the mock store's, in a shape this Beacon knows. It is not deleted."
        )


def _settle(path: Path) -> None:
    """Empty a SQLite file's write-ahead log into it, so that no log is ever left
    beside a file that is gone, where a new file of the same name would find it.

    Raises StillOpenError when SQLite could not, which is when a connection still
    reads or writes the file."""
    with closing(sqlite3.connect(path, timeout=_WAIT_SECONDS)) as db:
        busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    if busy:
        raise StillOpenError(
            f"The mock store's file under {path.parent.name}/ is still open somewhere. "
            "It is not deleted."
        )


def _delete(path: Path) -> None:
    """Delete a SQLite file with what SQLite keeps beside it."""
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
    start, and `LEFT_RUNNING` when a telling that was over did not stop whole.

    Raises ServeError and IngestConfigError as `repeats` does, and DataLockedError
    when another process has the data directory's stores."""
    from beacon.main import create_app

    clock = (lambda: datetime.now(UTC)) if clock is None else clock
    make_app = create_app if make_app is None else make_app
    repeat = repeats(env)
    # Held from here to the end of the process, over every telling and every
    # deletion between them: nothing else opens a store of this directory in a gap.
    lock = DataLock(default_data_dir(env), shared=env.get(INGEST_VARIABLE, "1") == "0")
    try:
        while True:
            if repeat:
                try:
                    begin_again(env, clock(), lock)
                except _NOT_BEGUN:
                    # The story is then served as far as it was told, and not begun
                    # again by this process: asking over and over would stop the
                    # server each time.
                    logger.error(
                        "The invented story could not be begun again: its files were "
                        "not deleted. This server goes on with the story as it stands."
                    )
                    repeat = False
            # An app for each telling, so that nothing of the one before is read.
            app = make_app()
            app.state.repeats_text = REPEATS if repeat else None
            # The lock this process already holds, which the app does not take again.
            app.state.data_lock = lock
            server = make_server(app)
            over = asyncio.run(_serve(server, app, repeat=repeat, clock=clock))
            if not server.started:
                return 3
            if not over:
                return 0
            if getattr(app.state, "stopped_whole", False) is not True:
                # Something of that telling may still have a store open, on a
                # thread that did not end in the time it was given. Nothing is
                # deleted under it, and no second server is started beside it in
                # this process: the process ends, and whatever runs it starts it
                # again, with nothing of the last one left.
                logger.error(
                    "The server whose story is over did not stop whole. This process "
                    "ends without beginning the story again; started again, it does."
                )
                return LEFT_RUNNING
            logger.info("The invented story is over. It is begun again.")
    finally:
        lock.close()


# What keeps a story from being begun again. None of them is raised with anything
# of a file under it but SQLite's own, which the log does not print.
_NOT_BEGUN = (
    NotTheMockStoresError,
    StillOpenError,
    MediaLockedError,
    EvidenceStoreError,
    OSError,
    sqlite3.Error,
)


def uvicorn_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    """The uvicorn server a deployment runs the app with."""
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            # Nothing is written down about who asked for what, and no address a
            # proxy forwards is read.
            access_log=False,
            proxy_headers=False,
            # Beacon has no WebSocket. With a WebSocket library installed, uvicorn
            # answers a handshake itself and writes its path, a private route's
            # included, to its error log. With none it takes no handshake, and the
            # request is answered as any other is.
            ws="none",
        )
    )


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="python -m beacon.serve", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env.get("PORT") or 8000))
    options = parser.parse_args(argv)

    def make_server(app: FastAPI) -> Any:
        return uvicorn_server(app, options.host, options.port)

    try:
        return run(env, make_server)
    except (ServeError, IngestConfigError, EmberConfigError) as error:
        print(f"The server was not started. {error}", file=sys.stderr)
        return 2
    except DataLockedError as error:
        print(f"The server was not started. {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    status = main()
    if status == LEFT_RUNNING:
        # A thread that did not stop would keep the process from ending, and the
        # address from being served again.
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(status)
    raise SystemExit(status)
