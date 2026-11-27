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
stand-in's stories: `MOCK_FILES`. Nothing in them was paid for: the setting is
refused where Muse Ember works on the invented story (`beacon.telling`), and a
store that says Muse Ember worked on it is not deleted whatever the setting
(`PAID_FOR`), because it holds the claims on what was paid for. It never touches a
live store's files, the Ember ledger, a scale run, or the push subscriptions, which
are devices' and have their own lifetimes. A tap's photo or clip is left to the
server's sweep, which deletes one once its hour is over and sooner once its tap
is gone.

**Under what it deletes.** This process holds the data directory's stores alone
(`beacon.datadir.DataLock`) from before the first store is opened until the
process ends, over every telling and every deletion between them, so no other
server and no `python -m beacon.ingest` has a store open then or opens one
meanwhile. Before anything is changed, each file has to be that kind of store's own
file and no link to another (`_own`), and is read through a connection that cannot
write: it has to say it is the mock store's, in a shape this Beacon knows, and
hold that kind's tables and no others (`_mock_stores`). The push subscriptions
file says `mock` too, in a shape number the other files also have, so what a file
says is not enough to know whose it is. A file that fails any of that is nobody's
to delete: nothing is deleted, **and the server does not go on**, because it would
open the same file to write to. A file whose write-ahead log SQLite could not
empty, because something still has it open, is not deleted either; the story is
then served as far as it was told.

