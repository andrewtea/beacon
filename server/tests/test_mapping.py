import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beacon.evidence import Point
from beacon.incident import Engine, Incident
from beacon.incident.build import CREDIT_511
from beacon.incident.places import NO_BLOCKS
from beacon.main import create_app
from beacon.mapping import NO_MAP_OF_MEDICAL, NO_POINT, Feature, drawn
from beacon.mapping import features as mapping
from tests.incident_rows import HAYES, Rows, at, mock_store
from tests.test_incident_aircraft import circle, offset
from tests.test_investigate import BLOCKS, NAMES, city


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    made = Rows(mock_store(tmp_path))
    city(made)
    return made


def engine_of(rows: Rows, minute: float = 6, blocks: Any = BLOCKS) -> Engine:
    return Engine(rows.store, blocks, names=NAMES, clock=lambda: at(minute))


def fire_of(engine: Engine) -> Incident:
    (fire,) = [incident for incident in engine.incidents() if incident.category == "fire"]
    return fire


def map_of(rows: Rows, minute: float = 6, blocks: Any = BLOCKS) -> tuple[Feature, ...]:
    engine = engine_of(rows, minute, blocks)
    return drawn(fire_of(engine), rows.store.get)


def of_kind(features: tuple[Feature, ...], kind: str) -> list[Feature]:
    return [feature for feature in features if feature.kind == kind]


def line_of(incident: Incident, kind: str, evidence_id: int) -> str:
    (text,) = [
        entry.text
        for entry in incident.timeline
        if entry.kind == kind and evidence_id in {s.evidence_id for s in entry.sources}
    ]
    return text


# The place.


def test_the_place_is_the_block_named_on_the_radio_and_rests_on_the_calls(rows: Rows) -> None:
    features = map_of(rows)

    place = features[0]
    assert place.kind == "place"
    assert place.point == BLOCKS.points[("Hayes St", 400)]
    assert place.text == (
        "The point Beacon's list of blocks has for the block named on the radio. It marks "
        "the block, not an address."
    )
    # Trusted as the radio calls are, and no more.
    assert place.confidence == "medium"
    assert {source.source for source in place.sources} == {"openmhz"}
    assert len(place.sources) == 3
    assert all(source.credit is None for source in place.sources)


def test_a_place_taken_from_511_says_so_and_carries_511s_credit(rows: Rows) -> None:
    # With no list of blocks the fire is where 511 listed its closure.
    features = map_of(rows, blocks=NO_BLOCKS)

    place = features[0]
    assert (place.kind, place.point) == ("place", HAYES)
    assert place.text == (
        "An approximate point, which Beacon took from a 511 event that listed a street of "
        "this incident's as closed: the middle of what 511 drew for the event. Beacon has "
        "no point of its own for this incident. The point need not lie on the road, and it "
        "marks no address."
    )
    assert place.confidence == "official"
    (source,) = place.sources
    assert (source.source, source.credit) == ("511", CREDIT_511)


def test_a_place_taken_from_511_does_not_say_the_radio_named_a_block(rows: Rows) -> None:
    # A call that names a street and no block, and a 511 event that closes the street.
    rows.radio(3, "alarm", "on_scene", units=("E5",), street="Oak St")
    oak = offset(HAYES, -400, 0)
    rows.traffic(3.5, "511.org/2", road="Oak St", headline="Oak St closed", point=oak)
    (alarm,) = [i for i in engine_of(rows).incidents() if i.category == "alarm"]
    assert (alarm.point, alarm.point_from) == (oak, "closure")

    place = drawn(alarm, rows.store.get)[0]

    assert "block" not in place.text
    assert place.text.startswith("An approximate point, which Beacon took from a 511 event")


