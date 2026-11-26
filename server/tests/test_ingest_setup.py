"""Building the pollers from the environment, and the mock scenario played through them."""

import itertools
import json
import logging
import resource
import sqlite3
import threading
import time as system_time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import EmberClient, EmberConfigError, SpendScope
from beacon.evidence import Evidence, EvidenceStore, EvidenceStoreError, NewEvidence
from beacon.ingest import (
    Clock,
    Ingest,
    IngestConfigError,
    PollerStatus,
    PollReport,
    Runner,
    SourceError,
    build,
    live_sources,
    open_store,
    source_pollers,
)
from beacon.ingest.http import SourceHttp, fetch
from beacon.ingest.listing import listed_again_row, not_listed_row
from beacon.ingest.nws import NwsAlerts
from beacon.ingest.opensky import OpenSkyAircraft
from beacon.ingest.traffic511 import Traffic511
from beacon.radio import NOTHING_KNOWN, Ffmpeg, Known
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
    assert len(everything) == 4 + 4 + 98 + 19
    # Everything is the invented scenario's, and the radio also a stand-in Ember's.
    assert {(row.kind == "radio", row.origin, row.stand_in, row.model) for row in everything} == {
        (False, "mock", True, None),
        (True, "mock", True, "fake"),
    }
    # Every text a person could read says it was made up. A radio row has no text.
    for row in everything:
        if row.kind == "aircraft":
            assert row.data["callsign"].startswith("MOCK")
        elif row.kind != "radio" and "change" not in row.data:
            assert INVENTED in (row.data.get("description") or row.data["headline"])


