"""Building the pollers from the environment, and the mock scenario played through them."""

import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from beacon.evidence import Evidence, EvidenceStore, NewEvidence
from beacon.ingest import (
    Clock,
    IngestConfigError,
    Runner,
    build,
    live_sources,
    open_store,
    source_pollers,
)
from beacon.ingest.http import http_client
from beacon.scenario import FIRE, INVENTED, Scenario, scenario_transport

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
# A mock store begins its story twenty minutes before it is first opened.
START = NOON - timedelta(minutes=20)
ALERT = "urn:oid:2.49.0.1.840.0.beacon-mock-scenario."


def story(minute: float) -> datetime:
    return START + timedelta(minutes=minute)


class Time:
    def __init__(self) -> None:
        self.moment = NOON

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def time() -> Time:
    return Time()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"BEACON_DATA_DIR": str(tmp_path)}


def told(rows: list[Evidence]) -> list[tuple[str, datetime]]:
    return [(row.ref, row.occurred_at) for row in rows]


def changes(store: EvidenceStore) -> list[tuple[str, str, datetime]]:
    return [
        (row.data["change"], row.data["about"], row.occurred_at)
        for row in store.search(kinds=["alert", "traffic"])
        if "change" in row.data
    ]


def test_with_no_live_source_the_pollers_play_the_whole_invented_fire(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(env, Clock(time))
    assert ingest.scenario == Scenario(start=START)
    assert ingest.store.mode == "mock"

    # Two hours on, the story is over.
    time.moment = NOON + timedelta(minutes=100)
    ingest.catch_up()
    store = ingest.store

    # NWS is asked every two minutes, so a change is seen at the next even minute.
    update = ALERT + "shelter.001.2"
    assert told(store.search(kinds=["alert"])) == [
        (ALERT + "wind.001.1", story(-180)),
        (ALERT + "shelter.001.1", story(16)),
        # The update replaces the first warning, which is therefore not taken as gone.
        (update, story(40)),
        # Dropped at minute 70, fifty minutes before it said it would end, and
        # missing from the answers of minutes 70 and 72.
        (f"{update}#not-listed@{story(72).isoformat()}", story(72)),
    ]
    widened = "511.org/mock-1002@" + story(20).isoformat()
    assert told(store.search(kinds=["traffic"])) == [
        ("511.org/mock-1001@" + story(-600).isoformat(), story(-600)),
        ("511.org/mock-1002@" + story(9).isoformat(), story(9)),
        (widened, story(20)),
        # No longer listed from minute 80.
        (f"{widened}#not-listed@{story(82).isoformat()}", story(82)),
    ]
    assert changes(store) == [
        ("not_listed", update, story(72)),
        ("not_listed", widened, story(82)),
    ]
    shelter = store.find("nws", ALERT + "shelter.001.1")
    assert shelter.data["event"] == "Shelter In Place Warning"
    assert shelter.point.distance_m(FIRE) < 100
    closures = store.search(kinds=["traffic"], near=FIRE, radius_m=200)
    assert [row.data.get("closure") for row in closures] == [True, True, None]
    assert [len(row.data.get("roads", [])) for row in closures] == [1, 2, 0]
    # What a source said is official. That it stopped saying it is what Beacon saw.
    assert [row.confidence for row in closures] == ["official", "official", "medium"]

    aircraft = store.search(kinds=["aircraft"], limit=1000)
    by_address: dict[str, list[Evidence]] = {}
    for row in aircraft:
        by_address.setdefault(row.data["icao24"], []).append(row)
    # The airliner at cruise is never stored. OpenSky is asked every thirty seconds:
    # the helicopter is there from minute 5 to 50, the light aircraft from 25 to 29.
    assert {address: len(rows) for address, rows in by_address.items()} == {
        "beac01": 90,
        "beac02": 8,
    }
    helicopter = by_address["beac01"]
    assert (helicopter[0].occurred_at, helicopter[-1].occurred_at) == (story(5), story(49.5))
    assert {row.data["category"] for row in helicopter} == {"rotorcraft"}
    circling = [row for row in helicopter if story(7) <= row.occurred_at < story(48)]
    assert len(circling) == 82
    for row in circling:
        assert row.point.distance_m(FIRE) == pytest.approx(450, abs=3)
    # It goes all the way round: it is seen on every side of the fire.
    assert {(row.point.lat > FIRE.lat, row.point.lon > FIRE.lon) for row in circling} == {
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    }
    assert {row.confidence for row in by_address["beac02"]} == {"medium"}

    everything = store.search(limit=5000)
    assert len(everything) == 4 + 4 + 98
    assert {(row.origin, row.stand_in, row.model) for row in everything} == {("mock", True, None)}
    # Every text a person could read says it was made up.
    for row in everything:
        if row.kind == "aircraft":
            assert row.data["callsign"].startswith("MOCK")
        elif "change" not in row.data:
            assert INVENTED in (row.data.get("description") or row.data["headline"])


def test_a_mock_store_first_opened_shows_a_fire_already_under_way(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(env, Clock(time))

    ingest.catch_up()

    # NOON is the story's twentieth minute: the warning is four minutes old, and
    # 511 has this minute widened the closure.
    now = ingest.store.search(kinds=["alert", "traffic"], start=NOON, end=NOON)
    assert [row.ref for row in now] == [
        ALERT + "wind.001.1",
        ALERT + "shelter.001.1",
        "511.org/mock-1002@" + story(20).isoformat(),
    ]
    # The helicopter has been there since minute 5, seen every thirty seconds.
    assert ingest.store.count_by_source() == {"nws": 2, "511": 3, "opensky": 31}
    assert ingest.store.state("scenario") == {
        "start": START.isoformat(),
        "played_to": NOON.isoformat(),
    }
    # The clock is let go when the story has been played up to now.
    assert ingest.clock.now() == NOON


def test_a_restart_continues_the_story_it_was_telling(env: dict[str, str], time: Time) -> None:
    first = build(env, Clock(time))
    first.catch_up()

    # The process ends. Ten minutes later another one opens the same store.
    time.moment = NOON + timedelta(minutes=10)
    second = build(env, Clock(time))
    assert second.scenario == first.scenario
    second.catch_up()
    again = build(env, Clock(time))
    again.catch_up()
    time.moment = NOON + timedelta(minutes=200)
    again.catch_up()

    # The same things happen in it as in a story played without a break. A restart
    # polls every source at once, so a change can be seen at another second.
    time.moment = NOON
    unbroken = build({"BEACON_DATA_DIR": env["BEACON_DATA_DIR"] + "/other"}, Clock(time))
    time.moment = NOON + timedelta(minutes=200)
    unbroken.catch_up()
    assert [change[:2] for change in changes(again.store)] == [
        change[:2] for change in changes(unbroken.store)
    ]
    assert len(again.store.search(kinds=["alert", "traffic"])) == 8
    # No position of the helicopter is stored twice, and none of its 45 minutes is
    # skipped: it is never more than thirty seconds between two positions.
    helicopter = [
        row.occurred_at
        for row in again.store.search(kinds=["aircraft"], limit=1000)
        if row.data["icao24"] == "beac01"
    ]
    assert len(helicopter) == len(set(helicopter))
    assert helicopter[0] == story(5)
    assert story(49.5) <= helicopter[-1] < story(50)
    gaps = {later - earlier for earlier, later in itertools.pairwise(helicopter)}
    assert max(gaps) == timedelta(seconds=30)


def test_a_step_cut_short_by_a_crash_is_played_again_without_counting_twice(
    env: dict[str, str], time: Time
) -> None:
    first = build(env, Clock(time))
    time.moment = story(70) - timedelta(seconds=10)
    first.catch_up()
    # The step of minute 70 polls NWS, which no longer lists the warning: its first
    # answer without it. The process dies before the step is marked as played.
    first.clock.hold(story(70))
    first.runner.step()

    time.moment = story(100)
    second = build(env, Clock(time))
    second.catch_up()

    # Minute 70 is asked about again, and is still one answer without the warning,
    # not two: the warning is taken as gone at minute 72, as in an unbroken story.
    assert changes(second.store) == [
        ("not_listed", ALERT + "shelter.001.2", story(72)),
        ("not_listed", "511.org/mock-1002@" + story(20).isoformat(), story(82)),
    ]


def test_what_others_write_to_the_store_does_not_move_the_story(
    env: dict[str, str], time: Time
) -> None:
    first = build(env, Clock(time))
    first.catch_up()
    # A tap is stored, by a route, well after the story was last played.
    tap = NewEvidence(
        kind="tap",
        source="app",
        ref="tap-1",
        occurred_at=NOON + timedelta(minutes=95),
        confidence="low",
        origin="live",
    )
    first.store.add([tap], now=NOON + timedelta(minutes=95))

    time.moment = NOON + timedelta(minutes=100)
    second = build(env, Clock(time))
    second.catch_up()

    # The eighty minutes in between are played, not skipped.
    assert len(changes(second.store)) == 2
    counts = second.store.count_by_source()
    assert (counts["nws"], counts["511"], counts["app"]) == (4, 4, 1)


def test_a_store_left_for_days_is_played_only_to_the_end_of_its_story(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(env, Clock(time))
    time.moment = NOON + timedelta(days=30)

    ingest.catch_up()

    # The story runs 95 minutes, and the pollers are given ten more to notice.
    assert ingest.store.state("scenario")["played_to"] == story(105).isoformat()
    assert len(changes(ingest.store)) == 2


@pytest.mark.parametrize(
    ("setting", "sources"),
    [
        ("", ()),
        (" , ", ()),
        ("nws", ("nws",)),
        (" opensky ,nws,, 511", ("nws", "opensky", "511")),
    ],
)
def test_the_live_sources_are_the_ones_the_setting_names(
    setting: str, sources: tuple[str, ...]
) -> None:
    assert live_sources({"BEACON_LIVE_SOURCES": setting}) == sources
    assert live_sources({}) == ()


@pytest.mark.parametrize("setting", [None, "", "  ", " , ", ",,"])
def test_a_setting_that_names_no_source_is_mock_mode_through_and_through(
    env: dict[str, str], time: Time, setting: str | None
) -> None:
    if setting is not None:
        env["BEACON_LIVE_SOURCES"] = setting

    ingest = build(env, Clock(time))
    ingest.catch_up()

    # One reading of the setting picks the store and the pollers alike.
    assert ingest.scenario is not None
    assert (ingest.store.mode, ingest.store.path.name) == ("mock", "mock.sqlite3")
    assert open_store(env).path == ingest.store.path
    assert ingest.store.count_by_source() == {"nws": 2, "511": 3, "opensky": 31}


def test_a_name_that_is_not_a_source_is_refused(env: dict[str, str]) -> None:
    for make in (build, open_store):
        with pytest.raises(IngestConfigError, match="names openmhz, radio, which is not a source"):
            make(env | {"BEACON_LIVE_SOURCES": "nws,radio,openmhz"})


OPENSKY = {"BEACON_LIVE_SOURCES": "opensky", "BEACON_OPENSKY_PERMITTED": "1"}


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"BEACON_LIVE_SOURCES": "511"}, "names 511, which needs BEACON_511_API_KEY"),
        # Credentials are not permission: OpenSky's terms ask for a written license.
        (
            {
                "BEACON_LIVE_SOURCES": "nws,opensky",
                "OPENSKY_CLIENT_ID": "beacon-client",
                "OPENSKY_CLIENT_SECRET": "s3cret",
            },
            "require a written license",
        ),
        (
            {"BEACON_LIVE_SOURCES": "opensky", "BEACON_OPENSKY_PERMITTED": "yes"},
            "set BEACON_OPENSKY_PERMITTED=1 once the license is given",
        ),
        (
            OPENSKY | {"OPENSKY_CLIENT_ID": "beacon-client"},
            "OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET are set together",
        ),
        (
            OPENSKY | {"OPENSKY_CLIENT_SECRET": "s3cret"},
            "OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET are set together",
        ),
    ],
)
def test_a_live_source_without_what_it_needs_is_refused_and_no_store_is_begun(
    env: dict[str, str], settings: dict[str, str], message: str
) -> None:
    with pytest.raises(IngestConfigError, match=message) as refused:
        build(env | settings)

    assert "s3cret" not in str(refused.value)
    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence").exists()