def test_an_incident_with_no_point_and_a_medical_response_have_nothing_drawn(rows: Rows) -> None:
    rows.radio(3, "alarm", "on_scene", units=("E5",), street="Oak St", block=100)
    engine = engine_of(rows, blocks=NO_BLOCKS)
    (alarm,) = [incident for incident in engine.incidents() if incident.category == "alarm"]
    assert alarm.point is None
    assert drawn(alarm, rows.store.get) == ()

    # The medical response has a point, a block south of the fire, and no map.
    (medical,) = [i for i in engine_of(rows).incidents() if i.category == "medical response"]
    assert medical.point is not None
    assert drawn(medical, rows.store.get) == ()


# 511's events.


def test_a_511_event_is_drawn_as_its_version_in_force_in_the_timelines_words(rows: Rows) -> None:
    engine = engine_of(rows)
    fire = fire_of(engine)

    (event,) = of_kind(drawn(fire, rows.store.get), "traffic_event")

    assert event.point == HAYES
    (source,) = event.sources
    assert event.text == line_of(fire, "closure", source.evidence_id)
    assert event.text == "511 lists: Activity on Hayes St"
    assert (event.confidence, source.credit) == ("official", CREDIT_511)
    # 511 said what the line says. Where the mark stands is Beacon's working out.
    assert event.note == (
        "Drawn at the middle of what 511 drew for the event, which Beacon worked out. It "
        "need not lie on the road."
    )


def test_only_the_version_of_a_511_event_that_is_in_force_is_drawn(rows: Rows) -> None:
    # A later version, somewhere else on the street.
    moved = offset(HAYES, 0, 120)
    later = rows.traffic(5.5, headline="Hayes St closed at Gough St", point=moved)

    (event,) = of_kind(map_of(rows), "traffic_event")
    assert (event.point, event.text) == (moved, "511 updated: Hayes St closed at Gough St")
    assert [source.evidence_id for source in event.sources] == [later.id]

    # 511 stops listing it. What Beacon saw is a line of the timeline, and nothing
    # is drawn as listed.
    rows.change(5.6, later)
    rows.change(5.8, later)
    assert of_kind(map_of(rows), "traffic_event") == []


# Aircraft.


def test_an_aircraft_of_the_timeline_is_drawn_where_it_was_seen_oldest_first(rows: Rows) -> None:
    circle(rows, HAYES, 300, minutes=4, icao24="a1b2c3")
    engine = engine_of(rows)
    fire = fire_of(engine)

    (aircraft,) = of_kind(drawn(fire, rows.store.get), "aircraft")

    (track,) = fire.aircraft
    assert [seen.point for seen in aircraft.path] == [row.point for row in track.rows]
    assert [seen.at for seen in aircraft.path] == sorted(seen.at for seen in aircraft.path)
    assert len(aircraft.path) == 9
    # The line the timeline has for it, with that line's sources and confidence.
    assert aircraft.text == line_of(fire, "aircraft", track.rows[0].id)
    assert aircraft.text.startswith("A helicopter (TEST1) circled nearby")
    assert {source.source for source in aircraft.sources} == {"opensky"}
    assert (aircraft.confidence, aircraft.note) == ("high", None)


def test_an_aircrafts_sources_are_the_rows_of_every_position_that_is_drawn(rows: Rows) -> None:
    circle(rows, HAYES, 300, minutes=4, icao24="a1b2c3")
    fire = fire_of(engine_of(rows))
    (track,) = fire.aircraft
    (line,) = [entry for entry in fire.timeline if entry.kind == "aircraft"]
    # The timeline's line names fewer rows than the path has positions: a sighting
    # in the middle of the path is not one of them.
    middle = track.rows[4]
    assert middle.id not in {source.evidence_id for source in line.sources}

    (aircraft,) = of_kind(drawn(fire, rows.store.get), "aircraft")

    named = [source.evidence_id for source in aircraft.sources]
    assert middle.id in named
    assert set(named) == {row.id for row in track.rows}
    # Each row once, the line's own first.
    assert len(named) == len(set(named)) == len(aircraft.path)
    assert named[: len(line.sources)] == [source.evidence_id for source in line.sources]


