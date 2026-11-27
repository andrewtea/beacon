import asyncio
import json
import logging
import re
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beacon import main as main_module
from beacon.api import places as api_places
from beacon.evidence import Point
from beacon.incident import Engine
from beacon.incident.build import CREDIT_511
from beacon.incident.engine import Snapshot
from beacon.incident.places import NO_BLOCKS
from beacon.ingest.area import SAN_FRANCISCO
from beacon.main import create_app
from beacon.places import (
    MAX_BODY_BYTES,
    MAX_PLACES,
    NOT_AN_AREA,
    NOTHING_NEAR,
    Admission,
    PlacesError,
    near,
    read_places,
)
from tests.incident_rows import HAYES, Rows, at, mock_store
from tests.test_incident_aircraft import offset
from tests.test_investigate import BLOCKS, NAMES, city
from tests.test_taps import post_slowly

SHOWN = {"Beacon-Mode": "mock"}
# Somebody's home, as a phone has it. The position is invented.
HOME = {"lat": 37.776_812_3, "lon": -122.423_987_6}
# What must be found nowhere after a check: the place as the phone gave it, and
# as it is rounded.
PRECISE = ("37.7768123", "122.4239876", "768123", "239876", "37.777", "122.424")
FAR = offset(HAYES, 3000, 0)


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    made = Rows(mock_store(tmp_path))
    city(made)
    return made


@pytest.fixture
def moment() -> list[datetime]:
    return [at(6)]


@pytest.fixture
def engine(rows: Rows, moment: list[datetime]) -> Engine:
    return Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: moment[0])


@pytest.fixture
def client(engine: Engine) -> TestClient:
    app = create_app()
    app.state.incidents = engine
    return TestClient(app, headers=SHOWN)


def check(client: TestClient, *places: Any) -> Any:
    return client.post("/api/places/check", json={"places": list(places)})


def everything_kept(tmp_path: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}


# What is near a place.


def test_the_open_incidents_with_a_place_are_listed_nearest_first_with_a_rounded_distance(
    rows: Rows, engine: Engine
) -> None:
    # A second fire some 600 m north, on a block of its own.
    north = offset(HAYES, 600, 0)
    rows.radio(3, "fire", "working", units=("E5",), street="Oak St", block=100)
    blocks = BLOCKS.points | {("Oak St", 100): north}
    engine = Engine(
        rows.store,
        type(BLOCKS).of([(s, b, p.lat, p.lon) for (s, b), p in blocks.items()]),
        names=NAMES,
        clock=lambda: at(6),
    )
    place = offset(HAYES, 130, 0)

    found = near(place, engine.incidents())

    assert [(item.incident.title, item.distance_m) for item in found] == [
        ("Fire at the 400 block of Hayes St", 150),
        ("Fire at the 100 block of Oak St", 450),
    ]
    assert [item.text for item in found] == [
        "Beacon has this incident about 150 m from this place.",
        "Beacon has this incident about 450 m from this place.",
    ]


def test_a_distance_under_25_m_is_said_as_the_same_block_and_a_kilometre_is_the_reach(
    engine: Engine,
) -> None:
    incidents = engine.incidents()
    (here,) = near(offset(HAYES, 20, 0), incidents)
    assert (here.distance_m, here.text) == (
        0,
        "Beacon has this incident about where this place is.",
    )
    assert [item.distance_m for item in near(offset(HAYES, 995, 0), incidents)] == [1000]
    assert near(offset(HAYES, 1005, 0), incidents) == ()


def test_a_medical_response_a_closed_record_and_an_incident_with_no_place_are_not_listed(
    rows: Rows, moment: list[datetime]
) -> None:
    # The city's medical response is a block south of the fire, and open.
    engine = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: moment[0])
    listed = near(offset(HAYES, -100, 0), engine.incidents())
    assert [item.incident.category for item in listed] == ["fire"]
    assert {incident.category for incident in engine.incidents()} == {"fire", "medical response"}

    # With no list of blocks and no closure on its street, an incident has no place.
    rows.radio(3, "alarm", "on_scene", units=("E5",), street="Oak St", block=100)
    nowhere = Engine(rows.store, NO_BLOCKS, names=NAMES, clock=lambda: moment[0])
    assert [i.point for i in nowhere.incidents() if i.category == "alarm"] == [None]
    assert "alarm" not in {item.incident.category for item in near(HAYES, nowhere.incidents())}

    # An incident whose record is closed is not listed, wherever it is.
    (fire,) = [i for i in engine.incidents() if i.category == "fire"]
    assert [item.incident for item in near(HAYES, [fire])] == [fire]
    assert near(HAYES, [replace(fire, open=False)]) == ()


