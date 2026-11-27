"""`python -m beacon.serve`: the server as a deployment runs it, and the invented
story begun again when it is over."""

import asyncio
import contextlib
import logging
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import beacon.main
from beacon import serve
from beacon.datadir import LOCK_FILE, DataLock, DataLockedError
from beacon.evidence import EvidenceStore, NewEvidence
from beacon.ingest.setup import MOCK_HEAD_START, IngestConfigError
from beacon.main import create_app
from beacon.story import Stories

ENV_KEY = "BEACON_DATA_DIR"


def data_dir() -> Path:
    return Path(os.environ[ENV_KEY])


def env() -> dict[str, str]:
    return {ENV_KEY: os.environ[ENV_KEY], serve.REPEAT_VARIABLE: "1"}


def served() -> datetime:
    """Run a mock server once under the test's data directory, and say when the
    story it began is over."""
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["mode"] == "mock"
    store = EvidenceStore(data_dir() / "evidence" / "mock.sqlite3", "mock")
    try:
        start = datetime.fromisoformat(store.state("scenario")["start"])
    finally:
        store.close()
    return serve.over_at(start)


def files() -> dict[str, bytes]:
    """Every file under the data directory but the stores' lock, which is made
    by whoever asks for it and is never deleted."""
    root = data_dir()
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != LOCK_FILE
    }


def kept() -> dict[str, bytes]:
    """The files as `files` has them, without what SQLite keeps beside a database
    in write-ahead mode while it is read (`-shm`, and a `-wal` it empties): reading
    a file makes those, and writes nothing to the file."""
    return {name: held for name, held in files().items() if not name.endswith(("-wal", "-shm"))}


@contextmanager
def alone() -> Iterator[DataLock]:
    """The data directory's stores, held alone, as `serve.run` holds them."""
    lock = DataLock(data_dir(), shared=False)
    try:
        yield lock
    finally:
        lock.close()


def begin_again(now: datetime) -> bool:
    with alone() as lock:
        return serve.begin_again(env(), now, lock)


def test_the_story_is_played_for_as_long_as_the_pollers_play_it() -> None:
    # A story begins twenty minutes before its store does, and is over 105 minutes
    # after it began: a server that tells it again does so every 85 minutes.
    start = datetime(2026, 3, 3, 16, 0, tzinfo=UTC)
    assert serve.over_at(start) == start + timedelta(minutes=105)
    assert timedelta(minutes=20) == MOCK_HEAD_START
    assert "about every 85 minutes" in serve.REPEATS
    # What a person is told is how long an answer is kept here. Of a photo, that
    # it cannot be read after its hour and that the sweep then deletes it: no
    # moment of deletion is promised, since the sweep runs only while the server is up.
    assert "kept here for about 85 minutes at most" in serve.REPEATS
    assert "cannot be read after an hour" in serve.REPEATS_MEDIA
    assert "sweep then deletes it" in serve.REPEATS_MEDIA
    assert "within" not in serve.REPEATS_MEDIA


def test_nothing_is_deleted_before_the_story_is_over_or_where_there_is_no_mock_store() -> None:
    # No store: nothing to begin again, and no directory is made for asking.
    assert begin_again(datetime.now(UTC)) is False
    assert files() == {}
    over = served()
    before = kept()

    assert begin_again(over - timedelta(seconds=1)) is False

    assert kept() == before


def test_when_the_story_is_over_the_mock_stores_files_are_deleted_and_nothing_else_is() -> None:
    over = served()
    root = data_dir()
    # What must outlast the story: a live store's files, a scale run, a tap's photo
    # (which the sweep deletes when its hour is over), the Ember ledger, and the
    # push subscriptions with this mode's key.
    for directory in ("evidence", "investigate", "story"):
        (root / directory / "live.sqlite3").write_bytes(b"a live store's")
    (root / "scale" / "day").mkdir(parents=True)
    (root / "scale" / "day" / "results.json").write_text("{}")
    (root / "push" / "mock.vapid.key").write_bytes(b"the key pushes are signed with")
    (root / "taps" / "mock").mkdir(parents=True, exist_ok=True)
    (root / "taps" / "mock" / "photo-of-a-tap").write_bytes(b"jpeg")
    before = files()
    mock = [f"{directory}/{name}" for directory, name in serve.MOCK_FILES]
    assert set(mock) <= set(before)
    assert {"ember/ledger.sqlite3", "push/mock.sqlite3", "push/mock.vapid.key"} <= set(before)

    assert begin_again(over) is True

    after = files()
    # The three files and whatever SQLite kept beside them, and nothing else.
    gone = set(before) - set(after)
    assert set(mock) <= gone
    assert all(name.removesuffix("-wal").removesuffix("-shm") in mock for name in gone)
    assert not any(name.startswith(tuple(mock)) for name in after)
    assert {name: held for name, held in before.items() if name not in gone} == after
    # Asked again, there is nothing to begin again.
    assert begin_again(over) is False


