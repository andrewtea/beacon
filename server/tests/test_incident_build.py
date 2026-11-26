"""Building incidents: what is attached to the calls, how it is worded, and its source."""

import math
from pathlib import Path

import pytest

from beacon.evidence import Point
from beacon.incident import NO_BLOCKS, Blocks, Engine, Incident
from beacon.incident.build import CREDIT_511
from tests.incident_rows import HAYES, Rows, at, mock_store

BLOCKS = Blocks.of([("Hayes St", 400, HAYES.lat, HAYES.lon)])
FAR = Point(37.7636, -122.4783)


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


def incidents(rows: Rows, minute: float, blocks: Blocks = BLOCKS) -> list[Incident]:
    return Engine(rows.store, blocks, clock=lambda: at(minute)).incidents()


def fire(rows: Rows, **fields: object) -> None:
    rows.radio(0, "fire", "working", units=("E36", "T6"), street="Hayes St", block=400, **fields)  # type: ignore[arg-type]


def test_every_line_of_a_timeline_names_its_row_its_source_and_its_confidence(rows: Rows) -> None:
    call = rows.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    heard = rows.radio(1, "fire", "working", units=("E36",), clarity="partial")
    closure = rows.traffic(5)
    alert = rows.alert(6, "shelter")
    notice = rows.notice(7, "evacuation_order")

    (incident,) = incidents(rows, 10)

    assert (incident.title, incident.place) == (
        "Fire at the 400 block of Hayes St",
        "400 block of Hayes St",
    )
    assert (incident.point, incident.point_from) == (HAYES, "blocks")
    assert [
        (entry.kind, entry.confidence, entry.sources[0].evidence_id, entry.sources[0].source)
        for entry in incident.timeline
    ] == [
        ("radio", "medium", call.id, "openmhz"),
        ("radio", "low", heard.id, "openmhz"),
        ("closure", "official", closure.id, "511"),
        ("alert", "official", alert.id, "nws"),
        ("notice", "high", notice.id, "notices"),
    ]
    assert all(entry.sources and entry.text for entry in incident.timeline)
    assert "Only part of the call could be made out." in incident.timeline[1].text
    # 511's data agreement: its credit is beside everything shown from it.
    assert [entry.sources[0].credit for entry in incident.timeline] == [
        None,
        None,
        CREDIT_511,
        None,
        None,
    ]
    # Every row here is the invented store's, and two are a fake Ember's.
    assert incident.stand_in
    assert [entry.sources[0].model for entry in incident.timeline] == [
        "fake",
        "fake",
        None,
        None,
        "fake",
    ]
    assert incident.evidence_ids == (call.id, heard.id, closure.id, alert.id, notice.id)


def test_a_medical_response_is_its_crews_and_its_block_and_nothing_else(rows: Rows) -> None:
    # A row that holds more than a medical row may, as one written by other code could.
    rows.radio(
        0,
        "medical",
        "on_scene",
        units=("M52",),
        street="Hayes St",
        block=400,
        details=("rescue_in_progress",),
        alarm=2,
    )
    rows.traffic(1)
    rows.alert(1, "shelter")
    rows.notice(2, "evacuation_order")
    rows.tap(2)

    (incident,) = incidents(rows, 5)

    assert (incident.category, incident.title) == (
        "medical response",
        "Medical response at the 400 block of Hayes St",
    )
    (entry,) = incident.timeline
    assert entry.text == "Medical response. Units: M52. Place: 400 block of Hayes St."
    assert entry.details == {
        "event": "medical response",
        "units": ["M52"],
        "place": "400 block of Hayes St",
        "clarity": "clear",
    }
    assert (incident.alarm, incident.official, incident.signals, incident.taps) == (None, (), (), 0)
    assert (incident.aircraft, incident.status) == ((), "unknown")


def test_what_crews_say_about_an_evacuation_is_radio_traffic_and_not_an_order(rows: Rows) -> None:
    fire(rows, details=("evacuation", "all_clear", "shelter_in_place"))

    (incident,) = incidents(rows, 5)

    text = incident.timeline[0].text
    assert text.count("That is radio traffic, not an official order.") == 3
    assert incident.official == ()
    (signal,) = incident.signals
    assert (signal.kind, "not an official order" in signal.text) == ("evacuation_on_radio", True)