**A telling that did not stop whole ends the process**, at once and before
anything else is waited for (`run`'s `leave`): a thread of it may still have a
store open, so nothing is deleted and no second server is started beside it. The
lock is not given up then. It is gone with the process, and whatever runs the
server starts it again.

It is for the invented scenario only: with `BEACON_LIVE_SOURCES` naming a source,
or with the pollers off, `BEACON_MOCK_REPEAT=1` is refused.
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, Protocol

import uvicorn
from fastapi import FastAPI

from beacon import files
from beacon.datadir import DataLock, DataLockedError
from beacon.ember.client import default_data_dir
from beacon.ember.errors import EmberConfigError
from beacon.errors import raise_bare
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.evidence import store as evidence_store
from beacon.ingest.setup import (
    _AFTER_THE_STORY,
    _SCENARIO_STATE,
    MOCK_HEAD_START,
    IngestConfigError,
    live_sources,
    whose,
)
from beacon.investigate import store as investigations
from beacon.scenario import DURATION
from beacon.story import store as stories
from beacon.taps.media import MediaLockedError, TapMedia
from beacon.telling import MOCK_EMBER_VARIABLE, TellingConfigError, teller_of

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


@dataclass(frozen=True)
class _Kind:
    """How a file of one kind is known: the table its mode is written in, the
    shapes this Beacon knows it in, the tables it can have, and the table no
    other kind has."""

    meta: str
    shapes: frozenset[int]
    tables: frozenset[str]
    own: str
    # What a file of the kind says its mode is, when it is the mock store's.
    modes: frozenset[str] = frozenset({"mock"})


# The evidence store says no shape.
_SAYS = {
    "investigate": _Kind(
        "meta", investigations.SHAPES, investigations.TABLES, investigations.OWN_TABLE
    ),
    "story": _Kind("meta", stories.SHAPES, stories.TABLES, stories.OWN_TABLE),
    # A mock evidence store Muse Ember was set to work on says so in its mode
    # (`PAID_MOCK`). It is the mock store's file, and is never deleted (`PAID_FOR`).
    "evidence": _Kind(
        "store_meta",
        frozenset({0}),
        evidence_store.TABLES,
        evidence_store.OWN_TABLE,
        frozenset({"mock", evidence_store.PAID_MOCK}),
    ),
}
# How long a story is played for, from its start to the last moment anything of it
# changes. The story then has nothing more to tell.
PLAYED_FOR = DURATION + _AFTER_THE_STORY
# The status the process ends with when a server that tells the story again did
# not stop whole. The process is then ended without waiting for a thread
# (`_leave`): only the invented story is ever told again, and nothing on the mock
# store is paid for.
LEFT_RUNNING = 4
# How often a running server is asked whether its story is over.
CHECK_SECONDS = 15.0

# What the app says of a server that tells the story again, in the server's words.
_EVERY_MINUTES = round((PLAYED_FOR - MOCK_HEAD_START).total_seconds() / 60)
REPEATS = (
    "On this server the invented scenario is told again from its beginning when it "
    f"is over, about every {_EVERY_MINUTES} minutes. Each time, the taps sent before "
    f"and their answers are deleted, so an answer is kept here for about {_EVERY_MINUTES} "
    "minutes at most, not for the 23 hours another server can keep one."
)
# Said after it by a server that takes a clip or a photo with a tap. It promises
# no moment of deletion: the sweep runs each minute, and only while the server is up.
REPEATS_MEDIA = (
    " A photo or a clip sent with a tap cannot be read after an hour, as on any "
    "server, and the server's sweep then deletes it. The sweep deletes one sooner "
    "once its tap has been deleted."
)
# What SQLite keeps beside a database, by what it adds to the file's name: the
# write-ahead log and its index, and the journal of a file that is not in
# write-ahead mode.
_BESIDE = ("-wal", "-shm", "-journal")
# The tables SQLite makes in a database by itself: the counter of rows' ids, and
# what `ANALYZE` keeps for its planner. They are in a file of any kind.
_SQLITES_OWN = frozenset(
    {"sqlite_sequence", "sqlite_stat1", "sqlite_stat2", "sqlite_stat3", "sqlite_stat4"}
)
# How long SQLite is waited for when a file's log is emptied before it is deleted.
_WAIT_SECONDS = 5.0

logger = logging.getLogger("beacon")


class ServeError(Exception):
    """The server cannot be run as asked. The text says what to change."""


def repeats(env: Mapping[str, str]) -> bool:
    """Whether the invented story is told again when it is over.

    Raises ServeError for a setting that is not 0 or 1, for 1 on a server that
    does not play the invented story, and for 1 where Muse Ember works on it
    (`beacon.telling`); IngestConfigError as `live_sources` does, and
    TellingConfigError as `teller_of` does."""
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
    if teller_of(env) == "ember":
        # Every telling by Muse Ember is paid for, and what is deleted between two
        # tellings would be the claims on what was paid for.
        raise ServeError(
            f"{REPEAT_VARIABLE}=1 is for a stand-in's telling, which costs nothing: with "
            f"{MOCK_EMBER_VARIABLE}=live the story is not told again by itself, and "
            "nothing of a telling Muse Ember was paid for is deleted."
        )
    return True


# Said of a mock store Muse Ember worked on, by a server that would delete it.
PAID_FOR = (
    "The mock store under this data directory is one Muse Ember worked on: it holds "
    "the claims on what was paid for. A server that tells the invented story again "
    f"({REPEAT_VARIABLE}=1) deletes the mock store between tellings, and does not run "
    "on this one. Nothing was deleted. Start the server as the store was begun: "
    f"{MOCK_EMBER_VARIABLE}=live with its budget, and without {REPEAT_VARIABLE}=1."
)


def over_at(start: datetime) -> datetime:
    """When a story that began at `start` has been played to its end."""
    return start + PLAYED_FOR


class NotTheMockStoresError(Exception):
    """What is where one of the mock store's files is kept was not seen to be
    that file: a link, another kind of store, a live store's, one a newer Beacon
    wrote, or one that could not be read. The text is fixed."""


_NOT_THE_FILE = (
    "What is kept as the mock store's under {}/ could not be seen to be that store's "
    "own file: it is a link, it could not be read, or it does not say it is the mock "
    "store's, in a shape and with the tables this Beacon knows. Nothing was deleted, "
    "and a server that tells the invented story again does not run on it. Find out "
    "what is there."
)


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

    Raises ServeError, with nothing changed, for a mock store Muse Ember worked
    on (`beacon.telling`), whatever this server is set to: its files hold the
    claims on what was paid for. NotTheMockStoresError for a file that was not
    seen to be the mock store's own of its kind, StillOpenError for one something
    has open,
    sqlite3.Error when its log could not be emptied, MediaLockedError when
    something has the taps' media, EvidenceStoreError when the mock store cannot
    be read as one, EmberConfigError for a relative `BEACON_DATA_DIR`, and OSError
    when a file could not be deleted, which leaves the evidence store, and so the
    next try, in place."""
    data = default_data_dir(env)
    if not (lock.held and lock.exclusive and lock.data == data):
        raise ValueError("The story is begun again only by who holds the stores alone.")
    # Before any file is written to, and whether or not there is a story to begin
    # again: each that is there is that store's own file and says so, read-only.
    there = [
        path
        for path in (_own(data, directory, name) for directory, name in MOCK_FILES)
        if path is not None
    ]
    for path in there:
        _mock_stores(path)
    evidence = data / MOCK_FILES[-1][0] / MOCK_FILES[-1][1]
    if evidence not in there:
        return False
    # The lock of whoever sweeps the taps' media (`python -m beacon.taps`).
    held = TapMedia(data / "taps", "mock")
    try:
        store = EvidenceStore(evidence, "mock")
        try:
            state = store.state(_SCENARIO_STATE)
            teller = whose(store)
        finally:
            store.close()
        if teller == "ember":
            # Whatever this server is set to: what the store says, read under the
            # lock, before anything is changed.
            raise ServeError(PAID_FOR)
        start = state.get("start")
        if not isinstance(start, str) or now < over_at(datetime.fromisoformat(start)):
            return False
        # Every log is emptied before any file goes: one that is busy stops it all.
        for path in there:
            _settle(path)
        # Each of them, one that is not there included: what SQLite left beside a
        # file that is gone would be found by a new file of the same name.
        for directory, name in MOCK_FILES:
            _delete(data / directory / name)
        return True
    finally:
        held.close()


def _own(data: Path, directory: str, name: str) -> Path | None:
    """The file of one of the mock store's kinds, or None when there is none.

    Before SQLite opens anything: SQLite reads and writes the files it keeps
    beside a database as well (`_BESIDE`), and emptying a log writes to the log.
    So the file and each of those that is there has to be a file of its own, in a
    directory of its own, and the ones beside it whether or not the file is there:
    a new file of the name would be opened with them.

    Raises NotTheMockStoresError when one is not, and when one could not be
    looked at: a link, of either kind, can be to another store's file, which
    would be written to or deleted in this one's place."""
    folder = data / directory
    path = folder / name
    try:
        # `Path.is_symlink` answers False for a directory it could not look at.
        linked = stat.S_ISLNK(os.lstat(folder).st_mode)
    except FileNotFoundError:
        return None
    except OSError:
        linked = True
    there = False
    for each in (path, *(path.with_name(name + suffix) for suffix in _BESIDE)):
        if linked:
            break
        try:
            found = os.lstat(each)
        except FileNotFoundError:
            continue
        except OSError:
            # It could not be looked at (no leave to, an error of the disk, a
            # directory that is a file): it was not seen to be this store's own,
            # which is all this answers. Not left as an OSError, which `run`
            # takes for a file that was seen to be and could not be deleted.
            linked = True
        else:
            linked = not stat.S_ISREG(found.st_mode) or found.st_nlink != 1
            there = there or each == path
    if linked:
        raise_bare(NotTheMockStoresError(_NOT_THE_FILE.format(directory)))
    return path if there else None


def _mock_stores(path: Path) -> None:
    """See that the file is its kind's and the mock store's, in a shape this
    Beacon knows, or is a file nothing was ever written to. It writes nothing to
    the file, and is called only for a file `_own` passed.

    Its kind is known by its tables, not by what it says: every one of Beacon's
    files says a mode, several in the same table and with the same shape number.

    Raises NotTheMockStoresError when it is not, and when it could not be read:
    a file that was not seen to be the mock store's is not opened to write to."""
    kind = _SAYS[path.parent.name]
    said = None
    with suppress(sqlite3.Error):
        said = files.look(path, meta=kind.meta)
    if said is not None:
        if said.objects == 0 and said.shape == 0:
            return
        if (
            said.mode in kind.modes
            and said.shape in kind.shapes
            and kind.own in said.tables
            # No table of another kind's, or of anybody else's.
            and said.tables - _SQLITES_OWN <= kind.tables
        ):
            return
    raise_bare(NotTheMockStoresError(_NOT_THE_FILE.format(path.parent.name)))


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
    for each in (path, *(path.with_name(path.name + suffix) for suffix in _BESIDE)):
        each.unlink(missing_ok=True)


class _Server(Protocol):
    started: bool
    should_exit: bool

    async def serve(self) -> None: ...


async def _serve(
    server: _Server,
    app: FastAPI,
    *,
    repeat: bool,
    clock: Callable[[], datetime],
    leave: Callable[[], object] | None = None,
) -> bool:
    """Serve until the server is told to stop, or, with `repeat`, until the story
    it plays is over, and say whether it ended because the story was over.

    `leave` is called when the server that ended did not stop whole, by the task
    that served and before that task is done (`_to_its_end`)."""
    serving = asyncio.ensure_future(_to_its_end(server, app, leave))
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


async def _to_its_end(server: _Server, app: FastAPI, leave: Callable[[], object] | None) -> None:
    """Serve to the server's end, however it ends, and call `leave` when it did
    not stop whole.

    **Here, in the task that serves, and not after the loop is done**: what runs
    the loop waits for the threads that did not stop, for up to five minutes,
    before anything after it can end the process. And for an end that is an
    error too: uvicorn ends a server that could not start (a startup that failed,
    an address it could not have) by raising `SystemExit`, after the app's own
    shutdown has run and said whether everything it had begun ended. That exit is
    taken here as the server not having started, which `server.started` says, so
    that it is not raised through the loop, past the look at how it stopped."""
    try:
        with suppress(SystemExit):
            await server.serve()
    finally:
        if leave is not None and _left_running(server, app):
            leave()


def _left_running(server: _Server, app: FastAPI) -> bool:
    """Whether something of a server that stopped may still be running: its
    shutdown said that not everything it began has ended, or it began and its
    shutdown said nothing. A server that could not start says the first too, when
    what it had begun by then did not all end."""
    stopped = getattr(app.state, "stopped_whole", None)
    return stopped is False or (server.started and stopped is not True)


def run(
    env: Mapping[str, str],
    make_server: Callable[[FastAPI], _Server],
    *,
    clock: Callable[[], datetime] | None = None,
    make_app: Callable[[], FastAPI] | None = None,
    leave: Callable[[], object] | None = None,
) -> int:
    """Run the server, again and again while its story is told again. Returns the
    process's exit status: 0 when the server stopped as asked, 3 when it could not
    start, and `LEFT_RUNNING` when a server that tells the story again did not
    stop whole.

    `leave` ends the process when such a server did not stop whole, and is called
    the moment that is known (`_serve`); it is the invented scenario's server only,
    on which nothing is paid for. Without one, as in a test, `LEFT_RUNNING` is
    returned instead. **The stores' lock is given up only by a server that stopped
    whole**: after any other, a thread may still have a store open, and the lock
    is kept until the process is gone.

    Raises ServeError and IngestConfigError as `repeats` does, ServeError as
    `begin_again` does for a store Muse Ember worked on, DataLockedError when
    another process has the data directory's stores, and NotTheMockStoresError
    when a file kept as the mock store's is not that."""
    from beacon.main import create_app

    clock = (lambda: datetime.now(UTC)) if clock is None else clock
    make_app = create_app if make_app is None else make_app
    # Whether this is a server that tells the story again, whatever becomes of one
    # try to: it is then the invented scenario's, and may be ended at once.
    told_again = repeat = repeats(env)
    # Held from here to the end of the process, over every telling and every
    # deletion between them: nothing else opens a store of this directory in a gap.
    lock = DataLock(default_data_dir(env), shared=env.get(INGEST_VARIABLE, "1") == "0")
    # The server that was begun last, with its app, which says how it stopped.
    last: tuple[_Server, FastAPI] | None = None
    try:
        while True:
            if repeat:
                try:
                    begin_again(env, clock(), lock)
                except _NOT_BEGUN:
                    # The files are the mock store's own, and one could not be had
                    # just now. The story is served as far as it was told, and not
                    # begun again by this process: asking over and over would stop
                    # the server each time.
                    logger.error(
                        "The invented story could not be begun again: its files were "
                        "not deleted. This server goes on with the story as it stands."
                    )
                    repeat = False
            # An app for each telling, so that nothing of the one before is read.
            app = make_app()
            app.state.repeats_text = _repeats_text(app) if repeat else None
            # The lock this process already holds, which the app does not take again.
            app.state.data_lock = lock
            server = make_server(app)
            last = (server, app)
            over = asyncio.run(
                _serve(server, app, repeat=repeat, clock=clock, leave=leave if told_again else None)
            )
            whole = not _left_running(server, app)
            if not whole and told_again:
                # Something of that telling may still have a store open, on a
                # thread that did not end in the time it was given. Nothing is
                # deleted under it, and no second server is started beside it in
                # this process: the process ends, and whatever runs it starts it
                # again, with nothing of the last one left.
                logger.error(_LEFT)
                return LEFT_RUNNING
            if not server.started:
                return 3
            if not over or not whole:
                return 0
            logger.info("The invented story is over. It is begun again.")
    finally:
        # By what the last server's own shutdown said, read here and not carried
        # in a variable: this is reached by an error as well, and a server that
        # ended in one can have left a worker behind like any other.
        if last is None or not _left_running(*last):
            lock.close()


_LEFT = (
    "The server did not stop whole. This process ends without beginning the "
    "invented story again; started again, it does."
)


def _repeats_text(app: FastAPI) -> str:
    """What a server that tells the story again says of it: of a clip or a photo
    only where one is taken."""
    takes_media = getattr(app.state, "input", "own") == "own"
    return REPEATS + (REPEATS_MEDIA if takes_media else "")


def _leave() -> NoReturn:
    """End the process now, with `LEFT_RUNNING`. A thread that did not stop would
    keep the process from ending, and the address from being served again, and
    waiting for it comes before anything else in an ordinary exit."""
    logger.error(_LEFT)
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(LEFT_RUNNING)


# What keeps a story from being begun again and leaves the server to go on with it
# as it stands: something else has a file just now. None of them is raised with
# anything of a file under it but SQLite's own, which the log does not print. A file
# that is not the mock store's own (`NotTheMockStoresError`) is not among them: the
# server would open it to write to.
_NOT_BEGUN = (
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
        return run(env, make_server, leave=_leave)
    except (ServeError, IngestConfigError, EmberConfigError, TellingConfigError) as error:
        print(f"The server was not started. {error}", file=sys.stderr)
        return 2
    except NotTheMockStoresError as error:
        print(f"The server does not run. {error}", file=sys.stderr)
        return 3
    except DataLockedError as error:
        print(f"The server was not started. {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    status = main()
    if status == LEFT_RUNNING:
        _leave()
    raise SystemExit(status)