def test_every_file_of_the_mock_store_but_the_devices_subscriptions_is_begun_again() -> None:
    # A file a later Beacon keeps for the mock store has to be named in
    # `MOCK_FILES`, or a new story would be told over the last one's leavings.
    served()
    root = data_dir()
    made = {
        (path.parent.name, path.name)
        for path in root.rglob("mock.sqlite3")
        if path.parent.name != "push"
    }
    assert made == set(serve.MOCK_FILES)
    # The evidence store goes last: it says whether the story is over, so a
    # deletion that was cut short is finished by the next one.
    assert serve.MOCK_FILES[-1] == ("evidence", "mock.sqlite3")


def test_nothing_is_deleted_under_a_running_server() -> None:
    over = served()
    before = kept()

    # A running server has the stores, so nobody else has them alone.
    with (
        TestClient(create_app()),
        pytest.raises(DataLockedError, match="has this data directory's stores open"),
    ):
        DataLock(data_dir(), shared=False)
    # And the story is begun again only by who does: not with a lock that is
    # shared, given up, or another directory's.
    shared = DataLock(data_dir(), shared=True)
    closed = DataLock(data_dir() / "spare", shared=False)
    closed.close()
    elsewhere = DataLock(data_dir() / "other", shared=False)
    try:
        for lock in (shared, closed, elsewhere):
            with pytest.raises(ValueError, match="holds the stores alone"):
                serve.begin_again(env(), over, lock)
    finally:
        shared.close()
        elsewhere.close()

    assert {name: held for name, held in kept().items() if name in before} == before


def test_a_server_holds_the_stores_from_before_it_opens_one_until_its_workers_have_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A second process that opened the store in a gap would read the story's start
    # from a file that is then deleted and made again. So: the lock is there before
    # the pollers are built, and still there while they are being stopped.
    seen: list[str] = []

    def held(when: str) -> None:
        try:
            DataLock(data_dir(), shared=True).close()
            seen.append(f"{when}: free")
        except DataLockedError:
            seen.append(f"{when}: held")

    build, stop = beacon.main.build, beacon.main._stop_ingest

    def building(env: os._Environ[str]) -> object:
        held("before the first store")
        assert not (data_dir() / "evidence").exists()
        return build(env)

    async def stopping(ingest: object) -> bool:
        whole = await stop(ingest)  # type: ignore[arg-type]
        held("after the pollers stopped")
        return whole

    monkeypatch.setattr(beacon.main, "build", building)
    monkeypatch.setattr(beacon.main, "_stop_ingest", stopping)
    with TestClient(create_app()):
        held("serving")
    held("after the server")

    assert seen == [
        "before the first store: held",
        "serving: held",
        "after the pollers stopped: held",
        "after the server: free",
    ]


def test_a_server_without_its_pollers_shares_the_stores_and_one_with_them_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `python -m beacon.ingest` beside a server started with BEACON_INGEST=0 is how
    # the pollers are run apart. Beside a server that polls, nothing else has them.
    monkeypatch.setenv("BEACON_INGEST", "0")
    with TestClient(create_app()):
        DataLock(data_dir(), shared=True).close()
        with pytest.raises(DataLockedError):
            DataLock(data_dir(), shared=False)
    monkeypatch.setenv("BEACON_INGEST", "1")
    with TestClient(create_app()), pytest.raises(DataLockedError):
        DataLock(data_dir(), shared=True)
    # And a server that polls does not start beside whoever has them.
    with alone(), pytest.raises(DataLockedError), TestClient(create_app()):
        pass