# Reading the places of a request.


def test_a_place_is_rounded_to_about_a_block_whatever_was_sent() -> None:
    assert read_places([HOME, {"lat": 37.78, "lon": -122.41}]) == (
        Point(37.777, -122.424),
        Point(37.78, -122.41),
    )


@pytest.mark.parametrize(
    "sent",
    [
        None,
        [],
        "37.77,-122.42",
        {"lat": 37.77, "lon": -122.42},
        [HOME] * (MAX_PLACES + 1),
        [{"lat": 37.77}],
        [{**HOME, "name": "Home"}],
        [{**HOME, "accuracy_m": 5}],
        [{"lat": "37.77", "lon": "-122.42"}],
        [{"lat": True, "lon": -122.42}],
        [{"lat": float("nan"), "lon": -122.42}],
        [{"lat": 10**400, "lon": -122.42}],
        [{"lat": 137.77, "lon": -122.42}],
        [HOME, [37.77, -122.42]],
    ],
)
def test_what_is_not_one_to_five_places_is_refused_in_a_fixed_sentence(sent: Any) -> None:
    with pytest.raises(PlacesError) as refused:
        read_places(sent)

    assert refused.value.status == 422
    assert str(refused.value).startswith("A request about places is a JSON object with places")
    # Nothing under the error could repeat a place.
    assert (refused.value.__cause__, refused.value.__context__) == (None, None)


def test_a_place_outside_san_francisco_is_refused_and_not_repeated() -> None:
    with pytest.raises(PlacesError) as refused:
        read_places([HOME, {"lat": 37.8044, "lon": -122.2712}])

    assert str(refused.value) == (
        "Beacon covers San Francisco, and one of these places is somewhere else."
    )
    assert (refused.value.__cause__, refused.value.__context__) == (None, None)


def test_the_app_saves_places_in_the_area_the_server_answers_for() -> None:
    # The app looks before it saves, so that a place this route would refuse is
    # never kept. Its box is written out in the app, and has to be this one.
    app_side = (Path(__file__).parents[2] / "web/src/lib/geo.ts").read_text(encoding="utf-8")
    found = re.search(
        r"COVERED = \{\s*south: ([-\d.]+),\s*west: ([-\d.]+),\s*north: ([-\d.]+),"
        r"\s*east: ([-\d.]+),?\s*\}",
        app_side,
    )
    assert found is not None
    assert tuple(float(number) for number in found.groups()) == (
        SAN_FRANCISCO.south,
        SAN_FRANCISCO.west,
        SAN_FRANCISCO.north,
        SAN_FRANCISCO.east,
    )


# The route.


def test_each_place_is_answered_in_the_order_sent_with_what_is_near_and_what_a_distance_is(
    client: TestClient,
) -> None:
    far = {"lat": FAR.lat, "lon": FAR.lon}

    response = check(client, far, HOME)

    assert response.status_code == 200
    answer = response.json()
    assert (answer["mode"], answer["partial"], answer["reach_m"]) == ("mock", False, 1000)
    assert answer["as_of"] == "2026-11-27T20:06:00Z"
    # A distance is not an area, and every answer says so.
    assert answer["caveat_text"] == NOT_AN_AREA
    assert (
        "does not tell you whether this place is inside or outside the area of an "
        "official order or alert"
    ) in answer["caveat_text"]
    nothing, home = answer["places"]
    # Nothing listed is said as that, and as no all-clear.
    assert nothing == {"near": [], "text": NOTHING_NEAR}
    assert nothing["text"].startswith(
        "Beacon lists no incident within a kilometre of this saved place in this reading."
    )
    assert "not an official all-clear" in nothing["text"]
    (fire,) = home["near"]
    assert (fire["incident"]["title"], fire["distance_m"]) == (
        "Fire at the 400 block of Hayes St",
        50,
    )
    assert fire["text"] == "Beacon has this incident about 50 m from this place."
    assert home["text"] is None
    # Measured from the block's own point: 511 has no part in it.
    assert (fire["incident"]["point_from"], fire["incident"]["point_source"]) == ("blocks", None)
    # The incident is as its own route has it: its radio basis, and that it is a
    # stand-in's.
    assert fire["incident"]["radio"]["confidence"] == "medium"
    assert fire["incident"]["stand_in"] is True
    # Nothing of the place is in the answer: not as sent, and not as rounded.
    assert not any(part in response.text for part in PRECISE)
    assert "lat" not in home