def test_an_alerts_instruction_is_the_authoritys_own_and_a_notices_words_are_not_shown(
    rows: Rows,
) -> None:
    fire(rows)
    alert = rows.alert(5, "shelter")
    notice = rows.notice(
        6,
        "evacuation_order",
        # A name with the shape of a street, which no list has: a model wrote it.
        streets=("Hayes St", "Janedoe St"),
        issuer="An Agency",
        title="Order naming Jane Doe",
        areas=["432 Hayes St, the home of Jane Doe"],
        instruction="Jane Doe must leave 432 Hayes St.",
        summary="Jane Doe, 67.",
    )

    (incident,) = incidents(rows, 10)

    official_alert, official_notice = incident.official
    assert (official_alert.kind, official_alert.reading, official_alert.state) == (
        "alert",
        False,
        "in_force",
    )
    assert (official_alert.instruction, official_alert.headline) == (
        "Instruction of shelter",
        "Headline of shelter",
    )
    assert official_alert.source.evidence_id == alert.id
    # A notice is a model's reading: its type, that it is a reading, and its link.
    assert (official_notice.kind, official_notice.type, official_notice.reading) == (
        "notice",
        "evacuation_order",
        True,
    )
    assert (official_notice.issuer, official_notice.headline, official_notice.instruction) == (
        None,
        None,
        None,
    )
    assert official_notice.source.url == notice.url
    assert official_notice.source.confidence == "high"
    # Published, and never said to be in force: Beacon has only read the document.
    assert official_notice.state == "published"
    shown = repr(incident)
    assert "Jane" not in shown
    assert "432" not in shown
    assert "An Agency" not in shown
    assert "Janedoe" not in shown
    (entry,) = [entry for entry in incident.timeline if entry.kind == "notice"]
    assert entry.text == (
        "An authority published an evacuation order. This is Beacon's reading of the "
        "document. Read the document for what it says."
    )
    assert entry.details["streets"] == ["Hayes St"]


def test_a_dropped_alert_is_what_beacon_saw_and_never_an_all_clear(rows: Rows) -> None:
    fire(rows)
    alert = rows.alert(5, "shelter")
    closure = rows.traffic(6)
    rows.change(20, alert)
    rows.change(22, closure)

    (incident,) = incidents(rows, 30)

    (official,) = incident.official
    assert official.state == "not_listed"
    dropped = [entry for entry in incident.timeline if entry.details.get("change")]
    assert [(entry.kind, entry.confidence) for entry in dropped] == [
        ("alert", "medium"),
        ("closure", "medium"),
    ]
    assert "not an official all-clear" in dropped[0].text
    assert "not an official reopening" in dropped[1].text
    assert {signal.kind for signal in incident.signals} == set()


def test_an_all_clear_lifts_the_order_and_the_incident_says_which(rows: Rows) -> None:
    fire(rows)
    order = rows.notice(5, "evacuation_order")
    assert [signal.kind for signal in incidents(rows, 10)[0].signals] == ["official_order"]

    clear = rows.notice(40, "all_clear")
    (incident,) = incidents(rows, 45)

    assert [(item.type, item.state, item.ended_by) for item in incident.official] == [
        ("evacuation_order", "lifted", clear.id),
        ("all_clear", "published", None),
    ]
    assert incident.official[0].source.evidence_id == order.id
    assert incident.signals == ()