@pytest.mark.parametrize("wrong", ["a live store's", "a newer Beacon's", "no database"])
@pytest.mark.parametrize("where", ["story", "investigate", "evidence"])
def test_a_file_that_does_not_say_it_is_the_mock_stores_is_not_deleted_and_nothing_else_is(
    where: str, wrong: str
) -> None:
    # A live store's file restored under the mock store's name, or one a newer
    # Beacon wrote, is the record of what was paid for. It is refused by whatever
    # opens it, and it is not this deletion's to empty: nothing is touched at all.
    over = served()
    path = data_dir() / where / "mock.sqlite3"
    for beside in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        beside.unlink(missing_ok=True)
    if wrong == "a live store's":
        if where == "story":
            Stories(path, "live").close()
        else:
            table = "store_meta" if where == "evidence" else "meta"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(f"CREATE TABLE {table} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute(f"INSERT INTO {table} VALUES ('mode', 'live')")
    elif wrong == "a newer Beacon's":
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO store_meta VALUES ('mode', 'mock')")
            db.execute("INSERT INTO meta VALUES ('mode', 'mock')")
            db.execute("PRAGMA user_version = 99")
    else:
        path.write_bytes(b"not a SQLite file, whatever it is" * 40)
    before = kept()

    with pytest.raises(serve.NotTheMockStoresError, match="Nothing was deleted"):
        begin_again(over + timedelta(hours=1))

    assert kept() == before


def push_file() -> Path:
    return data_dir() / "push" / "mock.sqlite3"


@pytest.mark.parametrize("how", ["the directory", "the file", "a second name"])
def test_a_link_to_another_stores_file_is_not_followed_to_delete_it(how: str) -> None:
    # The devices' subscriptions say `mock` too, in a shape number the
    # investigations file also has. Reached through a link where the
    # investigations are kept, they are not deleted in that file's place.
    over = served()
    folder = data_dir() / "investigate"
    for each in folder.iterdir():
        each.unlink()
    if how == "the directory":
        folder.rmdir()
        folder.symlink_to(data_dir() / "push", target_is_directory=True)
    elif how == "the file":
        (folder / "mock.sqlite3").symlink_to(push_file())
    else:
        os.link(push_file(), folder / "mock.sqlite3")
    subscriptions = push_file().read_bytes()
    before = {name: held for name, held in kept().items() if not name.startswith("investigate/")}

    with pytest.raises(serve.NotTheMockStoresError, match="is a link"):
        begin_again(over + timedelta(hours=1))

    assert push_file().read_bytes() == subscriptions
    after = {name: held for name, held in kept().items() if not name.startswith("investigate/")}
    assert after == before


@pytest.mark.parametrize("what", ["the subscriptions", "the stories", "only a view"])
@pytest.mark.parametrize("where", ["investigate", "story", "evidence"])
def test_a_file_of_another_kind_is_not_deleted_whatever_it_says_of_itself(
    where: str, what: str
) -> None:
    # What a file says is not whose it is: a copy of the subscriptions says `mock`
    # in a shape the investigations file has too. A file is known by its tables,
    # and one that holds anything at all is not "never written to".
    if where == "story" and what == "the stories":
        return
    over = served()
    path = data_dir() / where / "mock.sqlite3"
    for beside in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        beside.unlink(missing_ok=True)
    if what == "only a view":
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE VIEW kept AS SELECT 1 AS n")
    else:
        source = push_file() if what == "the subscriptions" else data_dir() / "story.copy"
        if what == "the stories":
            Stories(source, "mock").close()
        with closing(sqlite3.connect(source)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        path.write_bytes(source.read_bytes())
    before = kept()

    with pytest.raises(serve.NotTheMockStoresError):
        begin_again(over + timedelta(hours=1))

    assert kept() == before


def test_a_server_that_tells_the_story_again_does_not_run_on_a_file_that_is_not_its_own(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # It would open the same file to write to. Whether or not the story is over.
    served()
    path = data_dir() / "investigate" / "mock.sqlite3"
    for beside in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        beside.unlink(missing_ok=True)
    path.symlink_to(push_file())
    subscriptions = push_file().read_bytes()
    made: list[FastAPI] = []

    def make_server(app: FastAPI) -> Stub:
        made.append(app)
        return Stub(app)

    with pytest.raises(serve.NotTheMockStoresError):
        serve.run(env(), make_server)

    assert made == []
    assert push_file().read_bytes() == subscriptions
    # Nothing of it was left running, so the stores are anybody's again.
    DataLock(data_dir(), shared=False).close()
    # And the command says so in a sentence, with a status that is a failure.
    assert serve.main([], env()) == 3
    assert capsys.readouterr().err.startswith("The server does not run. What is kept as")


def test_a_file_something_still_has_open_is_not_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve, "_WAIT_SECONDS", 0.05)
    over = served()
    path = data_dir() / "evidence" / "mock.sqlite3"
    with closing(sqlite3.connect(path)) as reading, closing(sqlite3.connect(path)) as writing:
        # A reader in the middle of a read, and a write since that it has not seen:
        # the log cannot be emptied into the file under it.
        reading.execute("BEGIN")
        reading.execute("SELECT count(*) FROM evidence").fetchall()
        with writing:
            writing.execute(
                "INSERT INTO poller_state (name, state, updated_ms) VALUES ('since', '{}', 0)"
            )
        names = set(files())

        with pytest.raises(serve.StillOpenError, match="still open"):
            begin_again(over)

        # Not one of the three went, the two that nobody had open included.
        assert {f"{d}/{n}" for d, n in serve.MOCK_FILES} <= set(files())
        assert set(files()) >= names
    assert begin_again(over) is True


def test_a_deletion_that_was_cut_short_is_finished_by_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    over = served()
    delete = serve._delete

    def fails_on_the_evidence(path: Path) -> None:
        if path.parent.name == "evidence":
            raise OSError("no")
        delete(path)

    monkeypatch.setattr(serve, "_delete", fails_on_the_evidence)
    with pytest.raises(OSError, match="no"):
        begin_again(over)
    # The store that says the story is over is still there.
    assert (data_dir() / "evidence" / "mock.sqlite3").is_file()
    assert not (data_dir() / "investigate" / "mock.sqlite3").exists()

    monkeypatch.setattr(serve, "_delete", delete)
    assert begin_again(over) is True
    assert not (data_dir() / "evidence" / "mock.sqlite3").exists()


@pytest.mark.parametrize(
    ("more", "said"),
    [
        ({serve.REPEAT_VARIABLE: "yes"}, "must be 1 or 0"),
        ({"BEACON_LIVE_SOURCES": "nws"}, "is for the invented scenario"),
        ({"BEACON_INGEST": "0"}, "needs the pollers"),
    ],
)
def test_the_story_is_told_again_only_where_it_is_told(more: dict[str, str], said: str) -> None:
    before = files()

    with pytest.raises(serve.ServeError, match=said):
        serve.run(env() | more, lambda app: Stub(app))

    assert files() == before
    with pytest.raises(IngestConfigError):
        serve.repeats(env() | {"BEACON_LIVE_SOURCES": "openmhz"})
    assert serve.repeats({}) is False
    assert serve.repeats(env()) is True


class Stub:
    """Stands where uvicorn's server is: it runs the app's own startup and
    shutdown, and serves until it is told to stop."""

    def __init__(self, app: FastAPI, started: Callable[["Stub"], None] | None = None) -> None:
        self.app = app
        self.started = False
        self.should_exit = False
        self._started = started

    async def serve(self) -> None:
        async with self.app.router.lifespan_context(self.app):
            if self._started is not None:
                self._started(self)
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.005)


def test_a_server_whose_story_is_over_stops_and_starts_on_a_new_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve, "CHECK_SECONDS", 0.01)
    late = [timedelta(0)]
    tellings: list[FastAPI] = []
    seen: list[tuple[str | None, int]] = []

    def taps_of(app: FastAPI) -> int:
        return len(app.state.taps.store.search(kinds=["tap"], limit=10))

    def first(server: Stub) -> None:
        app = server.app
        # A tap sent during the first telling.
        app.state.taps.store.add(
            [
                NewEvidence(
                    kind="tap",
                    source="app",
                    ref="a-tap",
                    occurred_at=datetime.now(UTC),
                    confidence="low",
                    origin="live",
                )
            ]
        )
        seen.append((TestClient(app).get("/api/health").json()["repeats_text"], taps_of(app)))
        # And then the time at which its story is over.
        late[0] = timedelta(minutes=90)

    def second(server: Stub) -> None:
        late[0] = timedelta(0)
        app = server.app
        seen.append((TestClient(app).get("/api/health").json()["repeats_text"], taps_of(app)))
        # Stopped as a signal stops it.
        server.should_exit = True

    between: list[bool] = []

    def make_server(app: FastAPI) -> Stub:
        tellings.append(app)
        # Between two tellings, with one server stopped and the next not begun,
        # the stores are still this process's: nothing else opens one in the gap.
        try:
            DataLock(data_dir(), shared=True).close()
            between.append(False)
        except DataLockedError:
            between.append(True)
        return Stub(app, first if len(tellings) == 1 else second)

    status = serve.run(env(), make_server, clock=lambda: datetime.now(UTC) + late[0])

    # Two tellings, each on an app of its own. The first held the tap, and the
    # second began on a store that does not: the tap went with the story.
    assert status == 0
    assert between == [True, True]
    # And when the process is done, the stores are anybody's again.
    DataLock(data_dir(), shared=False).close()
    assert len(tellings) == 2
    assert tellings[0] is not tellings[1]
    said = serve.REPEATS + serve.REPEATS_MEDIA
    assert seen == [(said, 1), (said, 0)]
    assert "the taps sent before and their answers are deleted" in serve.REPEATS


