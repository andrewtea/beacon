"""`python -m beacon.serve`: the server as a deployment runs it, and the invented
story begun again when it is over."""

import asyncio
import contextlib
import logging
import os
import socket
import sqlite3
import sys
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
from beacon.datadir import LOCK_FILE, PENDING, DataLock, DataLockedError, HalfAsideError
from beacon.evidence import EvidenceStore, NewEvidence, Point
from beacon.ingest.setup import MOCK_HEAD_START, IngestConfigError
from beacon.investigate import Runs
from beacon.investigate import aside as aside_module
from beacon.investigate.aside import sweep_aside
from beacon.main import create_app
from beacon.push import store as push_store
from beacon.push.store import Place, Subscriptions
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


@pytest.mark.parametrize("beside", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("how", ["a link", "a second name"])
@pytest.mark.parametrize("there", [True, False])
def test_what_sqlite_keeps_beside_a_file_is_not_another_stores_under_a_link(
    beside: str, how: str, there: bool
) -> None:
    # SQLite opens the log beside a database, and emptying a log writes to it. A
    # log that is another store's, under a link, is found before SQLite opens
    # anything: whether or not the file itself is there, since a new file of the
    # name would be opened with what is beside it.
    over = served()
    theirs = data_dir() / "push" / "kept-beside-the-subscriptions"
    theirs.write_bytes(b"what another store's log holds" * 50)
    path = data_dir() / "investigate" / "mock.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for each in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + each).unlink(missing_ok=True)
    if not there:
        path.unlink()
    linked = path.with_name(path.name + beside)
    if how == "a link":
        linked.symlink_to(theirs)
    else:
        os.link(theirs, linked)
    before = kept()
    made: list[FastAPI] = []

    with pytest.raises(serve.NotTheMockStoresError, match="is a link"):
        begin_again(over + timedelta(hours=1))
    with pytest.raises(serve.NotTheMockStoresError):
        serve.run(env(), lambda app: made.append(app) or Stub(app))  # type: ignore[func-returns-value]

    assert made == []
    assert theirs.read_bytes() == b"what another store's log holds" * 50
    assert kept() == before
    assert linked.is_symlink() or linked.stat().st_nlink == 2


@pytest.mark.parametrize(
    "failing", ["evidence", "investigate/mock.sqlite3-wal", "story/mock.sqlite3"]
)
@pytest.mark.parametrize("error", [PermissionError(13, "no"), OSError(5, "the disk")])
def test_a_file_that_could_not_be_looked_at_was_not_seen_to_be_the_mock_stores(
    monkeypatch: pytest.MonkeyPatch, failing: str, error: OSError
) -> None:
    # An error from looking is not "the file could not be deleted just now",
    # after which the server goes on: nothing was established about the file, so
    # no app is made on it.
    over = served()
    lstat = os.lstat

    def fails(path: os.PathLike[str] | str, *args: object, **more: object) -> os.stat_result:
        if str(path).endswith(failing):
            raise error
        return lstat(path, *args, **more)  # type: ignore[arg-type]

    monkeypatch.setattr(serve.os, "lstat", fails)
    before = kept()
    made: list[FastAPI] = []

    with pytest.raises(serve.NotTheMockStoresError, match="Nothing was deleted"):
        begin_again(over + timedelta(hours=1))
    with pytest.raises(serve.NotTheMockStoresError):
        serve.run(env(), lambda app: made.append(app) or Stub(app))  # type: ignore[func-returns-value]

    assert made == []
    assert kept() == before
    # The stores are given up: nothing of a server was begun.
    monkeypatch.setattr(serve.os, "lstat", lstat)
    DataLock(data_dir(), shared=False).close()


def test_the_tables_sqlite_makes_by_itself_do_not_make_a_file_another_kinds() -> None:
    # `ANALYZE` leaves the planner's statistics in a table of SQLite's own. A file
    # with it is still its kind's, and the story is begun again.
    over = served()
    for directory, name in serve.MOCK_FILES:
        with closing(sqlite3.connect(data_dir() / directory / name)) as db:
            db.execute("ANALYZE")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        assert "sqlite_stat1" in tables

    assert begin_again(over) is True

    # A table that is neither the kind's nor SQLite's still makes it nobody's.
    over = served()
    with closing(sqlite3.connect(data_dir() / "story" / "mock.sqlite3")) as db, db:
        db.execute("CREATE TABLE somebody_elses (n)")
    with pytest.raises(serve.NotTheMockStoresError):
        begin_again(over)


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


# Setting a store aside, so that a story is begun anew by hand.


def plain() -> dict[str, str]:
    """A server's environment with nothing that tells the story again."""
    return {ENV_KEY: os.environ[ENV_KEY]}


def set_aside(now: datetime) -> object:
    # As the command holds the stores: alone, and let in while a move is unfinished.
    lock = DataLock(data_dir(), shared=False, finishing=True)
    try:
        return serve.set_aside(plain(), now, lock)
    finally:
        lock.close()


ASIDE_AT = datetime(2026, 12, 1, 18, 30, 5, tzinfo=UTC)
ASIDE_IN = "told/20261201T183005Z"
ADDRESS = "https://fcm.googleapis.com/fcm/send/dXY2zQ:APA91bH_-secretSecretSecret"
SPOT = Point(37.776, -122.424)


def test_a_store_set_aside_is_moved_whole_and_nothing_is_deleted() -> None:
    served()
    root = data_dir()
    # What a server on real sources keeps, the ledger, and a device's subscription
    # are not the story's, and stay where they are.
    for directory in ("evidence", "investigate", "story"):
        (root / directory / "live.sqlite3").write_bytes(b"a live store's")
    before = kept()
    mock = [f"{directory}/{name}" for directory, name in serve.MOCK_FILES]
    assert set(mock) <= set(before)

    assert str(set_aside(ASIDE_AT)) == ASIDE_IN

    after = kept()
    # Each file is where it was moved to, to the byte, and no longer where it was.
    assert {name: after[f"{ASIDE_IN}/{name}"] for name in mock} == {
        name: before[name] for name in mock
    }
    assert not any(name in after for name in mock)
    assert {name: held for name, held in before.items() if name not in mock} == {
        name: held for name, held in after.items() if not name.startswith("told/")
    }
    # It is still the store it was: its story can be read where it now is.
    moved = EvidenceStore(root / ASIDE_IN / "evidence" / "mock.sqlite3", "mock")
    try:
        assert isinstance(moved.state("scenario")["start"], str)
    finally:
        moved.close()
    # With no mock store there is nothing to set aside, and no directory is made.
    assert set_aside(ASIDE_AT + timedelta(minutes=1)) is None
    assert [path.name for path in (root / "told").iterdir()] == ["20261201T183005Z"]


def test_nothing_is_made_for_asking_where_there_is_no_mock_store() -> None:
    assert set_aside(ASIDE_AT) is None
    assert files() == {}
    assert not (data_dir() / "told").exists()


def test_the_server_started_after_a_store_was_set_aside_begins_the_story_anew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    over = served()
    assert set_aside(ASIDE_AT) is not None

    # At the minute it is told to, which a store whose story had begun would not.
    monkeypatch.setenv("BEACON_MOCK_STORY_MINUTE", "77")
    anew = served()

    assert anew != over
    assert anew - datetime.now(UTC) < serve.PLAYED_FOR - timedelta(minutes=76)
    # Set aside at the same second, the second store gets a directory of its own.
    assert str(set_aside(ASIDE_AT)) == ASIDE_IN + "-2"
    assert {path.name for path in (data_dir() / "told").iterdir()} == {
        "20261201T183005Z",
        "20261201T183005Z-2",
    }


def test_a_store_muse_ember_worked_on_is_set_aside_as_it_is_and_never_deleted() -> None:
    over = served()
    root = data_dir()
    store = EvidenceStore(root / "evidence" / "mock.sqlite3", "mock")
    store.mark_paid()
    store.close()
    # A server that tells the story again refuses the store, whatever comes of it.
    with pytest.raises(serve.ServeError, match="Nothing was deleted"):
        begin_again(over)
    before = kept()["evidence/mock.sqlite3"]

    assert str(set_aside(ASIDE_AT)) == ASIDE_IN

    # The claims on what was paid for are where the store is, and it still says
    # whose it is.
    assert kept()[f"{ASIDE_IN}/evidence/mock.sqlite3"] == before
    moved = EvidenceStore(root / ASIDE_IN / "evidence" / "mock.sqlite3", "mock")
    try:
        assert moved.paid
    finally:
        moved.close()


@pytest.mark.parametrize("how", ["the file", "a second name"])
def test_a_link_to_another_stores_file_is_not_set_aside_in_its_place(how: str) -> None:
    served()
    folder = data_dir() / "investigate"
    for each in folder.iterdir():
        each.unlink()
    if how == "the file":
        (folder / "mock.sqlite3").symlink_to(push_file())
    else:
        os.link(push_file(), folder / "mock.sqlite3")
    before = kept()

    with pytest.raises(serve.ServeError, match=r"under investigate/ .* Nothing was moved"):
        set_aside(ASIDE_AT)

    assert kept() == before
    assert not (data_dir() / "told").exists()


def test_a_file_something_still_has_open_is_not_set_aside_and_neither_is_any_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve, "_WAIT_SECONDS", 0.05)
    served()
    path = data_dir() / "evidence" / "mock.sqlite3"
    with closing(sqlite3.connect(path)) as reading, closing(sqlite3.connect(path)) as writing:
        reading.execute("BEGIN")
        reading.execute("SELECT count(*) FROM evidence").fetchall()
        with writing:
            writing.execute(
                "INSERT INTO poller_state (name, state, updated_ms) VALUES ('since', '{}', 0)"
            )

        with pytest.raises(serve.ServeError, match=r"is open somewhere.* Nothing was moved"):
            set_aside(ASIDE_AT)

        assert {f"{d}/{n}" for d, n in serve.MOCK_FILES} <= set(files())
        assert not (data_dir() / "told").exists()
    assert set_aside(ASIDE_AT) is not None