def test_an_aircraft_seen_more_often_than_is_drawn_says_so(
    rows: Rows, monkeypatch: pytest.MonkeyPatch
) -> None:
    circle(rows, HAYES, 300, minutes=4, icao24="a1b2c3")
    monkeypatch.setattr(mapping, "MAX_PATH", 4)
    fire = fire_of(engine_of(rows))

    (aircraft,) = of_kind(drawn(fire, rows.store.get), "aircraft")

    # The newest ones.
    assert [seen.point for seen in aircraft.path] == [
        row.point for row in fire.aircraft[0].rows[-4:]
    ]
    assert aircraft.note is not None
    assert aircraft.note.startswith("Only its newest")
    # The rows of the positions that are drawn, and the rows the sentence rests
    # on, which can be older than any of them.
    (line,) = [entry for entry in fire.timeline if entry.kind == "aircraft"]
    assert {source.evidence_id for source in aircraft.sources} == {
        *(row.id for row in fire.aircraft[0].rows[-4:]),
        *(source.evidence_id for source in line.sources),
    }


# Alerts.


def test_the_area_of_an_alert_in_force_is_drawn_in_the_authoritys_words(rows: Rows) -> None:
    engine = engine_of(rows)
    fire = fire_of(engine)

    (alert,) = of_kind(drawn(fire, rows.store.get), "alert_area")

    (ring,) = alert.rings
    assert len(ring) == 5
    assert ring[0] == ring[-1]
    # Around the fire, as the alert's own row has it: latitude and longitude the
    # right way round.
    assert all(point.distance_m(HAYES) < 700 for point in ring)
    (source,) = alert.sources
    assert alert.text == line_of(fire, "alert", source.evidence_id)
    assert alert.text == (
        "Department of Emergency Management issued an alert, Shelter In Place Warning: "
        "Headline of shelter"
    )
    assert (alert.confidence, source.source) == ("official", "nws")


def test_an_alert_that_is_not_in_force_or_has_no_drawn_area_is_not_drawn(tmp_path: Path) -> None:
    def alerts(build: Any) -> list[Feature]:
        made = Rows(mock_store(tmp_path / build.__name__))
        made.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
        build(made)
        return of_kind(map_of(made), "alert_area")

    def in_force(made: Rows) -> None:
        made.alert(4, "shelter")

    def cancelled(made: Rows) -> None:
        made.alert(4, "shelter")
        made.alert(5, "cancel", message_type="Cancel", replaces=("shelter",))

    def past_its_time(made: Rows) -> None:
        made.alert(4, "shelter", until=5)

    def no_longer_listed(made: Rows) -> None:
        first = made.alert(4, "shelter")
        made.change(5, first)

    def for_a_whole_zone(made: Rows) -> None:
        made.alert(4, "shelter", geometry=None)

    assert len(alerts(in_force)) == 1
    for build in (cancelled, past_its_time, no_longer_listed, for_a_whole_zone):
        assert alerts(build) == [], build.__name__


def test_an_area_with_more_points_than_are_drawn_is_left_out_whole_and_says_so(
    rows: Rows, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mapping, "MAX_RING_POINTS", 4)

    (alert,) = of_kind(map_of(rows), "alert_area")

    assert alert.rings == ()
    assert alert.note == (
        "Its area has more points than Beacon draws, so it is not drawn. The alert "
        "says which area it covers."
    )


def ring(*positions: tuple[float, float]) -> list[list[float]]:
    return [[lon, lat] for lat, lon in positions]


# A square around the fire, and a hole in it that the fire is not in.
SQUARE = ring(
    (37.77, -122.43), (37.77, -122.42), (37.78, -122.42), (37.78, -122.43), (37.77, -122.43)
)
HOLE = ring((37.771, -122.429), (37.771, -122.428), (37.772, -122.428), (37.771, -122.429))


