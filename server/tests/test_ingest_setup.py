"""Building the pollers from the environment, and the mock scenario played through them."""

import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from beacon.evidence import Evidence, EvidenceStore
from beacon.ingest import Clock, IngestConfigError, build, live_sources
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
    assert told(store.search(kinds=["alert"])) == [
        (ALERT + "wind.001.1", story(-180)),
        (ALERT + "shelter.001.1", story(16)),
        # The update replaces the first warning, which is therefore not cleared.
        (ALERT + "shelter.001.2", story(40)),
        # Dropped at minute 70, fifty minutes before it said it would end, and
        # missing from the answers of minutes 70 and 72.
        (ALERT + "shelter.001.2#cleared", story(72)),
    ]
    assert told(store.search(kinds=["traffic"])) == [
        ("511.org/mock-1001@" + story(-600).isoformat(), story(-600)),
        ("511.org/mock-1002@" + story(9).isoformat(), story(9)),
        ("511.org/mock-1002@" + story(20).isoformat(), story(20)),
        # No longer listed from minute 80.
        ("511.org/mock-1002@" + story(20).isoformat() + "#cleared", story(82)),
    ]
    shelter = store.find("nws", ALERT + "shelter.001.1")
    assert shelter.data["event"] == "Shelter In Place Warning"
    assert shelter.point.distance_m(FIRE) < 100
    closures = store.search(kinds=["traffic"], near=FIRE, radius_m=200)
    assert [row.data.get("closure") for row in closures] == [True, True, None]
    assert [len(row.data.get("roads", [])) for row in closures] == [1, 2, 0]

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
        text = row.data.get("description") or row.data.get("headline")
        if row.kind != "aircraft" and "clears" not in row.data:
            assert INVENTED in text
        elif row.kind == "aircraft":
            assert row.data["callsign"].startswith("MOCK")


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
    assert ingest.store.last_recorded_at() == NOON


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
    interrupted = again.store.search(kinds=["alert", "traffic"])

    time.moment = NOON
    uninterrupted = build({"BEACON_DATA_DIR": env["BEACON_DATA_DIR"] + "/other"}, Clock(time))
    time.moment = NOON + timedelta(minutes=200)
    uninterrupted.catch_up()

    assert [row.ref for row in interrupted] == [
        row.ref for row in uninterrupted.store.search(kinds=["alert", "traffic"])
    ]
    # No position of the helicopter is stored twice, and none of its 45 minutes is
    # skipped: a restart polls at once, so it is never more than thirty seconds
    # between two positions.
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


def test_a_store_left_for_days_is_caught_up_only_to_the_end_of_its_story(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(env, Clock(time))
    time.moment = NOON + timedelta(days=30)

    ingest.catch_up()

    # The story runs 95 minutes, and the pollers are given ten more to notice.
    assert ingest.store.last_recorded_at() <= story(105)
    assert ingest.store.find("511", f"511.org/mock-1002@{story(20).isoformat()}#cleared")


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


def test_a_name_that_is_not_a_source_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(IngestConfigError, match="names openmhz, radio, which is not a source"):
        build(env | {"BEACON_LIVE_SOURCES": "nws,radio,openmhz"})


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"BEACON_LIVE_SOURCES": "511"}, "names 511, which needs BEACON_511_API_KEY"),
        (
            {"BEACON_LIVE_SOURCES": "opensky", "OPENSKY_CLIENT_ID": "beacon-client"},
            "OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET are set together",
        ),
        (
            {"BEACON_LIVE_SOURCES": "opensky", "OPENSKY_CLIENT_SECRET": "s3cret"},
            "OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET are set together",
        ),
    ],
)
def test_a_live_source_without_the_setting_it_needs_is_refused(
    env: dict[str, str], settings: dict[str, str], message: str
) -> None:
    with pytest.raises(IngestConfigError, match=message) as refused:
        build(env | settings)

    assert "s3cret" not in str(refused.value)


def test_naming_a_source_and_setting_its_key_is_all_it_takes_to_poll_it_for_real(
    env: dict[str, str], time: Time
) -> None:
    requests: list[httpx2.Request] = []
    # The real sources' answers are stood in for by the scenario's, which are in
    # the same formats and refuse a request the real source would refuse.
    sources = scenario_transport(Scenario(start=START), time)

    def network(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.host == "auth.opensky-network.org":
            return httpx2.Response(200, json={"access_token": "token-1", "expires_in": 1800})
        return sources.handle_request(request)

    ingest = build(
        env
        | {
            "BEACON_LIVE_SOURCES": "nws,opensky,511",
            "BEACON_511_API_KEY": "key-from-the-environment",
            "OPENSKY_CLIENT_ID": "beacon-client",
            "OPENSKY_CLIENT_SECRET": "s3cret",
        },
        Clock(time),
        transport=httpx2.MockTransport(network),
    )

    # Building asks nothing, and there is no story to catch up on.
    ingest.catch_up()
    assert requests == []
    assert ingest.scenario is None
    assert (ingest.store.mode, ingest.store.path.name) == ("live", "live.sqlite3")

    results = ingest.runner.step()

    assert {name: type(result).__name__ for name, result in results.items()} == {
        "nws": "PollReport",
        "opensky": "PollReport",
        "511": "PollReport",
    }
    by_host = {request.url.host: request for request in requests}
    assert by_host["api.511.org"].url.params["api_key"] == "key-from-the-environment"
    assert by_host["opensky-network.org"].headers["authorization"] == "Bearer token-1"
    assert b"client_secret=s3cret" in by_host["auth.opensky-network.org"].content
    assert ingest.store.count_by_source() == {"nws": 2, "opensky": 1, "511": 2}
    assert {row.origin for row in ingest.store.search()} == {"live"}
    # With an account OpenSky is asked every thirty seconds.
    time.moment = NOON + timedelta(seconds=30)
    assert list(ingest.runner.step()) == ["opensky"]


def test_only_the_named_sources_run_and_the_rest_are_not_mocked_beside_them(
    env: dict[str, str], time: Time
) -> None:
    sources = scenario_transport(Scenario(start=START), time)
    ingest = build(env | {"BEACON_LIVE_SOURCES": "nws"}, Clock(time), transport=sources)

    assert list(ingest.runner.step()) == ["nws"]

    assert ingest.store.count_by_source() == {"nws": 2}
    # The mock store is another file, and this run did not begin it.
    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence" / "mock.sqlite3").exists()


def test_the_mock_and_live_stores_are_kept_apart_in_one_data_directory(
    env: dict[str, str], time: Time
) -> None:
    mock = build(env, Clock(time))
    mock.catch_up()
    sources = scenario_transport(Scenario(start=START), time)
    live = build(env | {"BEACON_LIVE_SOURCES": "nws"}, Clock(time), transport=sources)
    live.runner.step()

    assert EvidenceStore.from_env(env).count_by_source() == {"nws": 2, "511": 3, "opensky": 31}
    assert EvidenceStore.from_env(env | {"BEACON_LIVE_SOURCES": "nws"}).count_by_source() == {
        "nws": 2
    }
