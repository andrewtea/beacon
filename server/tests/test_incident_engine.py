"""The engine on the mock scenario, and the routes that read it."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beacon.evidence import EvidenceStore
from beacon.incident import (
    BLOCKS_VARIABLE,
    NO_BLOCKS,
    Engine,
    IncidentConfigError,
    open_engine,
)
from beacon.ingest import Clock, build
from beacon.main import create_app
from beacon.scenario import FIRE, INVENTED
from tests.incident_rows import NOON, Rows, at, mock_store

# A mock store begins its story twenty minutes before it is first opened.
START = NOON - timedelta(minutes=20)


class Story:
    """The mock scenario, played to a minute of its story, and the engine on it."""

    def __init__(self, tmp_path: Path) -> None:
        self.moment = NOON
        self.ingest = build({"BEACON_DATA_DIR": str(tmp_path)}, Clock(lambda: self.moment))
        self.engine = open_engine({}, self.ingest.store, clock=lambda: self.moment)

    def play(self, minute: float) -> Engine:
        self.moment = START + timedelta(minutes=minute)
        self.ingest.catch_up()
        return self.engine


@pytest.fixture
def story(tmp_path: Path) -> Story:
    return Story(tmp_path)


def minute_of(moment) -> float:  # type: ignore[no-untyped-def]
    return (moment - START).total_seconds() / 60


def test_the_mock_scenario_is_one_fire_two_medical_responses_and_an_alarm(story: Story) -> None:
    found = story.play(100).incidents()

    assert [(incident.title, incident.status, incident.open) for incident in found] == [
        ("Alarm at the 1900 block of Irving St", "cancelled", False),
        ("Medical response at the 5200 block of Geary Blvd", "unknown", False),
        ("Medical response at the 400 block of Fell St", "unknown", False),
        ("Fire at the 400 block of Hayes St", "cleared", False),
    ]
    assert all(incident.stand_in for incident in found)
    fire = found[-1]
    assert (fire.point, fire.alarm, minute_of(fire.opened_at)) == (FIRE, 3, 0)
    assert fire.units == (
        "B2", "B3", "D2", "E1", "E3", "E5", "E21", "E29", "E36", "RS1", "T1", "T3", "T6",
    )  # fmt: skip
    # Seventeen of the nineteen calls that were heard are the fire's or another
    # incident's; the one nobody could make out is nobody's.
    assert [len([e for e in i.timeline if e.kind in ("radio", "medical")]) for i in found] == [
        2,
        1,
        1,
        14,
    ]
    assert [
        (minute_of(entry.at), entry.kind) for entry in fire.timeline if entry.kind != "radio"
    ] == [
        (7.0, "aircraft"),
        (9.0, "closure"),
        (16.0, "alert"),
        (19.0, "notice"),
        (20.0, "closure"),
        (40.0, "alert"),
        (72.0, "alert"),
        (74.0, "notice"),
        (82.0, "closure"),
        (88.0, "notice"),
    ]
    assert [(item.kind, item.type, item.state) for item in fire.official] == [
        ("alert", "Shelter In Place Warning", "replaced"),
        ("alert", "Shelter In Place Warning", "not_listed"),
        ("notice", "evacuation_order", "lifted"),
        ("notice", "all_clear", "published"),
        ("notice", "press_release", "published"),
    ]
    # The wind advisory is the city's, and the road work on Lincoln Way was there
    # all day: neither is the fire's.
    assert "Wind" not in repr(fire)
    assert "Lincoln" not in repr(fire)
    # The medic sent to Fell St is E21's second call, and is not the fire.
    fell = found[2]
    assert (fell.units, fell.official, fell.signals) == (("E21", "M52"), (), ())
    # No word a model wrote about a notice is in any incident: the press release's
    # stand-in form names a person, and the order's gives an instruction.
    shown = repr(found)
    for word in ("Jane", "432", "Civic Center", "Leave now"):
        assert word not in shown
    # Every text that is shown from the scenario's sources says it was made up.
    for entry in fire.timeline:
        if entry.kind == "closure" and "change" not in entry.details:
            assert INVENTED in entry.text


def test_the_fire_grows_and_its_signals_with_it(story: Story) -> None:
    def signals(minute: float) -> list[str]:
        (fire,) = [i for i in story.play(minute).incidents() if i.category == "fire"]
        return [signal.kind for signal in fire.signals]

    assert signals(5) == []
    # A second alarm, a closure, and a helicopter that has been round a few times.
    assert signals(12) == ["alarm", "many_units", "closure", "aircraft_circling"]
    assert signals(20) == [
        "alarm",
        "evacuation_on_radio",
        "many_units",
        "official_order",
        "official_alert",
        "closure",
        "aircraft_circling",
    ]
    # The all-clear lifts the order, and the warning is no longer listed.
    assert signals(75) == [
        "alarm",
        "evacuation_on_radio",
        "many_units",
        "closure",
        "aircraft_circling",
    ]


def test_the_engine_reads_again_when_a_row_is_stored_and_not_before(tmp_path: Path) -> None:
    rows = Rows(mock_store(tmp_path))
    moment = [at(1)]
    searched = []

    class Counting(EvidenceStore):
        def search(self, **asked):  # type: ignore[no-untyped-def]
            searched.append(asked)
            return super().search(**asked)

    engine = Engine(Counting(rows.store.path, "mock"), NO_BLOCKS, clock=lambda: moment[0])
    rows.radio(0, "fire", "working", units=("E36",), street="Irving St", block=1900)

    assert len(engine.incidents()) == 1
    asked = len(searched)
    assert len(engine.incidents()) == 1
    assert len(searched) == asked

    rows.radio(0.5, "alarm", "dispatched", units=("E5",), street="Fell St", block=400)
    assert len(engine.incidents()) == 2
    # With no new row, it is read again once what it worked out is a few seconds old.
    asked = len(searched)
    moment[0] = at(1.2)
    engine.incidents()
    assert len(searched) > asked
    assert not engine.partial


def test_a_flood_of_taps_crowds_no_call_out_of_what_the_engine_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("beacon.incident.engine.MAX_ROWS", 3)
    monkeypatch.setattr("beacon.incident.engine.MAX_NEARBY", 4)
    rows = Rows(mock_store(tmp_path))
    rows.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    for step in range(10):
        rows.tap(1 + step / 10)

    engine = open_engine({}, rows.store, clock=lambda: at(5))
    (incident,) = engine.incidents()

    # The call is still read, and the taps are counted up to the most that are read.
    assert (incident.title, incident.taps) == ("Fire at the 400 block of Hayes St", 4)
    assert not engine.partial


def test_an_alert_older_than_the_horizon_is_still_ended_by_what_replaced_it(tmp_path: Path) -> None:
    rows = Rows(mock_store(tmp_path))
    # Issued a day ago for two days, replaced fourteen hours ago: both rows are
    # found by their spans, and the first is not in force.
    rows.alert(-1440, "first", until=1440, point=None)
    rows.alert(-840, "second", until=1440, message_type="Update", replaces=("first",))
    rows.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)

    (incident,) = open_engine({}, rows.store, clock=lambda: at(5)).incidents()

    assert [(item.source.evidence_id, item.state) for item in incident.official] == [
        (2, "in_force")
    ]


def test_the_live_engine_takes_its_blocks_from_the_file_the_environment_names(
    tmp_path: Path,
) -> None:
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    listed = tmp_path / "blocks.txt"
    listed.write_text("Hayes St,400,37.7766,-122.4236\nnot a block\n")

    assert open_engine({}, live).blocks.points == {}
    assert set(open_engine({BLOCKS_VARIABLE: str(listed)}, live).blocks.points) == {
        ("Hayes St", 400)
    }
    with pytest.raises(IncidentConfigError, match=BLOCKS_VARIABLE):
        open_engine({BLOCKS_VARIABLE: str(tmp_path / "missing.txt")}, live)


def test_the_routes_list_incidents_and_give_one_with_a_source_on_every_line(story: Story) -> None:
    app = create_app()
    app.state.incidents = story.play(20)
    client = TestClient(app)

    listed = client.get("/api/incidents").json()

    assert (listed["mode"], listed["partial"]) == ("mock", False)
    assert listed["as_of"] == "2026-11-27T20:00:00Z"
    assert [incident["title"] for incident in listed["incidents"]] == [
        "Medical response at the 5200 block of Geary Blvd",
        "Medical response at the 400 block of Fell St",
        "Fire at the 400 block of Hayes St",
    ]
    summary = listed["incidents"][-1]
    assert "timeline" not in summary
    assert (summary["open"], summary["alarm"], summary["stand_in"]) == (True, 3, True)
    assert summary["point"] == {"lat": FIRE.lat, "lon": FIRE.lon}

    answer = client.get(f"/api/incidents/{summary['id']}").json()
    fire = answer["incident"]
    assert (answer["mode"], fire["id"]) == ("mock", summary["id"])
    for entry in fire["timeline"]:
        assert entry["text"]
        assert entry["confidence"] in ("official", "high", "medium", "low")
        assert entry["sources"]
        assert all(source["label"] and source["stand_in"] for source in entry["sources"])
    closure = next(entry for entry in fire["timeline"] if entry["kind"] == "closure")
    assert closure["sources"][0]["credit"] == {
        "text": "data provided by 511.org",
        "url": "https://511.org",
    }
    order = next(item for item in fire["official"] if item["kind"] == "notice")
    assert (order["type"], order["reading"], order["instruction"]) == (
        "evacuation_order",
        True,
        None,
    )
    assert order["source"]["url"].endswith("/documents/evacuation-order-hayes-st.pdf")
    assert order["source"]["model"] == "fake"
    assert fire["aircraft"][0]["circling"] is True

    assert client.get("/api/incidents/inc-0000000000000000").status_code == 404


def test_an_incident_that_ended_leaves_the_list_two_hours_on_and_stays_readable(
    story: Story,
) -> None:
    app = create_app()
    app.state.incidents = story.play(100)
    client = TestClient(app)
    assert len(client.get("/api/incidents").json()["incidents"]) == 4

    story.moment = START + timedelta(minutes=260)

    assert client.get("/api/incidents").json()["incidents"] == []
    everything = client.get("/api/incidents", params={"all": "true"}).json()["incidents"]
    assert len(everything) == 4
    assert client.get(f"/api/incidents/{everything[0]['id']}").status_code == 200


def test_a_server_that_reads_no_incidents_says_so() -> None:
    client = TestClient(create_app())

    assert client.get("/api/incidents").status_code == 503
    assert client.get("/api/incidents/inc-1").json() == {
        "detail": "This server is not reading incidents."
    }


def test_the_server_reads_incidents_from_the_store_its_taps_go_to() -> None:
    with TestClient(create_app()) as client:
        answer = client.get("/api/incidents")

    assert answer.status_code == 200
    assert answer.json()["mode"] == "mock"