def test_an_area_is_drawn_with_its_holes(tmp_path: Path) -> None:
    made = Rows(mock_store(tmp_path))
    made.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    made.alert(4, "shelter", geometry={"type": "Polygon", "coordinates": [SQUARE, HOLE]})

    (alert,) = of_kind(map_of(made), "alert_area")

    assert [len(found) for found in alert.rings] == [5, 4]
    assert alert.rings[1][0] == Point(37.771, -122.429)
    assert alert.note is None


@pytest.mark.parametrize(
    "broken",
    [
        # A position that is nowhere.
        [[-122.429, 37.771], [-122.428, 370.0], [-122.428, 37.772], [-122.429, 37.771]],
        # A ring that does not end where it began.
        [*HOLE[:-1], [-122.4285, 37.7715]],
        # Too few positions to be a ring.
        HOLE[:2] + HOLE[-1:],
        "a hole",
    ],
)
def test_an_area_with_a_hole_that_cannot_be_read_is_not_drawn_at_all_and_says_so(
    tmp_path: Path, broken: Any
) -> None:
    made = Rows(mock_store(tmp_path))
    made.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    made.alert(4, "shelter", geometry={"type": "Polygon", "coordinates": [SQUARE, broken]})
    fire = fire_of(engine_of(made))
    # The engine has the alert as in force for the place: the fire is in the square.
    assert [official.state for official in fire.official] == ["in_force"]

    (alert,) = of_kind(drawn(fire, made.store.get), "alert_area")

    # Not the square without its hole: that would show the hole as inside the area.
    assert alert.rings == ()
    assert alert.note == (
        "Beacon could not read every part of this alert's area, so none of it is drawn: "
        "an area drawn in part can show a place as inside it that the alert leaves out. "
        "The alert says which area it covers."
    )
    # The alert is still listed, in the authority's words and under its row.
    assert alert.text.startswith("Department of Emergency Management issued an alert")
    assert [source.source for source in alert.sources] == ["nws"]


def test_rings_are_read_whole_or_not_at_all() -> None:
    polygon = {"type": "Polygon", "coordinates": [SQUARE, HOLE]}
    assert [len(found) for found in mapping._rings(polygon) or ()] == [5, 4]

    broken = [[-122.43, 37.77], [-122.42, 370.0], [-122.42, 37.78], [-122.43, 37.77]]
    two = {"type": "MultiPolygon", "coordinates": [[SQUARE], [HOLE]]}
    assert [len(found) for found in mapping._rings(two) or ()] == [5, 4]
    # One polygon of several that cannot be read, and none is.
    assert mapping._rings({"type": "MultiPolygon", "coordinates": [[SQUARE], [broken]]}) is None
    assert mapping._rings({"type": "MultiPolygon", "coordinates": [[SQUARE], "x"]}) is None
    assert mapping._rings({"type": "MultiPolygon", "coordinates": [[SQUARE], []]}) is None
    for nothing in (
        None,
        "x",
        {},
        {"type": "Point", "coordinates": [-122.43, 37.77]},
        {"type": "GeometryCollection", "geometries": [polygon]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [SQUARE[:3]]},
        {"type": "Polygon", "coordinates": [[[True, 37.77]] * 4]},
        {"type": "MultiPolygon", "coordinates": []},
    ):
        assert mapping._rings(nothing) is None, nothing


# Taps and notices.


def test_nothing_of_a_tap_or_a_notice_is_drawn(rows: Rows) -> None:
    where = offset(HAYES, 37, 53)
    for minute in (4.2, 4.4, 4.6):
        rows.tap(minute, where)
    fire = fire_of(engine_of(rows))
    assert fire.taps == 3
    assert any(official.kind == "notice" for official in fire.official)

    features = drawn(fire, rows.store.get)

    assert {feature.kind for feature in features} == {"place", "traffic_event", "alert_area"}
    everywhere = [
        point
        for feature in features
        for point in (
            *([feature.point] if feature.point else []),
            *(seen.point for seen in feature.path),
            *(point for ring in feature.rings for point in ring),
        )
    ]
    assert where not in everywhere
    assert not any(source.kind in ("tap", "notice") for f in features for source in f.sources)