def test_the_radio_of_the_mock_scenario_tells_the_fire_through_a_stand_in_for_ember(
    env: dict[str, str], time: Time, tmp_path: Path
) -> None:
    ingest = build(env, Clock(time))
    time.moment = NOON + timedelta(minutes=100)
    ingest.catch_up()

    calls = ingest.store.search(kinds=["radio"])
    # Twenty calls were made. The click of a microphone was never heard.
    assert [row.ref for row in calls] == [
        f"mock-call-{number:04d}" for number in range(20) if number != 4
    ]
    assert {(row.source, row.origin, row.model, row.point) for row in calls} == {
        ("openmhz", "mock", "fake", None)
    }
    # A call is stored when the poll after it hears it, at the time it was made.
    assert [calls[0].occurred_at, calls[-1].occurred_at] == [story(0), story(90)]
    fire = [row for row in calls if row.data["event"] == "fire"]
    assert [
        (row.data["status"], row.data["alarm"]) for row in fire if row.data["alarm"] is not None
    ] == [("working", 2), ("working", 3), ("under_control", 3)]
    assert [row.data["status"] for row in fire][:4] == [
        "dispatched",
        "en_route",
        "on_scene",
        "working",
    ]
    assert fire[-1].data["status"] == "cleared"
    at_the_fire = [row for row in calls if row.place == "400 block of Hayes St"]
    assert len(at_the_fire) == 10
    # The call whose form had a house number where the block belongs.
    assert calls[2].data["block"] == 400
    assert "evacuation" in calls[7].data["details"]

    # The two medical calls, one on the fire channel and one on the EMS channel
    # that the stand-in took for something else: crews and a block, and no more.
    medical = [row for row in calls if row.data["event"] == "medical response"]
    assert [
        (row.place, row.data["units"], row.data["status"], row.data["details"]) for row in medical
    ] == [
        ("400 block of Fell St", ["M52", "E21"], "unknown", []),
        ("5200 block of Geary Blvd", ["M78"], "unknown", []),
    ]
    assert [row.data["talkgroup"] for row in medical] == [9101, 9103]
    (unheard,) = [row for row in calls if row.data["clarity"] == "unintelligible"]
    assert (unheard.confidence, unheard.data["units"], unheard.place) == ("low", [], None)
    # Something that is not the fire: an alarm across town, sounded and cancelled.
    assert [row.data["status"] for row in calls if row.data["event"] == "alarm"] == [
        "dispatched",
        "cancelled",
    ]

    # The stand-in spent nothing, and each call was "heard" once.
    with sqlite3.connect(tmp_path / "ember" / "ledger.sqlite3") as db:
        heard = db.execute(
            "SELECT source, task, cost_nanousd, COUNT(*) FROM ember_calls GROUP BY 1, 2, 3"
        ).fetchall()
    assert heard == [("fake", "extract_transmission", 0, 19)]
    # No audio is kept: the store, the ledger, and nothing else.
    assert {path.suffix.split("-")[0] for path in tmp_path.rglob("*") if path.is_file()} == {
        ".sqlite3"
    }


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
    assert ingest.store.count_by_source() == {"nws": 2, "511": 3, "opensky": 31, "openmhz": 10}
    assert ingest.store.state("scenario") == {
        "start": START.isoformat(),
        "played_to": NOON.isoformat(),
        # How each poller's last poll went, for a process that goes on from here.
        "polls": {
            name: {"polled_at": NOON.isoformat(), "ok": True, "partial": False}
            for name in ("nws", "opensky", "511", "openmhz")
        },
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


def test_a_step_in_which_a_poll_failed_is_played_again_and_nothing_is_missed(
    env: dict[str, str], time: Time, monkeypatch: pytest.MonkeyPatch
) -> None:
    polled: list[tuple[str, datetime]] = []
    for poller in (NwsAlerts, OpenSkyAircraft, Traffic511):

        def poll(
            self: Any, store: EvidenceStore, now: datetime, poll: Any = poller.poll
        ) -> PollReport:
            polled.append((self.name, now))
            # At minute 6 the helicopter has just begun to circle. OpenSky cannot be
            # read, twice, in the one step in which all three sources are asked.
            if (self.name, now) == ("opensky", story(6)) and polled.count(polled[-1]) < 3:
                raise SourceError("opensky", "could not be reached")
            return poll(self, store, now)

        monkeypatch.setattr(poller, "poll", poll)
    ingest = build(env, Clock(time))

    # The playing ends at the step that failed, which is not saved as played.
    assert ingest.catch_up() is False
    last_played = (story(6) - timedelta(seconds=10)).isoformat()
    assert ingest.store.state("scenario")["played_to"] == last_played
    assert ingest.runner.status()[1] == PollerStatus("opensky", story(6), False)
    assert ingest.clock.now() == NOON
    # Played again in the same process, it fails again and is still not passed over.
    assert ingest.catch_up() is False
    assert ingest.store.state("scenario")["played_to"] == last_played

    # The third time the source answers, and the story goes on to now.
    assert ingest.catch_up() is True
    assert ingest.store.state("scenario")["played_to"] == NOON.isoformat()
    # Only the poller that failed was asked again for that moment.
    assert [name for name, moment in polled if moment == story(6)] == [
        "nws",
        "opensky",
        "511",
        "opensky",
        "opensky",
    ]
    helicopter = [row.occurred_at for row in ingest.store.search(kinds=["aircraft"], limit=1000)]
    assert story(6) in helicopter
    assert {later - earlier for earlier, later in itertools.pairwise(helicopter)} == {
        timedelta(seconds=30)
    }


def test_a_step_that_failed_is_played_again_by_the_process_that_comes_after(
    env: dict[str, str], time: Time, monkeypatch: pytest.MonkeyPatch
) -> None:
    poll = OpenSkyAircraft.poll

    def fail_at_minute_five(self: Any, store: EvidenceStore, now: datetime) -> PollReport:
        if now == story(5):
            raise SourceError("opensky", "could not be reached")
        return poll(self, store, now)

    with monkeypatch.context() as patched:
        patched.setattr(OpenSkyAircraft, "poll", fail_at_minute_five)
        assert build(env, Clock(time)).catch_up() is False

    # The process ends there. The next one is asked about minute 5 again.
    second = build(env, Clock(time))
    assert second.catch_up() is True
    helicopter = [row.occurred_at for row in second.store.search(kinds=["aircraft"], limit=1000)]
    assert helicopter[0] == story(5)
    assert len(helicopter) == 31


def test_a_restart_after_the_story_has_ended_still_says_how_the_last_polls_went(
    env: dict[str, str], time: Time
) -> None:
    first = build(env, Clock(time))
    time.moment = NOON + timedelta(days=1)
    first.catch_up()
    finished = first.runner.status()

    second = build(env, Clock(time))

    # Nothing is left to poll, so what the earlier process saw is all there is to say.
    assert second.catch_up() is True
    assert second.runner.status() == finished
    assert finished == [
        PollerStatus("nws", story(104), True),
        PollerStatus("opensky", story(105), True),
        PollerStatus("511", story(104), True),
        PollerStatus("openmhz", story(105), True),
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
    assert ingest.store.count_by_source() == {"nws": 2, "511": 3, "opensky": 31, "openmhz": 10}


def test_a_name_that_is_not_a_source_is_refused(env: dict[str, str]) -> None:
    for make in (build, open_store):
        with pytest.raises(IngestConfigError, match="names cnn, radio, which is not a source"):
            make(env | {"BEACON_LIVE_SOURCES": "nws,radio,cnn"})


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
    # The pollers are given the network, which is what makes their rows `live`.
    assert ingest.http.origin == "live"
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
        ("nws", "opensky", "511"), settings, SourceHttp(httpx2.MockTransport(network))
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


@pytest.fixture(autouse=True)
def an_ffmpeg_that_can_be_confined(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a poller for real radio tries ffmpeg out, confined, and reads where
    the host sends core dumps. These tests are about the settings, on any machine;
    `test_radio_audio` is about ffmpeg and the host."""
    monkeypatch.setattr(Ffmpeg, "problem", lambda self: None)
    monkeypatch.setattr("beacon.ingest.setup.core_dump_problem", lambda: None)


RADIO = {
    "BEACON_LIVE_SOURCES": "openmhz",
    "BEACON_OPENMHZ_PERMITTED": "1",
    "BEACON_OPENMHZ_TALKGROUPS": "1201, 1203,1301",
    "BEACON_OPENMHZ_EMS_TALKGROUPS": "1301",
    "BEACON_OPENMHZ_BUDGET_USD": "0.25",
    "REPLAY": "0",
    "MODEL_API_KEY": "key-from-the-env",
}


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        # Being able to ask is not permission to.
        (RADIO | {"BEACON_OPENMHZ_PERMITTED": ""}, "needs its permission"),
        (RADIO | {"BEACON_OPENMHZ_PERMITTED": "yes"}, "set BEACON_OPENMHZ_PERMITTED=1 once"),
        (RADIO | {"BEACON_OPENMHZ_TALKGROUPS": ""}, "needs BEACON_OPENMHZ_TALKGROUPS"),
        (RADIO | {"BEACON_OPENMHZ_TALKGROUPS": "fire,ems"}, "is a list of talkgroup numbers"),
        (RADIO | {"BEACON_OPENMHZ_TALKGROUPS": "-1201"}, "is a list of talkgroup numbers"),
        (
            RADIO | {"BEACON_OPENMHZ_EMS_TALKGROUPS": "1301,1400"},
            "names a talkgroup that BEACON_OPENMHZ_TALKGROUPS does not",
        ),
        (
            RADIO | {"BEACON_OPENMHZ_MEDIA_HOSTS": "https://s3.example.org/calls"},
            "is a list of host names",
        ),
        # Every call is paid for, so the most it may cost has no default.
        (RADIO | {"BEACON_OPENMHZ_BUDGET_USD": ""}, "needs BEACON_OPENMHZ_BUDGET_USD"),
        (RADIO | {"BEACON_OPENMHZ_BUDGET_USD": "a little"}, "needs BEACON_OPENMHZ_BUDGET_USD"),
        (RADIO | {"BEACON_OPENMHZ_BUDGET_USD": "250"}, "needs BEACON_OPENMHZ_BUDGET_USD"),
        # A call cannot be replayed, so a client that only replays cannot hear one.
        (RADIO | {"REPLAY": "1"}, "heard by a live Ember and cannot be replayed"),
    ],
)
def test_radio_is_not_polled_for_real_without_permission_a_budget_and_a_live_ember(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], message: str
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")

    with pytest.raises(IngestConfigError, match=message) as refused:
        build(env | settings)

    assert "key-from-the-env" not in str(refused.value)
    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence").exists()


def test_radio_is_not_polled_for_real_until_the_spend_so_far_is_known(
    env: dict[str, str],
) -> None:
    # The Ember client's own rule: a client that can spend needs EMBER_SPENT_BEFORE_USD.
    with pytest.raises(EmberConfigError, match="EMBER_SPENT_BEFORE_USD"):
        build(env | RADIO)

    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence").exists()


def test_radio_is_not_polled_for_real_where_its_audio_cannot_be_converted(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    monkeypatch.setattr(
        Ffmpeg, "problem", lambda self: "ffmpeg is run confined by bubblewrap, which is missing."
    )

    with pytest.raises(
        IngestConfigError,
        match=r"^BEACON_LIVE_SOURCES names openmhz\. ffmpeg is run confined by bubblewrap",
    ):
        build(env | RADIO)

    # Refused before a client that can spend was built.
    assert not (Path(env["BEACON_DATA_DIR"]) / "ember").exists()


def test_radio_is_not_polled_for_real_on_a_host_that_pipes_core_dumps_to_a_collector(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    monkeypatch.setattr(
        "beacon.ingest.setup.core_dump_problem",
        lambda: "This host pipes core dumps to a collector.",
    )
    before = resource.getrlimit(resource.RLIMIT_CORE)

    with pytest.raises(
        IngestConfigError,
        match=r"^BEACON_LIVE_SOURCES names openmhz\. This host pipes core dumps",
    ):
        build(env | RADIO)

    # Refused before a client that can spend was built, and nothing was changed.
    assert not (Path(env["BEACON_DATA_DIR"]) / "ember").exists()
    assert resource.getrlimit(resource.RLIMIT_CORE) == before


def test_the_decoder_of_real_radio_is_kept_from_beacons_data_directory(
    env: dict[str, str], time: Time, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")

    ingest = build(env | RADIO, Clock(time))

    (radio,) = ingest.paid
    assert radio._transcoder._hidden == (Path(env["BEACON_DATA_DIR"]),)
    ingest.close()


def test_naming_radio_builds_its_poller_with_a_live_ember_under_its_budget_and_asks_nothing(
    env: dict[str, str], time: Time, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    built: list[tuple[EmberClient, SpendScope | None]] = []
    from_env = EmberClient.from_env

    def remember(*args: Any, scope: SpendScope | None = None, **options: Any) -> EmberClient:
        built.append((from_env(*args, scope=scope, **options), scope))
        return built[-1][0]

    monkeypatch.setattr(EmberClient, "from_env", remember)

    ingest = build(env | RADIO | {"BEACON_OPENMHZ_MEDIA_HOSTS": "S3.Example.org"}, Clock(time))

    assert (ingest.store.mode, ingest.http.origin, ingest.scenario) == ("live", "live", None)
    assert [(status.name, status.ok) for status in ingest.runner.status()] == [("openmhz", None)]
    # One client, which can spend, and every call it makes counts against the
    # budget the operator set for radio over all runs: 25 cents.
    ((ember, scope),) = built
    assert ember.spends
    assert scope == SpendScope("ingest:openmhz", 250_000_000)
    assert ember.spent_usd() == 0
    ingest.close()


def test_real_radio_names_only_the_streets_and_units_the_operator_listed(
    env: dict[str, str],
    time: Time,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    streets, units = tmp_path / "streets.txt", tmp_path / "units.txt"
    streets.write_text("# The city's street names\nHAYES ST\nGough St\n\nAVENUE OF THE PALMS\n")
    units.write_text("E36\nTruck 6\n")
    lists = {"BEACON_RADIO_STREETS_FILE": str(streets), "BEACON_RADIO_UNITS_FILE": str(units)}

    with caplog.at_level(logging.WARNING):
        ingest = build(env | RADIO | lists, Clock(time))

    (radio,) = ingest.paid
    assert radio._known == Known.of(streets=["Hayes St", "Gough St"], units=["E36", "T6"])
    assert "1 names in the lists of streets and units are not names Beacon keeps" in caplog.text
    # The process holds real radio audio in memory from here on, and dumps no core.
    assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
    ingest.close()


def test_real_radio_with_no_lists_names_no_street_and_no_unit_and_says_so(
    env: dict[str, str],
    time: Time,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")

    with caplog.at_level(logging.WARNING):
        ingest = build(env | RADIO, Clock(time))

    (radio,) = ingest.paid
    assert radio._known == NOTHING_KNOWN
    assert "No list of streets (BEACON_RADIO_STREETS_FILE)" in caplog.text
    assert "No list of units (BEACON_RADIO_UNITS_FILE)" in caplog.text
    ingest.close()


def test_a_list_of_names_that_cannot_be_read_is_refused(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    missing = {"BEACON_RADIO_UNITS_FILE": str(tmp_path / "no-such-file.txt")}

    with pytest.raises(
        IngestConfigError, match="BEACON_RADIO_UNITS_FILE names a file that cannot be read"
    ):
        build(env | RADIO | missing)

    # Refused before a client that can spend was built, or a store was opened.
    assert not (Path(env["BEACON_DATA_DIR"]) / "ember").exists()
    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence").exists()


def test_the_mock_scenarios_radio_is_closed_and_waited_for_with_the_pollers(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(env, Clock(time))

    assert [work.name for work in ingest.paid] == ["openmhz"]


def test_what_a_stand_in_answered_cannot_be_stored_as_a_real_sources(
    tmp_path: Path, time: Time
) -> None:
    # The pollers a live deployment would build, from the same settings, but handed
    # the invented scenario where the network would be.
    stand_in = SourceHttp(scenario_transport(Scenario(start=START), time))
    pollers = source_pollers(
        ("nws", "opensky", "511"), OPENSKY | {"BEACON_511_API_KEY": "key-from-the-env"}, stand_in
    )
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")

    # Their rows are `mock` ones, because the client says so and nothing else can.
    assert (stand_in.origin, {poller.origin for poller in pollers}) == ("mock", {"mock"})
    # The live store's runner does not take them, and each refuses the store itself.
    with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
        Runner(pollers, live, Clock(time))
    for poller in pollers:
        with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
            poller.poll(live, NOON)
    assert live.count_by_source() == {}
    assert [live.state(poller.name) for poller in pollers] == [{}, {}, {}]


def test_a_stand_ins_silence_is_not_recorded_in_the_live_store_as_an_item_going_missing(
    tmp_path: Path, time: Time
) -> None:
    # The live store holds what the real sources listed: an alert and a closure,
    # each with the state its real poller saved.
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    alert = NewEvidence(
        kind="alert",
        source="nws",
        ref="urn:oid:real.1",
        occurred_at=NOON - timedelta(hours=1),
        until=NOON + timedelta(hours=5),
        confidence="official",
        origin="live",
        data={"event": "Wind Advisory", "message_type": "Alert", "replaces": []},
    )
    closure = NewEvidence(
        kind="traffic",
        source="511",
        ref="511.org/9@2026-11-27T19:00:00+00:00",
        occurred_at=NOON - timedelta(hours=1),
        confidence="official",
        origin="live",
        data={"event_id": "511.org/9", "headline": "Road closed"},
    )
    listed = {
        "nws": {"at": "earlier", "listed": {alert.ref: {"ref": alert.ref, "missed": 0}}},
        "511": {"at": "earlier", "listed": {"511.org/9": {"ref": closure.ref, "missed": 0}}},
    }
    live.add([alert], state=("nws", listed["nws"]), now=NOON)
    live.add([closure], state=("511", listed["511"]), now=NOON)
    before = live.search()

    # A stand-in that lists nothing is polled against that store, over and over.
    silent = SourceHttp(
        httpx2.MockTransport(
            lambda request: httpx2.Response(
                200,
                json={"type": "FeatureCollection", "features": []}
                if request.url.host == "api.weather.gov"
                else {"events": [], "pagination": {"offset": 0}},
            )
        )
    )
    for poller in (NwsAlerts(silent), Traffic511(silent, api_key="token")):
        for count in (2, 4, 6):
            with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
                poller.poll(live, NOON + timedelta(minutes=count))

    # Nothing is stored, and no answer of the stand-in's counts as one without the item.
    assert live.search() == before
    assert (live.state("nws"), live.state("511")) == (listed["nws"], listed["511"])


def test_a_change_row_has_the_origin_of_the_poller_that_saw_the_change(
    tmp_path: Path,
) -> None:
    # A mock store can hold a row from a real source. That a stand-in then stopped
    # listing it is the stand-in's doing, and is recorded as such.
    store = EvidenceStore(tmp_path / "mock.sqlite3", "mock")
    listed = [
        {
            "id": "https://api.weather.gov/alerts/urn:oid:real.1",
            "geometry": None,
            "properties": {
                "id": "urn:oid:real.1",
                "status": "Actual",
                "messageType": "Alert",
                "event": "Wind Advisory",
                "sent": "2026-11-27T11:00:00-08:00",
                "ends": "2026-11-27T22:00:00-08:00",
            },
        }
    ]
    answers = SourceHttp(
        httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json={"type": "FeatureCollection", "features": listed}
            )
        )
    )
    real = NewEvidence(
        kind="alert",
        source="nws",
        ref="urn:oid:real.1",
        occurred_at=NOON - timedelta(hours=1),
        until=NOON + timedelta(hours=10),
        confidence="official",
        origin="live",
        data={"event": "Wind Advisory", "message_type": "Alert", "replaces": []},
    )
    store.add([real], now=NOON)
    poller = NwsAlerts(answers)
    poller.poll(store, NOON)

    listed.clear()
    poller.poll(store, NOON + timedelta(minutes=2))
    poller.poll(store, NOON + timedelta(minutes=4))

    # The stand-in listed the alert once, which is "listed again" for a stored row
    # no poll had listed, and then did not.
    assert [(row.data.get("change"), row.origin) for row in store.search()] == [
        (None, "live"),
        ("listed_again", "mock"),
        ("not_listed", "mock"),
    ]


def test_a_change_about_a_stand_ins_row_is_a_stand_ins_whoever_saw_it(tmp_path: Path) -> None:
    mock = EvidenceStore(tmp_path / "mock.sqlite3", "mock")
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    invented, heard_by_a_fake = mock.add(
        [
            NewEvidence(
                kind="alert",
                source="nws",
                ref="urn:oid:invented.1",
                occurred_at=NOON,
                place="San Francisco",
                confidence="official",
                origin="mock",
                data={"event": "Shelter In Place Warning"},
            ),
            NewEvidence(
                kind="radio",
                source="openmhz",
                ref="call-1",
                occurred_at=NOON,
                confidence="medium",
                origin="live",
                model="fake",
            ),
        ]
    )

    # A poller that asks the real source sees that the item is not listed. The row
    # that says so repeats the invented row's place, so it is a stand-in's too.
    for about in (invented, heard_by_a_fake):
        for build_row in (not_listed_row, listed_again_row):
            change = NewEvidence(**build_row(about, NOON + timedelta(minutes=4), "live"))

            assert (change.origin, change.model) == (about.origin, about.model)
            assert change.stand_in
            with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
                live.add([change])
    assert live.search() == []


def test_in_the_mock_store_a_change_is_a_stand_ins_whichever_poller_saw_the_last_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = [True]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            features = [
                {
                    "id": "https://api.weather.gov/alerts/urn:oid:wind.1",
                    "geometry": None,
                    "properties": {
                        "id": "urn:oid:wind.1",
                        "status": "Actual",
                        "messageType": "Alert",
                        "event": "Wind Advisory",
                        "sent": "2026-11-27T11:00:00-08:00",
                        "ends": "2026-11-27T22:00:00-08:00",
                    },
                }
            ]
            body = json.dumps(
                {"type": "FeatureCollection", "features": features if listed[0] else []}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    # A poller that asks the network, which here is a server on this machine, and
    # one that a stand-in answers with an empty list. They share a mock store.
    monkeypatch.setattr("beacon.ingest.nws.URL", f"http://127.0.0.1:{server.server_port}/alerts")
    over_the_network = NwsAlerts(SourceHttp())
    answered_by_a_stand_in = NwsAlerts(
        SourceHttp(
            httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, json={"type": "FeatureCollection", "features": []}
                )
            )
        )
    )
    store = EvidenceStore(tmp_path / "mock.sqlite3", "mock")

    over_the_network.poll(store, NOON)
    listed[0] = False
    # The first answer without the alert is the stand-in's, and the second the network's.
    answered_by_a_stand_in.poll(store, NOON + timedelta(minutes=2))
    over_the_network.poll(store, NOON + timedelta(minutes=4))
    server.shutdown()
    server.server_close()

    alert, change = store.search()
    assert (alert.origin, over_the_network.origin) == ("live", "live")
    # One of the two answers it rests on was a stand-in's, and the store does not
    # keep which: a change worked out in the mock store is a stand-in's.
    assert (change.data["change"], change.origin, change.stand_in) == ("not_listed", "mock", True)


def test_a_pollers_origin_is_its_clients_and_cannot_be_set() -> None:
    stand_in = SourceHttp(httpx2.MockTransport(lambda request: httpx2.Response(200)))

    for poller in (
        NwsAlerts(stand_in),
        OpenSkyAircraft(stand_in),
        Traffic511(stand_in, api_key="token"),
    ):
        assert poller.origin == "mock"
        with pytest.raises(AttributeError):
            poller.origin = "live"  # type: ignore[misc]
        assert poller.origin == "mock"


class Silent:
    """A loopback server that takes a request and never answers it."""

    def __init__(self) -> None:
        self.asked = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.asked.set()
                system_time.sleep(5)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/"
        threading.Thread(target=self._server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_stopping_calls_off_a_request_that_is_out_and_leaves_no_thread(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = Silent()
    http = SourceHttp()

    class Asking:
        name = "slow"
        origin = "live"
        interval_seconds = 30.0

        def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
            fetch(http, "GET", server.url, source="slow", max_bytes=10)
            row = NewEvidence(
                kind="alert",
                source="slow",
                ref="1",
                occurred_at=now,
                confidence="low",
                origin="live",
            )
            return PollReport(added=len(store.add([row], now=now)))

    store = EvidenceStore(tmp_path / "live.sqlite3", "live")
    clock = Clock()
    ingest = Ingest(store, Runner([Asking()], store, clock), clock, http, None)
    ingest.start()
    assert server.asked.wait(5)
    began = system_time.monotonic()

    with caplog.at_level(logging.DEBUG, logger="beacon"):
        stopped = ingest.stop()

    # The source would have kept the poller waiting for twenty seconds.
    assert stopped is True
    assert system_time.monotonic() - began < 2
    assert "beacon-ingest" not in [thread.name for thread in threading.enumerate()]
    assert store.count_by_source() == {}
    # Being stopped is not a failure of the source's, and is not logged as one.
    assert caplog.text == ""
    server.close()


def test_stopping_calls_off_a_write_that_is_waiting_and_nothing_is_stored_afterwards(
    env: dict[str, str], time: Time
) -> None:
    ingest = build(env, Clock(time))
    # Something else is in the middle of a write to the store, as a route can be.
    other = sqlite3.connect(ingest.store.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    ingest.start()
    system_time.sleep(0.3)
    began = system_time.monotonic()

    stopped = ingest.stop()

    assert stopped is True
    assert system_time.monotonic() - began < 2
    assert "beacon-ingest" not in [thread.name for thread in threading.enumerate()]
    # The other write ends. Nothing of the pollers' follows it into the store: the
    # first step of the story was never stored, and is not marked as played.
    other.execute("COMMIT")
    other.close()
    system_time.sleep(0.2)
    after = open_store(env)
    assert after.count_by_source() == {}
    assert after.state("scenario") == {"start": START.isoformat()}
    with pytest.raises(EvidenceStoreError, match="is closed and takes no more rows"):
        ingest.store.add([], state=("scenario", {}))


def test_the_pollers_are_started_once(env: dict[str, str], time: Time) -> None:
    ingest = build(env, Clock(time))
    ingest.start()

    with pytest.raises(RuntimeError, match="already started"):
        ingest.start()

    assert ingest.stop() is True
    # Stopping what never started, or has stopped, is no error.
    assert ingest.stop() is True
    assert build(env, Clock(time)).stop() is True


def test_the_mock_and_live_stores_are_kept_apart_in_one_data_directory(
    env: dict[str, str], time: Time
) -> None:
    mock = build(env, Clock(time))
    mock.catch_up()
    live_env = env | {"BEACON_LIVE_SOURCES": "nws"}
    live = open_store(live_env)

    assert (live.mode, live.path.name) == ("live", "live.sqlite3")
    assert live.count_by_source() == {}
    assert open_store(env).count_by_source() == {"nws": 2, "511": 3, "opensky": 31, "openmhz": 10}
    assert (open_store(env).mode, open_store(env).path.name) == ("mock", "mock.sqlite3")