def test_there_has_to_be_a_directory_of_the_data_directorys_own_to_move_a_store_to(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    served()
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (data_dir() / "told").symlink_to(elsewhere, target_is_directory=True)
    before = kept()

    with pytest.raises(
        serve.ServeError, match=r"is not a directory of its own.* Nothing was moved"
    ):
        set_aside(ASIDE_AT)

    assert kept() == before
    assert list(elsewhere.iterdir()) == []


def _fails_on(
    monkeypatch: pytest.MonkeyPatch, directory: str, step: str = "unlink"
) -> Callable[[], None]:
    """Have a step of the move fail for a file where the store keeps it, under
    `directory`: giving it its second name (`link`), or taking away the name it
    had (`unlink`). Answers what mends it."""
    real = getattr(os, step)

    def fails(path: Path, *more: object, **how: object) -> None:
        if Path(path).parent == data_dir() / directory:
            raise OSError("no")
        real(path, *more, **how)

    monkeypatch.setattr(serve.os, step, fails)
    return lambda: monkeypatch.setattr(serve.os, step, real)


def test_a_move_that_was_cut_short_keeps_everything_out_until_the_next_finishes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served()
    mend = _fails_on(monkeypatch, "story")
    with pytest.raises(serve.PartlyAsideError, match="only in part") as refused:
        set_aside(ASIDE_AT)
    assert "told/20261201T183005Z/" in str(refused.value)
    assert "Nothing was deleted" in str(refused.value)
    # Nothing of a path, or of the error that stopped it, is under the sentence.
    assert refused.value.__context__ is None
    assert not (data_dir() / "evidence" / "mock.sqlite3").exists()
    left = [f"{d}/{n}" for d, n in serve.MOCK_FILES if d != "evidence"]
    assert all(name in files() for name in left)
    assert (data_dir() / "told" / PENDING).read_text() == "20261201T183005Z"
    # Every file of the store has its name under told/ from before the first
    # was taken from its place: the ones still in place are there too, as the
    # same file. So a Beacon from before this command, which knows nothing of
    # the note, takes away a name and never the file, and its own look at a file
    # with two names refuses it.
    for directory, name in serve.MOCK_FILES:
        assert (data_dir() / ASIDE_IN / directory / name).is_file()
    for name in left:
        assert (data_dir() / name).samefile(data_dir() / ASIDE_IN / name)
        with pytest.raises(serve.NotTheMockStoresError):
            serve._own(data_dir(), *name.split("/"))

    # The stories and the investigations that are left hold the claims on what was
    # paid for. Nothing begins a story beside them, and nothing deletes them: no
    # server, with the pollers or without, no poller, and no telling again.
    before = files()
    for shared in (False, True):
        with pytest.raises(HalfAsideError, match="did not finish"):
            DataLock(data_dir(), shared=shared)
    with pytest.raises(HalfAsideError), TestClient(create_app()):
        pass
    assert serve.main([], env()) == 3
    assert "did not finish" in capsys.readouterr().err
    assert files() == before

    # The command, while the file still cannot be moved, says the same and exits
    # as a failure that is not "nothing was set aside".
    assert serve.main(["--set-aside"], plain()) == 3
    said = capsys.readouterr().err
    assert said.startswith("The mock store's files were set aside only in part")
    assert "Nothing was set aside" not in said
    assert files() == before

    # Given again, it moves what is left to the same place, a later moment or not,
    # with what SQLite kept beside a file that went before.
    stale = data_dir() / "evidence" / "mock.sqlite3-wal"
    stale.write_bytes(b"")
    mend()
    assert str(set_aside(ASIDE_AT + timedelta(hours=1))) == ASIDE_IN
    assert all(f"{ASIDE_IN}/{name}" in files() for name in left)
    assert not any(name in files() for name in left)
    assert not stale.exists()
    assert (data_dir() / ASIDE_IN / "evidence" / stale.name).is_file()
    assert not (data_dir() / "told" / PENDING).exists()
    assert [path.name for path in (data_dir() / "told").iterdir()] == ["20261201T183005Z"]
    with alone():
        pass


@pytest.mark.parametrize(
    ("directory", "step"),
    # The last file to be given its second name, and the first to leave its place.
    [("investigate", "link"), ("evidence", "unlink")],
)
def test_a_move_of_which_no_file_went_leaves_nothing_behind_and_keeps_nobody_out(
    monkeypatch: pytest.MonkeyPatch, directory: str, step: str
) -> None:
    served()
    before = kept()
    _fails_on(monkeypatch, directory, step)

    with pytest.raises(serve.ServeError, match="Nothing was moved") as refused:
        set_aside(ASIDE_AT)

    assert not isinstance(refused.value, serve.PartlyAsideError)
    assert kept() == before
    assert list((data_dir() / "told").iterdir()) == []
    # No file is left with a second name.
    for kind, name in serve.MOCK_FILES:
        assert (data_dir() / kind / name).stat().st_nlink == 1
    with alone():
        pass


@pytest.mark.parametrize("note", ["", "../evidence", "20261201T183005Z", "x" * 4000])
def test_a_note_that_names_no_directory_of_the_commands_own_moves_nothing(note: str) -> None:
    # The last is of the right shape and names a directory that is not there.
    served()
    (data_dir() / "told").mkdir()
    (data_dir() / "told" / PENDING).write_text(note)
    before = kept()

    with pytest.raises(serve.PartlyAsideError, match="does not name a directory"):
        set_aside(ASIDE_AT)

    assert kept() == before
    with pytest.raises(HalfAsideError):
        DataLock(data_dir(), shared=True)


@pytest.mark.parametrize("how", ["a link", "a second name"])
def test_a_note_that_is_not_a_file_of_its_own_is_not_read(
    how: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    served()
    (data_dir() / ASIDE_IN).mkdir(parents=True)
    written = tmp_path_factory.mktemp("elsewhere") / "note"
    written.write_text("20261201T183005Z")
    note = data_dir() / "told" / PENDING
    if how == "a link":
        note.symlink_to(written)
    else:
        os.link(written, note)
    before = kept()

    with pytest.raises(serve.PartlyAsideError, match="does not name a directory"):
        set_aside(ASIDE_AT)

    assert kept() == before


def test_a_move_is_not_gone_on_with_into_a_directory_that_is_a_link(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    served()
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (data_dir() / ASIDE_IN).mkdir(parents=True)
    (data_dir() / ASIDE_IN / "evidence").symlink_to(elsewhere, target_is_directory=True)
    (data_dir() / "told" / PENDING).write_text("20261201T183005Z")
    before = kept()

    with pytest.raises(serve.PartlyAsideError, match="only in part"):
        set_aside(ASIDE_AT)

    assert list(elsewhere.iterdir()) == []
    assert kept() == before
    with pytest.raises(HalfAsideError):
        DataLock(data_dir(), shared=True)


def test_a_note_is_written_whole_or_not_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    served()
    before = kept()
    # What a try that was stopped while it wrote its note left: no note.
    (data_dir() / "told").mkdir()
    (data_dir() / "told" / (PENDING + ".part")).write_text("2026")
    with alone():
        pass
    replace = os.replace

    def stopped(old: Path, new: Path) -> None:
        raise OSError("no")

    monkeypatch.setattr(serve.os, "replace", stopped)
    with pytest.raises(serve.ServeError, match="Nothing was moved"):
        set_aside(ASIDE_AT)
    assert kept() == before
    assert list((data_dir() / "told").iterdir()) == []

    monkeypatch.setattr(serve.os, "replace", replace)
    assert str(set_aside(ASIDE_AT)) == ASIDE_IN
    assert [path.name for path in (data_dir() / "told").iterdir()] == ["20261201T183005Z"]


def test_a_file_that_could_not_be_looked_at_is_not_taken_to_be_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Else the note would go with a file of the store still in its place.
    served()
    seen = serve._seen
    story = data_dir() / "story" / "mock.sqlite3"

    def unseen(path: Path) -> os.stat_result | None:
        if path == story:
            raise PermissionError("no")
        return seen(path)

    monkeypatch.setattr(serve, "_seen", unseen)
    with pytest.raises(serve.ServeError, match="Nothing was moved"):
        set_aside(ASIDE_AT)

    assert story.is_file()
    assert not (data_dir() / "told" / PENDING).exists()


def test_a_note_that_is_a_pipe_is_refused_and_not_waited_for() -> None:
    served()
    (data_dir() / ASIDE_IN).mkdir(parents=True)
    os.mkfifo(data_dir() / "told" / PENDING)
    done: list[BaseException | None] = []

    def give() -> None:
        try:
            set_aside(ASIDE_AT)
        except BaseException as error:
            done.append(error)
        else:
            done.append(None)

    thread = threading.Thread(target=give, daemon=True)
    thread.start()
    thread.join(10)

    assert not thread.is_alive()
    assert isinstance(done[0], serve.PartlyAsideError)
    assert "does not name a directory" in str(done[0])


def test_a_note_of_which_only_a_part_was_written_is_never_given_the_notes_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served()
    before = kept()
    write = os.write

    def short(descriptor: int, data: bytes) -> int:
        return write(descriptor, data[:4])

    monkeypatch.setattr(serve.os, "write", short)
    with pytest.raises(serve.ServeError, match="Nothing was moved"):
        set_aside(ASIDE_AT)

    assert kept() == before
    assert list((data_dir() / "told").iterdir()) == []
    with alone():
        pass


def test_the_disk_is_asked_to_keep_every_new_name_before_a_name_is_taken_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `told/` is an entry of the data directory's: its listing is kept too.
    served()
    steps: list[tuple[str, Path]] = []
    keep, unlink = serve._kept, os.unlink

    def kept_(directory: Path) -> None:
        steps.append(("kept", directory))
        keep(directory)

    def unlink_(path: Path, *more: object, **how: object) -> None:
        steps.append(("unlink", Path(path)))
        unlink(path, *more, **how)

    monkeypatch.setattr(serve, "_kept", kept_)
    monkeypatch.setattr(serve.os, "unlink", unlink_)
    assert set_aside(ASIDE_AT) is not None

    first = next(
        index
        for index, (what, path) in enumerate(steps)
        if what == "unlink" and path.name == "mock.sqlite3"
    )
    asked = [path for what, path in steps[:first] if what == "kept"]
    told = data_dir() / "told"
    assert {data_dir(), told, told / "20261201T183005Z"} <= set(asked)
    assert {told / "20261201T183005Z" / kind for kind, _ in serve.MOCK_FILES} <= set(asked)


def test_a_move_that_is_finished_is_not_said_to_be_unfinished_when_the_disk_does_not_confirm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served()
    keep = serve._kept
    note = data_dir() / "told" / PENDING

    def fails_once_the_note_is_gone(directory: Path) -> None:
        if directory == data_dir() / "told" and not note.exists() and any(directory.iterdir()):
            raise OSError("no")
        keep(directory)

    monkeypatch.setattr(serve, "_kept", fails_once_the_note_is_gone)
    assert serve.main(["--set-aside"], plain()) == 0
    said = capsys.readouterr()
    assert "were moved to told/" in said.out
    assert "The disk did not confirm that it kept that last step" in said.out
    assert "only in part" not in said.out + said.err
    # What it says is so: nothing is kept out, and nothing is left to move.
    assert not note.exists()
    with alone():
        pass
    assert not any(f"{d}/{n}" in files() for d, n in serve.MOCK_FILES)


@pytest.mark.parametrize("shape", [0, 1])
def test_investigations_in_a_shape_that_is_not_swept_as_it_is_are_not_set_aside(shape: int) -> None:
    # Set aside, an expired run in such a file would never be deleted.
    served()
    file = data_dir() / "investigate" / "mock.sqlite3"
    _claimed(file, "tap-old", ASIDE_AT - timedelta(days=3))
    with closing(sqlite3.connect(file)) as db:
        db.execute(f"PRAGMA user_version = {shape}")
    before = kept()

    with pytest.raises(serve.ServeError, match=r"a shape an earlier Beacon.*Nothing was moved"):
        set_aside(ASIDE_AT)

    assert kept() == before
    assert not (data_dir() / "told").exists()
    # A server makes the file this shape, as the sentence says, and then it goes.
    served()
    assert set_aside(ASIDE_AT) is not None


def test_a_story_begun_anew_is_not_kept_from_a_device_by_what_the_last_telling_sent() -> None:
    # An incident's id is made from its first call's start, to the second: two
    # tellings that count their first minute from the same second share it.
    served()
    place = Place(SPOT, ASIDE_AT)
    devices = Subscriptions(push_file(), "mock")
    try:
        devices.put(ADDRESS, [SPOT], ASIDE_AT)
        permit = devices.permit(ASIDE_AT)
        assert devices.claim(ADDRESS, "inc-1", place, ASIDE_AT) is True
        devices.settle(ADDRESS, "inc-1", "sent")
        assert devices.claim(ADDRESS, "inc-1", place, ASIDE_AT) is False
    finally:
        devices.close()

    assert set_aside(ASIDE_AT) is not None

    # The subscriptions file is not moved, and keeps the device and its permit.
    assert not any(name.startswith(f"{ASIDE_IN}/push") for name in files())
    devices = Subscriptions(push_file(), "mock")
    try:
        assert [kept.endpoint for kept in devices.all()] == [ADDRESS]
        assert devices.claim(ADDRESS, "inc-1", place, ASIDE_AT) is True
        assert devices.put(ADDRESS, [SPOT], ASIDE_AT, permit=permit) is not None
    finally:
        devices.close()


def _sent_once(place: Place) -> None:
    devices = Subscriptions(push_file(), "mock")
    try:
        devices.put(ADDRESS, [SPOT], ASIDE_AT)
        assert devices.claim(ADDRESS, "inc-1", place, ASIDE_AT) is True
    finally:
        devices.close()


def _claimed_again(place: Place) -> bool:
    devices = Subscriptions(push_file(), "mock")
    try:
        return devices.claim(ADDRESS, "inc-1", place, ASIDE_AT)
    finally:
        devices.close()


def test_what_was_sent_is_not_forgotten_when_a_store_is_not_set_aside(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The story goes on, and its incident would be sent to the device again.
    served()
    place = Place(SPOT, ASIDE_AT)
    _sent_once(place)

    # Nowhere to move to.
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (data_dir() / "told").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(serve.ServeError, match="Nothing was moved"):
        set_aside(ASIDE_AT)
    assert _claimed_again(place) is False
    (data_dir() / "told").unlink()

    # A first file that does not go.
    _fails_on(monkeypatch, "evidence")
    with pytest.raises(serve.ServeError, match="Nothing was moved"):
        set_aside(ASIDE_AT)
    assert _claimed_again(place) is False


@pytest.mark.parametrize("how", ["it could not be written to", "it is not what it was"])
def test_a_move_is_not_finished_until_what_was_sent_is_forgotten(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    served()
    place = Place(SPOT, ASIDE_AT)
    _sent_once(place)
    forget = push_store.forget_sent

    def fails(path: Path, mode: str) -> int | None:
        if how == "it could not be written to":
            raise sqlite3.OperationalError("database is locked")
        return None

    monkeypatch.setattr(push_store, "forget_sent", fails)
    with pytest.raises(serve.PartlyAsideError, match="could not be forgotten") as refused:
        set_aside(ASIDE_AT)
    assert refused.value.__context__ is None
    # Every file went, and still no server starts: one would begin a story whose
    # incident the device was already sent.
    assert not any(f"{d}/{n}" in files() for d, n in serve.MOCK_FILES)
    with pytest.raises(HalfAsideError):
        DataLock(data_dir(), shared=False)

    monkeypatch.setattr(push_store, "forget_sent", forget)
    assert str(set_aside(ASIDE_AT)) == ASIDE_IN
    assert _claimed_again(place) is True
    with alone():
        pass


def _push_counts() -> tuple[int, int, int, int]:
    """The subscriptions file's shape, and how many subscriptions, permits, and
    rows of what was sent it holds, read without opening it for use."""
    with closing(sqlite3.connect(f"file:{push_file()}?mode=ro", uri=True)) as db:
        return (
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT count(*) FROM subscription").fetchone()[0],
            db.execute("SELECT count(*) FROM permit").fetchone()[0],
            db.execute("SELECT count(*) FROM sent").fetchone()[0],
        )


def test_setting_a_store_aside_changes_a_subscriptions_file_as_it_is() -> None:
    served()
    place = Place(SPOT, ASIDE_AT)
    _sent_once(place)
    devices = Subscriptions(push_file(), "mock")
    try:
        assert devices.permit(ASIDE_AT) is not None
    finally:
        devices.close()
    assert _push_counts() == (4, 1, 1, 1)

    # A file of the shape before: whatever opens it for use deletes its
    # subscriptions and its permits. Setting a store aside is not that, and
    # leaves all of it, what was sent included, to whoever opens it next.
    with closing(sqlite3.connect(push_file())) as db:
        db.execute("PRAGMA user_version = 3")
    assert set_aside(ASIDE_AT) is not None
    assert _push_counts() == (3, 1, 1, 1)

    # A file of this shape loses what was sent and nothing else.
    with closing(sqlite3.connect(push_file())) as db:
        db.execute("PRAGMA user_version = 4")
    served()
    assert set_aside(ASIDE_AT + timedelta(hours=1)) is not None
    assert _push_counts() == (4, 1, 1, 0)


@pytest.mark.parametrize("what", ["a newer shape", "another kind", "the live store's"])
def test_no_store_is_set_aside_beside_a_subscriptions_file_that_is_not_seen_to_be_one(
    what: str,
) -> None:
    served()
    _sent_once(Place(SPOT, ASIDE_AT))
    if what == "a newer shape":
        with closing(sqlite3.connect(push_file())) as db:
            db.execute("PRAGMA user_version = 99")
    else:
        for beside in ("", "-wal", "-shm"):
            push_file().with_name(push_file().name + beside).unlink(missing_ok=True)
        if what == "another kind":
            # It says `mock` too, in a shape number a subscriptions file also has.
            _claimed(push_file(), "tap-1", ASIDE_AT)
        else:
            Subscriptions(push_file(), "live").close()
    before = kept()

    with pytest.raises(serve.ServeError, match=r"under push/ could not be seen.*Nothing was moved"):
        set_aside(ASIDE_AT)

    assert kept() == before
    assert not (data_dir() / "told").exists()


def _claimed(file: Path, tap_id: str, at: datetime) -> None:
    runs = Runs(file, "mock")
    try:
        assert runs.claim(tap_id, at) is True
    finally:
        runs.close()


def _run_ids(file: Path) -> list[str]:
    with closing(sqlite3.connect(file)) as db:
        return [row[0] for row in db.execute("SELECT tap_id FROM run ORDER BY tap_id")]


def test_the_investigations_of_a_store_that_was_set_aside_are_deleted_when_their_day_is_over(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # A run holds a tap's id. Moving the file does not keep it past its day.
    served()
    _claimed(data_dir() / "investigate" / "mock.sqlite3", "tap-early", ASIDE_AT)
    _claimed(data_dir() / "investigate" / "mock.sqlite3", "tap-late", ASIDE_AT + timedelta(hours=2))
    assert set_aside(ASIDE_AT + timedelta(hours=3)) is not None
    moved = data_dir() / ASIDE_IN.replace("183005", "213005") / "investigate" / "mock.sqlite3"
    assert _run_ids(moved) == ["tap-early", "tap-late"]

    assert sweep_aside(data_dir(), ASIDE_AT + timedelta(hours=23)) == (0, 0)
    assert sweep_aside(data_dir(), ASIDE_AT + timedelta(hours=25)) == (1, 0)
    assert _run_ids(moved) == ["tap-late"]

    # A link under told/ could be to any file: it is not opened, and is counted.
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "mock.sqlite3"
    _claimed(elsewhere, "tap-elsewhere", ASIDE_AT)
    linked = data_dir() / "told" / "20261202T000000Z" / "investigate"
    linked.mkdir(parents=True)
    (linked / "mock.sqlite3").symlink_to(elsewhere)
    # And a directory of no name the command gives is not looked into.
    other = data_dir() / "told" / "mine" / "investigate"
    other.mkdir(parents=True)
    _claimed(other / "mock.sqlite3", "tap-mine", ASIDE_AT)

    # And a file of another kind is not swept as investigations, though it says
    # `mock` in a shape number an investigations file also has. No table is made
    # in it.
    another = data_dir() / "told" / "20261203T000000Z" / "investigate"
    another.mkdir(parents=True)
    Subscriptions(another / "mock.sqlite3", "mock").close()
    # Nor is a store with no investigations a file that could not be swept.
    (data_dir() / "told" / "20261204T000000Z" / "evidence").mkdir(parents=True)

    assert sweep_aside(data_dir(), ASIDE_AT + timedelta(days=3)) == (1, 2)
    assert _run_ids(moved) == []
    assert _run_ids(elsewhere) == ["tap-elsewhere"]
    assert _run_ids(other / "mock.sqlite3") == ["tap-mine"]
    with closing(sqlite3.connect(another / "mock.sqlite3")) as db:
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
    assert "run" not in tables


def test_an_archive_that_could_not_be_looked_at_is_counted_and_not_passed_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served()
    _claimed(data_dir() / "investigate" / "mock.sqlite3", "tap-early", ASIDE_AT)
    assert set_aside(ASIDE_AT) is not None
    moved = data_dir() / ASIDE_IN / "investigate" / "mock.sqlite3"
    lstat = os.lstat

    def unseen(path: Path) -> os.stat_result:
        if Path(path) == moved:
            raise PermissionError("no")
        return lstat(path)

    monkeypatch.setattr(aside_module.os, "lstat", unseen)
    assert sweep_aside(data_dir(), ASIDE_AT + timedelta(days=3)) == (0, 1)
    monkeypatch.setattr(aside_module.os, "lstat", lstat)
    assert _run_ids(moved) == ["tap-early"]


def test_a_server_sweeps_the_investigations_that_were_set_aside() -> None:
    served()
    long_ago = datetime.now(UTC) - timedelta(days=2)
    _claimed(data_dir() / "investigate" / "mock.sqlite3", "tap-old", long_ago)
    assert set_aside(ASIDE_AT) is not None
    moved = data_dir() / ASIDE_IN / "investigate" / "mock.sqlite3"
    assert _run_ids(moved) == ["tap-old"]

    with TestClient(create_app()):
        deadline = time.monotonic() + 5
        while _run_ids(moved) and time.monotonic() < deadline:
            time.sleep(0.02)

    assert _run_ids(moved) == []


def test_the_command_sets_aside_and_serves_nothing_and_is_refused_under_a_server(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served()
    monkeypatch.setattr(serve, "run", lambda *_, **__: pytest.fail("It served."))
    monkeypatch.setattr(serve, "uvicorn_server", lambda *_: pytest.fail("It served."))

    # A server has the stores: nothing is moved under it.
    with alone():
        assert serve.main(["--set-aside"], plain()) == 2
    assert capsys.readouterr().err.startswith("Nothing was set aside. ")
    assert not (data_dir() / "told").exists()
    # A server on real sources has no story to begin anew.
    assert serve.main(["--set-aside"], plain() | {"BEACON_LIVE_SOURCES": "nws"}) == 2
    assert "is for the invented scenario" in capsys.readouterr().err
    assert not (data_dir() / "told").exists()

    assert serve.main(["--set-aside"], plain()) == 0
    said = capsys.readouterr().out
    assert "were moved to told/" in said
    assert "begins the invented story anew" in said
    assert not (data_dir() / "evidence" / "mock.sqlite3").exists()
    # The stores are given up: the server that is started next has them.
    with alone():
        pass
    assert serve.main(["--set-aside"], plain()) == 0
    assert "There is no mock store" in capsys.readouterr().out


def test_a_server_that_tells_the_story_again_begins_it_at_the_twentieth_minute_only() -> None:
    # What such a server says of how often the story begins is worked out from it.
    with pytest.raises(serve.ServeError, match="BEACON_MOCK_STORY_MINUTE is not set beside it"):
        serve.repeats(env() | {"BEACON_MOCK_STORY_MINUTE": "20"})


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


class Uvicorns(Stub):
    """Ends a server that could not start as uvicorn does: the app's shutdown is
    run, and then the process is asked to exit, by an exception."""

    async def serve(self) -> None:
        try:
            await super().serve()
        except beacon.main.ServerSetupError:
            sys.exit(3)


@pytest.mark.parametrize("repeat", ["1", "0"])
def test_a_server_uvicorn_ends_with_an_exit_keeps_the_stores_when_it_left_something(
    monkeypatch: pytest.MonkeyPatch, repeat: str
) -> None:
    # uvicorn raises SystemExit after a startup that failed. Raised through the
    # loop, it would pass the look at how the server stopped, and the lock would
    # be given up under a worker that is still there.
    async def not_whole(ingest: object) -> bool:
        await stop(ingest)  # type: ignore[arg-type]
        return False

    def fails(*args: object) -> None:
        raise beacon.main.ServerSetupError("no")

    stop = beacon.main._stop_ingest
    monkeypatch.setattr(beacon.main, "_stop_ingest", not_whole)
    monkeypatch.setattr(beacon.main, "open_push", fails)
    left: list[bool] = []

    def leave() -> None:
        # Called by the task that served, while its loop still runs.
        left.append(asyncio.get_running_loop().is_running())

    status = serve.run(env() | {serve.REPEAT_VARIABLE: repeat}, Uvicorns, leave=leave)

    assert (status, left) == ((serve.LEFT_RUNNING, [True]) if repeat == "1" else (3, []))
    with pytest.raises(DataLockedError):
        DataLock(data_dir(), shared=True)


def test_a_server_uvicorn_ends_with_an_exit_that_left_nothing_gives_the_stores_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fails(*args: object) -> None:
        raise beacon.main.ServerSetupError("no")

    monkeypatch.setattr(beacon.main, "open_push", fails)
    left: list[int] = []

    assert serve.run(env(), Uvicorns, leave=lambda: left.append(1)) == 3

    assert left == []
    DataLock(data_dir(), shared=False).close()


def test_a_real_uvicorn_that_cannot_have_its_address_is_a_server_that_did_not_start() -> None:
    # The exit uvicorn raises for an address somebody else has is taken as that,
    # not raised through `run`: nothing of the app was left, so the stores are
    # given up, and the status is the failure to start.
    with closing(socket.socket()) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]

        status = serve.run(
            {ENV_KEY: os.environ[ENV_KEY]},
            lambda app: serve.uvicorn_server(app, "127.0.0.1", port),
        )

    assert status == 3
    DataLock(data_dir(), shared=False).close()


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