def test_naming_a_source_builds_its_poller_on_the_live_store_and_asks_nothing_yet(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(
        env
        | OPENSKY
        | {"BEACON_LIVE_SOURCES": "nws,opensky,511", "BEACON_511_API_KEY": "key-from-the-env"},
        Clock(time),
    )

    # There is no story to play, and building opens no connection.
    ingest.catch_up()
    assert ingest.scenario is None
    assert (ingest.store.mode, ingest.store.path.name) == ("live", "live.sqlite3")
    assert [(status.name, status.ok) for status in ingest.runner.status()] == [
        ("nws", None),
        ("opensky", None),
        ("511", None),
    ]
    assert ingest.store.count_by_source() == {}
    # The mock store is another file, and this did not begin it.
    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence" / "mock.sqlite3").exists()
    ingest.close()

    only = build(env | {"BEACON_LIVE_SOURCES": "nws"}, Clock(time))
    # A source that is not named is off. It is not mocked beside the live ones.
    assert [status.name for status in only.runner.status()] == ["nws"]


def test_each_source_is_asked_with_the_settings_the_environment_holds(
    tmp_path: Path, time: Time
) -> None:
    requests: list[httpx2.Request] = []
    # The scenario answers for the sources, in their formats, and refuses a request
    # the real source would refuse. Its answers are a stand-in's, so the pollers
    # are built to say so and write to a mock store.
    sources = scenario_transport(Scenario(start=START), time)

    def network(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.host == "auth.opensky-network.org":
            return httpx2.Response(200, json={"access_token": "token-1", "expires_in": 1800})
        return sources.handle_request(request)

    settings = OPENSKY | {
        "BEACON_511_API_KEY": "key-from-the-env",
        "OPENSKY_CLIENT_ID": "beacon-client",
        "OPENSKY_CLIENT_SECRET": "s3cret",
    }
    store = EvidenceStore(tmp_path / "disposable.sqlite3", "mock")
    pollers = source_pollers(
        ("nws", "opensky", "511"),
        settings,
        http_client(httpx2.MockTransport(network)),
        origin="mock",
    )
    runner = Runner(pollers, store, Clock(time))

    results = runner.step()

    assert {name: type(result).__name__ for name, result in results.items()} == {
        "nws": "PollReport",
        "opensky": "PollReport",
        "511": "PollReport",
    }
    by_host = {request.url.host: request for request in requests}
    assert by_host["api.511.org"].url.params["api_key"] == "key-from-the-env"
    assert by_host["opensky-network.org"].headers["authorization"] == "Bearer token-1"
    assert b"client_secret=s3cret" in by_host["auth.opensky-network.org"].content
    assert store.count_by_source() == {"nws": 2, "opensky": 1, "511": 2}
    assert {row.origin for row in store.search()} == {"mock"}
    # With an account OpenSky is asked every thirty seconds.
    time.moment = NOON + timedelta(seconds=30)
    assert list(runner.step()) == ["opensky"]


def test_the_mock_and_live_stores_are_kept_apart_in_one_data_directory(
    env: dict[str, str], time: Time
) -> None:
    mock = build(env, Clock(time))
    mock.catch_up()
    live_env = env | {"BEACON_LIVE_SOURCES": "nws"}
    live = open_store(live_env)

    assert (live.mode, live.path.name) == ("live", "live.sqlite3")
    assert live.count_by_source() == {}
    assert open_store(env).count_by_source() == {"nws": 2, "511": 3, "opensky": 31}
    assert (open_store(env).mode, open_store(env).path.name) == ("mock", "mock.sqlite3")