# The route.


@pytest.fixture
def client(rows: Rows) -> TestClient:
    circle(rows, HAYES, 300, minutes=4, icao24="a1b2c3")
    app = create_app()
    app.state.incidents = engine_of(rows)
    return TestClient(app)


def id_of(client: TestClient, category: str) -> str:
    listed = client.get("/api/incidents").json()["incidents"]
    (found,) = [incident["id"] for incident in listed if incident["category"] == category]
    return found


def test_the_map_of_an_incident_has_each_thing_with_its_words_its_confidence_and_its_sources(
    client: TestClient,
) -> None:
    fire = id_of(client, "fire")

    response = client.get(f"/api/incidents/{fire}/map")

    assert response.status_code == 200
    answer = response.json()
    assert (answer["mode"], answer["partial"], answer["text"]) == ("mock", False, None)
    assert answer["as_of"] == "2026-11-27T20:06:00Z"
    # The incident as its card has it.
    assert answer["incident"]["id"] == fire
    assert answer["incident"]["radio"]["confidence"] == "medium"
    features = answer["features"]
    assert [feature["kind"] for feature in features] == [
        "place",
        "traffic_event",
        "aircraft",
        "alert_area",
    ]
    for feature in features:
        assert feature["text"]
        assert feature["confidence"] in ("official", "high", "medium", "low")
        assert feature["sources"]
        # The invented scenario's rows say that they are that.
        assert all(source["stand_in"] for source in feature["sources"])
    place, event, aircraft, alert = features
    assert place["point"] == {"lat": 37.7766, "lon": -122.4236}
    assert event["sources"][0]["credit"] == {"text": CREDIT_511.text, "url": CREDIT_511.url}
    assert len(aircraft["path"]) == 9
    assert set(aircraft["path"][0]) == {"lat", "lon", "at"}
    assert len(alert["rings"][0]) == 5
    assert (place["path"], place["rings"], alert["point"], aircraft["note"]) == ([], [], None, None)
    assert len(aircraft["sources"]) == 9
    assert (place["note"], alert["note"]) == (None, None)
    assert event["note"].startswith("Drawn at the middle of what 511 drew for the event")


def test_a_medical_response_has_no_map_and_neither_has_an_incident_nobody_has(
    client: TestClient,
) -> None:
    medical = client.get(f"/api/incidents/{id_of(client, 'medical response')}/map")
    assert (medical.status_code, medical.json()) == (404, {"detail": NO_MAP_OF_MEDICAL})
    assert "Fell" not in medical.text

    nobody = client.get("/api/incidents/inc-0000000000000000/map")
    assert (nobody.status_code, nobody.json()) == (404, {"detail": "There is no such incident."})


def test_an_incident_with_no_point_is_answered_with_why_nothing_is_drawn(rows: Rows) -> None:
    rows.radio(3, "alarm", "on_scene", units=("E5",), street="Oak St", block=100)
    app = create_app()
    app.state.incidents = engine_of(rows, blocks=NO_BLOCKS)
    client = TestClient(app)

    answer = client.get(f"/api/incidents/{id_of(client, 'alarm')}/map").json()

    assert (answer["features"], answer["text"]) == ([], NO_POINT)
    assert answer["incident"]["point"] is None


def test_the_map_route_shows_no_tap(rows: Rows) -> None:
    where = offset(HAYES, 37, 53)
    rows.tap(4.2, where, ref="tapSecret0000001")
    app = create_app()
    app.state.incidents = engine_of(rows)
    client = TestClient(app)

    said = client.get(f"/api/incidents/{id_of(client, 'fire')}/map").text

    assert json.loads(said)["incident"]["taps"] == 1
    assert "tapSecret" not in said
    assert f"{where.lat:.4f}" not in said