def test_no_sentence_about_a_place_tells_anybody_what_to_do_or_that_they_are_in_an_area(
    client: TestClient,
) -> None:
    answer = check(client, HOME, {"lat": FAR.lat, "lon": FAR.lon}).json()
    said = " ".join(
        [answer["caveat_text"]]
        + [place["text"] or "" for place in answer["places"]]
        + [item["text"] for place in answer["places"] for item in place["near"]]
    ).lower()

    for word in ("evacuate", "leave", "stay", "safe", "danger", "you are", "affected"):
        assert word not in said
    # Only to say that a distance does not tell which.
    assert "whether this place is inside or outside the area" in said
    assert "no open incident" not in said


def test_a_place_with_only_a_medical_response_near_is_answered_as_one_with_nothing_near(
    tmp_path: Path, moment: list[datetime]
) -> None:
    def answer_of(rows: Rows) -> Any:
        app = create_app()
        app.state.incidents = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: moment[0])
        point = BLOCKS.points[("Fell St", 400)]
        response = check(TestClient(app, headers=SHOWN), {"lat": point.lat, "lon": point.lon})
        return response.json()["places"]

    medical = Rows(mock_store(tmp_path / "medical"))
    medical.radio(2.5, "medical", units=("M52",), street="Fell St", block=400)
    engine = Engine(medical.store, BLOCKS, names=NAMES, clock=lambda: moment[0])
    (response,) = engine.incidents()
    assert (response.category, response.open) == ("medical response", True)
    assert response.point == BLOCKS.points[("Fell St", 400)]

    # The same answer as where Beacon has nothing at all: the sentence does not say
    # that there is no incident, and does not give away that there is a medical one.
    assert answer_of(medical) == answer_of(Rows(mock_store(tmp_path / "empty")))
    assert answer_of(medical) == [{"near": [], "text": NOTHING_NEAR}]


def test_a_distance_measured_from_a_point_of_511s_says_so_and_carries_511s_credit(
    rows: Rows, moment: list[datetime]
) -> None:
    # With no list of blocks the fire is where 511 says its closure is.
    app = create_app()
    app.state.incidents = Engine(rows.store, NO_BLOCKS, names=NAMES, clock=lambda: moment[0])

    answer = check(TestClient(app, headers=SHOWN), HOME).json()

    (fire,) = answer["places"][0]["near"]
    assert fire["incident"]["point_from"] == "closure"
    assert fire["text"] == (
        f"Beacon has this incident about {fire['distance_m']} m from this place, going by "
        "where 511 lists a road closure on its street."
    )
    # The row the point is from, with how far it is trusted and 511's credit.
    source = fire["incident"]["point_source"]
    assert (source["source"], source["confidence"]) == ("511", "official")
    assert source["credit"] == {"text": CREDIT_511.text, "url": CREDIT_511.url}


def test_a_check_keeps_nothing_and_logs_nothing(
    client: TestClient, rows: Rows, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rows.store.search(kinds=["radio"])
    before = everything_kept(tmp_path)
    count = rows.store.count_by_source()

    assert check(client, HOME).status_code == 200
    assert check(client, {**HOME, "name": "Jane's home"}).status_code == 422

    assert everything_kept(tmp_path) == before
    assert rows.store.count_by_source() == count
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert not any(part in logged for part in (*PRECISE, "Jane"))
    assert "/api/places" not in logged


def test_the_route_is_one_of_the_private_paths() -> None:
    assert main_module._is_private("/api/places/check")
    assert main_module._is_private("/served/under/api/places/check")
    line = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.77:50123", "POST", "/api/places/check", "1.1", 200),
        None,
    )
    assert main_module._NO_PRIVATE_LINES.filter(line) is False


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'{"places": [{"lat": 37.7768123, "lon": -122.4239876}], "name": "Home"}',
        b'{"place": [{"lat": 37.7768123, "lon": -122.4239876}]}',
        b'{"places": [{"lat": 37.7768123, "lon": -122.4239876, "name": "Home"}]}',
        b'{"places": [[[[[[[[[[37.7768123]]]]]]]]]]}',
        b'{"places": [{"lat": 37.7768123, "lon": -122.4239876}]' + b" " * 10 + b"x",
        '{"places": [{"lat": 37.7768123, "lon": -122.4239876}]}'.encode("utf-16"),
    ],
)
def test_a_body_that_is_not_the_form_is_refused_and_nothing_of_it_is_repeated(
    client: TestClient, body: bytes
) -> None:
    response = client.post("/api/places/check", content=body)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("A request about places is a JSON object")
    assert not any(part in response.text for part in (*PRECISE, "Home"))


