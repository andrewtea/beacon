"""`python -m beacon.serve`: the server as a deployment runs it, and the invented
story begun again when it is over."""

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beacon import serve
from beacon.evidence import EvidenceStore, NewEvidence
from beacon.ingest.setup import MOCK_HEAD_START, IngestConfigError
from beacon.main import create_app
from beacon.taps.media import MediaLockedError

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
    root = data_dir()
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_the_story_is_played_for_as_long_as_the_pollers_play_it() -> None:
    # A story begins twenty minutes before its store does, and is over 105 minutes
    # after it began: a server that tells it again does so every 85 minutes.
    start = datetime(2026, 3, 3, 16, 0, tzinfo=UTC)
    assert serve.over_at(start) == start + timedelta(minutes=105)
    assert timedelta(minutes=20) == MOCK_HEAD_START
    assert "about every 85 minutes" in serve.REPEATS


def test_nothing_is_deleted_before_the_story_is_over_or_where_there_is_no_mock_store() -> None:
    # No store: nothing to begin again, and no directory is made for asking.
    assert serve.begin_again(env(), datetime.now(UTC)) is False
    assert files() == {}
    over = served()
    before = files()

    assert serve.begin_again(env(), over - timedelta(seconds=1)) is False

    assert files() == before


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

    assert serve.begin_again(env(), over) is True

    after = files()
    # The three files and whatever SQLite kept beside them, and nothing else.
    gone = set(before) - set(after)
    assert set(mock) <= gone
    assert all(name.removesuffix("-wal").removesuffix("-shm") in mock for name in gone)
    assert not any(name.startswith(tuple(mock)) for name in after)
    assert {name: held for name, held in before.items() if name not in gone} == after
    # Asked again, there is nothing to begin again.
    assert serve.begin_again(env(), over) is False


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
    before = set(files())

    with TestClient(create_app()), pytest.raises(MediaLockedError):
        serve.begin_again(env(), over)

    assert {f"{d}/{n}" for d, n in serve.MOCK_FILES} <= before & set(files())


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
        serve.begin_again(env(), over)
    # The store that says the story is over is still there.
    assert (data_dir() / "evidence" / "mock.sqlite3").is_file()
    assert not (data_dir() / "investigate" / "mock.sqlite3").exists()

    monkeypatch.setattr(serve, "_delete", delete)
    assert serve.begin_again(env(), over) is True
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

    def make_server(app: FastAPI) -> Stub:
        tellings.append(app)
        return Stub(app, first if len(tellings) == 1 else second)

    status = serve.run(env(), make_server, clock=lambda: datetime.now(UTC) + late[0])

    # Two tellings, each on an app of its own. The first held the tap, and the
    # second began on a store that does not: the tap went with the story.
    assert status == 0
    assert len(tellings) == 2
    assert tellings[0] is not tellings[1]
    assert seen == [(serve.REPEATS, 1), (serve.REPEATS, 0)]
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