def test_only_what_was_said_about_this_place_at_this_time_is_attached(rows: Rows) -> None:
    fire(rows)
    # Road work that has been there since morning, on the same street.
    rows.traffic(1, "511.org/old", created=-600)
    # A closure across town, an alert drawn for another place, and a city-wide one.
    rows.traffic(2, "511.org/far", road="Lincoln Way", point=FAR)
    rows.alert(2, "elsewhere", point=FAR)
    rows.alert(-180, "wind", point=None, until=600)
    # A notice about another street, and a document Ember did not take for a notice.
    rows.notice(3, "evacuation_order", streets=("Irving St",))
    rows.notice(3, "other", hazard="none", streets=())
    # A closure on a street the calls do not name, within reach of the point.
    near = rows.traffic(4, "511.org/near", road="Ivy St")
    rows.tap(5)
    rows.tap(5, FAR)

    (incident,) = incidents(rows, 10)

    assert [entry.kind for entry in incident.timeline] == ["radio", "closure", "taps"]
    assert incident.timeline[1].sources[0].evidence_id == near.id
    assert (incident.official, incident.taps) == ((), 1)


def test_taps_are_counted_and_never_pointed_at_one_by_one(rows: Rows) -> None:
    fire(rows)
    taps = [rows.tap(minute) for minute in (1, 2, 3)]

    (incident,) = incidents(rows, 5)

    (entry,) = [entry for entry in incident.timeline if entry.kind == "taps"]
    assert entry.text == '3 people nearby pressed "What\'s that?" while this was going on.'
    (source,) = entry.sources
    assert (source.evidence_id, source.confidence, source.stand_in) == (None, "low", False)
    assert (entry.confidence, incident.taps) == ("low", 3)
    # A tap's id is what its sender reads the answer with, so no incident holds one.
    assert not {tap.id for tap in taps} & set(incident.evidence_ids)
    assert all(tap.ref not in repr(incident) for tap in taps)
    assert [signal.kind for signal in incident.signals] == ["taps"]


def test_without_a_list_of_blocks_a_closure_on_the_street_gives_the_point(rows: Rows) -> None:
    fire(rows)
    rows.tap(2)
    (unplaced,) = incidents(rows, 3, NO_BLOCKS)
    assert (unplaced.point, unplaced.point_from, unplaced.taps) == (None, None, 0)

    rows.traffic(4)
    (placed,) = incidents(rows, 5, NO_BLOCKS)

    assert (placed.point, placed.point_from, placed.taps) == (HAYES, "closure", 1)


def test_a_circling_helicopter_is_a_line_and_a_signal_and_a_high_airliner_is_neither(
    rows: Rows,
) -> None:
    fire(rows)
    for step in range(13):
        angle = math.tau * step * 30 / 108
        point = Point(
            HAYES.lat + 400 * math.cos(angle) / 111_320,
            HAYES.lon + 400 * math.sin(angle) / (111_320 * math.cos(math.radians(HAYES.lat))),
        )
        rows.aircraft(1 + step / 2, point, icao24="heli01")
        rows.aircraft(1 + step / 2, HAYES, icao24="high01", altitude_m=2500, category="large")

    (incident,) = incidents(rows, 10)

    (track,) = incident.aircraft
    assert (track.icao24, track.circling) == ("heli01", True)
    (entry,) = [entry for entry in incident.timeline if entry.kind == "aircraft"]
    assert entry.text == "A helicopter (TEST1) circled nearby for about 6 min, as low as 300 m."
    assert (entry.confidence, len(entry.sources)) == ("high", 2)
    assert [signal.kind for signal in incident.signals] == ["aircraft_circling"]


def test_an_incident_is_open_while_it_is_heard_from_and_closed_once_cleared(rows: Rows) -> None:
    fire(rows)
    first = incidents(rows, 10)[0]
    assert (first.open, first.status) == (True, "working")
    # Nothing more is heard: it goes quiet without ever being cleared.
    assert not incidents(rows, 50)[0].open

    rows.radio(20, "fire", "cleared", units=("E36", "T6"))
    cleared = incidents(rows, 21)[0]

    assert (cleared.open, cleared.status) == (False, "cleared")
    # The incident keeps its name as its rows are added to.
    assert cleared.id == first.id


def test_what_is_said_after_now_is_not_yet_known(rows: Rows) -> None:
    fire(rows, alarm=2)
    rows.radio(30, "fire", "working", units=("E36",), alarm=3)
    rows.alert(30, "shelter")

    (incident,) = incidents(rows, 10)

    assert (incident.alarm, incident.official, len(incident.timeline)) == (2, (), 1)