def test_a_body_longer_than_a_request_about_places_may_be_is_refused(client: TestClient) -> None:
    body = json.dumps({"places": [HOME], "pad": "A" * MAX_BODY_BYTES}).encode()

    response = client.post("/api/places/check", content=body)

    assert (response.status_code, response.json()["detail"]) == (
        413,
        "A request about places is at most two kilobytes.",
    )


@pytest.mark.parametrize(
    ("shown", "status"),
    [(b"live", 409), (None, 400), (b"MOCK", 400)],
)
def test_a_request_made_for_the_other_mode_is_refused_unread(
    engine: Engine, shown: bytes | None, status: int
) -> None:
    app = create_app()
    app.state.incidents = engine

    async def nothing() -> dict[str, Any]:
        raise AssertionError("The body of a request that was to be refused was read.")

    headers = [] if shown is None else [(b"beacon-mode", shown)]
    refused, answer = post_slowly(
        app, nothing, headers=headers, mode=None, path="/api/places/check"
    )

    assert refused == status
    assert "othing of" in answer["detail"]
    assert "was read" in answer["detail"]


def test_a_request_that_does_not_arrive_in_time_is_not_waited_for(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_places, "RECEIVE_SECONDS", 0.05)
    app = create_app()
    app.state.incidents = engine
    pieces = [{"type": "http.request", "body": b'{"places": ', "more_body": True}]

    async def stalls() -> dict[str, Any]:
        if pieces:
            return pieces.pop()
        await asyncio.sleep(3600)
        raise AssertionError("The server waited an hour for a request about places.")

    status, answer = post_slowly(app, stalls, path="/api/places/check")

    assert status == 408
    assert answer == {
        "detail": "A request about places has to arrive within five seconds. Send it again."
    }
    assert app.state.place_checks.held == 0


def test_a_sender_that_goes_away_mid_request_is_answered_and_raises_nothing(
    engine: Engine,
) -> None:
    app = create_app()
    app.state.incidents = engine
    pieces = [
        {"type": "http.disconnect"},
        {"type": "http.request", "body": b'{"places": ', "more_body": True},
    ]

    async def leaves() -> dict[str, Any]:
        return pieces.pop()

    # Nothing is raised out of the app, which is what a server would log.
    status, answer = post_slowly(app, leaves, path="/api/places/check")

    assert status == 400
    assert answer == {"detail": "The request ended before all of it had arrived."}
    assert app.state.place_checks.held == 0


class Held(Engine):
    """An engine whose reading waits until it is let go on."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.reading = threading.Event()
        self.go_on = threading.Event()

    @property
    def mode(self) -> Any:
        return self._engine.mode

    def snapshot(self) -> Snapshot:
        self.reading.set()
        assert self.go_on.wait(10)
        return self._engine.snapshot()


def test_only_so_many_checks_are_worked_on_at_once_and_one_more_is_refused_unread(
    engine: Engine,
) -> None:
    held = Held(engine)
    app = create_app()
    app.state.incidents = held
    app.state.place_checks = Admission(most=1)
    client = TestClient(app, headers=SHOWN)
    first: list[Any] = []
    working = threading.Thread(target=lambda: first.append(check(client, HOME)))
    working.start()
    try:
        assert held.reading.wait(10)
        assert app.state.place_checks.held == 1

        # One more, while the first is being answered. Its body is not read.
        async def nothing() -> dict[str, Any]:
            raise AssertionError("The body of a request there was no place for was read.")

        status, answer = post_slowly(app, nothing, path="/api/places/check")
        assert status == 503
        assert answer == {
            "detail": "Beacon is busy with other requests about places. Try again in a moment."
        }
        busy = check(client, HOME)
        assert (busy.status_code, busy.headers["retry-after"]) == (503, "1")
    finally:
        held.go_on.set()
        working.join(10)

    # The first is answered, its place is given back, and the next is taken.
    assert first[0].status_code == 200
    assert app.state.place_checks.held == 0
    assert check(client, HOME).status_code == 200
    # A request that is refused for what it holds gives its place back too.
    assert check(client, {**HOME, "name": "Home"}).status_code == 422
    assert app.state.place_checks.held == 0


def test_a_server_that_reads_no_incidents_answers_no_check() -> None:
    response = TestClient(create_app(), headers=SHOWN).post(
        "/api/places/check", json={"places": [HOME]}
    )

    assert (response.status_code, response.json()["detail"]) == (
        503,
        "This server is not reading incidents.",
    )