def test_a_server_that_is_not_told_to_repeat_serves_its_story_as_it_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve, "CHECK_SECONDS", 0.01)
    over = served()
    before = {name for name in files() if name.endswith("mock.sqlite3")}
    said: list[str | None] = []

    def started(server: Stub) -> None:
        said.append(TestClient(server.app).get("/api/health").json()["repeats_text"])
        server.should_exit = True

    plain = {ENV_KEY: os.environ[ENV_KEY]}
    status = serve.run(plain, lambda app: Stub(app, started), clock=lambda: over + timedelta(1))

    # Nothing was deleted, and the health names no repeating.
    assert (status, said) == (0, [None])
    assert before <= set(files())


def test_a_story_that_cannot_be_begun_again_is_served_as_it_stands_and_not_stopped_for(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(serve, "CHECK_SECONDS", 0.01)
    over = served()

    def fails(path: Path) -> None:
        raise OSError("no")

    monkeypatch.setattr(serve, "_delete", fails)
    made: list[FastAPI] = []
    said: list[str | None] = []

    async def stop_later(server: Stub) -> None:
        await asyncio.sleep(0.1)
        server.should_exit = True

    def started(server: Stub) -> None:
        said.append(TestClient(server.app).get("/api/health").json()["repeats_text"])
        # Several askings later, it is stopped as a signal stops it: it was not
        # stopped for its story being over, though it is.
        server._stopping = asyncio.ensure_future(stop_later(server))  # type: ignore[attr-defined]

    def make_server(app: FastAPI) -> Stub:
        made.append(app)
        return Stub(app, started)

    status = serve.run(env(), make_server, clock=lambda: over + timedelta(hours=1))

    assert (status, len(made), said) == (0, 1, [None])
    assert "could not be begun again" in caplog.text
    assert (data_dir() / "evidence" / "mock.sqlite3").is_file()


def test_a_telling_that_did_not_stop_whole_ends_the_process_and_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A poller that did not stop in its time can still have the store open. Nothing
    # is deleted under it, and no second server is begun beside it.
    monkeypatch.setattr(serve, "CHECK_SECONDS", 0.01)

    async def not_whole(ingest: object) -> bool:
        await stop(ingest)  # type: ignore[arg-type]
        return False

    stop = beacon.main._stop_ingest
    monkeypatch.setattr(beacon.main, "_stop_ingest", not_whole)
    made: list[FastAPI] = []

    def make_server(app: FastAPI) -> Stub:
        made.append(app)
        return Stub(app)

    left: list[bool] = []

    def leave() -> None:
        # Where the process is ended: while the loop that served still runs, which
        # is before anything waits for the threads that did not stop.
        left.append(asyncio.get_running_loop().is_running())

    status = serve.run(
        env(), make_server, clock=lambda: datetime.now(UTC) + timedelta(hours=3), leave=leave
    )

    assert (status, len(made), left) == (serve.LEFT_RUNNING, 1, [True])
    assert made[0].state.stopped_whole is False
    assert "did not stop whole" in caplog.text
    assert {f"{d}/{n}" for d, n in serve.MOCK_FILES} <= set(files())
    # The lock is not given up: a thread may still have a store open. It is gone
    # with the process.
    with pytest.raises(DataLockedError):
        DataLock(data_dir(), shared=True)


@pytest.mark.parametrize("repeat", ["1", "0"])
def test_a_server_stopped_as_asked_that_did_not_stop_whole_keeps_the_stores(
    monkeypatch: pytest.MonkeyPatch, repeat: str
) -> None:
    # Not only a telling that was over: a server stopped by a signal can leave a
    # worker behind as well.
    async def not_whole(ingest: object) -> bool:
        await stop(ingest)  # type: ignore[arg-type]
        return False

    stop = beacon.main._stop_ingest
    monkeypatch.setattr(beacon.main, "_stop_ingest", not_whole)
    left: list[int] = []

    def started(server: Stub) -> None:
        server.should_exit = True

    status = serve.run(
        env() | {serve.REPEAT_VARIABLE: repeat},
        lambda app: Stub(app, started),
        leave=lambda: left.append(1),
    )

    # Only the invented scenario's server that tells its story again is ended at
    # once. Any other ends as it would, and the lock is the process's until then.
    assert (status, left) == ((serve.LEFT_RUNNING, [1]) if repeat == "1" else (0, []))
    with pytest.raises(DataLockedError):
        DataLock(data_dir(), shared=True)


def test_a_server_run_by_itself_keeps_the_stores_when_it_did_not_stop_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `uvicorn beacon.main:app`, which holds the lock itself.
    async def not_whole(ingest: object) -> bool:
        await stop(ingest)  # type: ignore[arg-type]
        return False

    stop = beacon.main._stop_ingest
    monkeypatch.setattr(beacon.main, "_stop_ingest", not_whole)
    with TestClient(create_app()):
        pass

    with pytest.raises(DataLockedError):
        DataLock(data_dir(), shared=True)


@pytest.mark.parametrize("shared", [True, False])
def test_a_refused_lock_never_advises_a_second_data_directory(shared: bool) -> None:
    # A second directory has a ledger and claims of its own: the same work could
    # be paid for twice.
    with alone(), pytest.raises(DataLockedError) as refused:
        DataLock(data_dir(), shared=shared)

    said = str(refused.value)
    assert "Stop" in said
    assert "this one data directory" in said
    assert "BEACON_DATA_DIR" not in said
    assert "of its own" not in said


def test_a_server_that_could_not_start_and_left_something_running_keeps_the_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What had begun before the start failed is ended as on any stop, and may not
    # all have ended.
    async def not_whole(ingest: object) -> bool:
        await stop(ingest)  # type: ignore[arg-type]
        return False

    def fails(*args: object) -> None:
        raise beacon.main.ServerSetupError("no")

    stop = beacon.main._stop_ingest
    monkeypatch.setattr(beacon.main, "_stop_ingest", not_whole)
    monkeypatch.setattr(beacon.main, "open_push", fails)

    class Failing(Stub):
        async def serve(self) -> None:
            # As uvicorn does with a startup that failed: it never says it started.
            with contextlib.suppress(beacon.main.ServerSetupError):
                await super().serve()

    left: list[int] = []
    status = serve.run(env(), Failing, leave=lambda: left.append(1))

    assert (status, left) == (serve.LEFT_RUNNING, [1])
    with pytest.raises(DataLockedError):
        DataLock(data_dir(), shared=True)


def test_a_server_that_stopped_says_so_only_when_every_part_of_it_did() -> None:
    app = create_app()
    with TestClient(app):
        assert not hasattr(app.state, "stopped_whole")
    assert app.state.stopped_whole is True


def test_a_websocket_handshake_is_not_taken_and_its_path_is_in_no_log() -> None:
    # uvicorn, with a WebSocket library installed, answers a handshake itself and
    # writes the path it was for to its error log, which no access-log setting
    # turns off. A tap's id is in the path of its routes.
    with closing(socket.socket()) as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    app = FastAPI()
    server = serve.uvicorn_server(app, "127.0.0.1", port)
    assert (server.config.ws, server.config.access_log, server.config.proxy_headers) == (
        "none",
        False,
        False,
    )
    lines: list[str] = []

    class Kept(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    kept = Kept(level=logging.DEBUG)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()))
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        watched = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "")]
        for logger in watched:
            logger.addHandler(kept)
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=5)) as asking:
                asking.sendall(
                    b"GET /api/taps/the-id-of-a-tap/run?also=this HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    b"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                answer = asking.recv(4096)
        finally:
            for logger in watched:
                logger.removeHandler(kept)
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    # Answered as a request to a path nobody has, not as a handshake.
    assert answer.startswith(b"HTTP/1.1 404")
    assert lines, "uvicorn says that it takes no upgrade, which shows the log was heard"
    assert not any("the-id-of-a-tap" in line or "also=this" in line for line in lines)


def test_a_server_that_could_not_start_ends_with_a_failure_and_is_not_started_again() -> None:
    class Failing:
        started = False
        should_exit = False

        async def serve(self) -> None:
            return None

    made: list[FastAPI] = []

    def make_server(app: FastAPI) -> Failing:
        made.append(app)
        return Failing()

    assert serve.run(env(), make_server) == 3
    assert len(made) == 1


def test_a_setting_that_cannot_be_kept_to_is_said_in_a_sentence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert serve.main([], env() | {"BEACON_INGEST": "0"}) == 2
    assert capsys.readouterr().err.startswith("The server was not started. BEACON_MOCK_REPEAT=1")
