"""Building incidents: what is attached to the calls, how it is worded, and its source."""

import math
from pathlib import Path

import pytest

from beacon.evidence import NewEvidence, Point
from beacon.incident import NO_BLOCKS, Blocks, Engine, Incident
from beacon.incident.build import CREDIT_511
from beacon.ingest import nws, traffic511
from beacon.redact import Names
from tests.incident_rows import HAYES, Rows, area, at, mock_store

BLOCKS = Blocks.of([("Hayes St", 400, HAYES.lat, HAYES.lon)])
NAMES = Names.of(
    streets=("Hayes St", "Gough St", "Octavia St"), places=("Civic Center Plaza", "An Agency")
)
FAR = Point(37.7636, -122.4783)


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


def incidents(
    rows: Rows, minute: float, blocks: Blocks = BLOCKS, names: Names | None = None
) -> list[Incident]:
    return Engine(rows.store, blocks, names=names, clock=lambda: at(minute)).incidents()


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
    # With no list of names nothing of the notice passes, and all of it is withheld.
    assert official_notice.withheld == ("headline", "instruction", "summary", "issuer", "areas")

    # With the lists, every text that names a person is still left out whole. The
    # issuer is a listed public body and nothing else, so it is shown.
    (incident,) = incidents(rows, 10, names=NAMES)
    official_notice = incident.official[1]
    assert official_notice.issuer == "An Agency"
    assert official_notice.withheld == ("headline", "instruction", "summary", "areas")
    shown = repr(incident)
    assert "Jane" not in shown
    assert "432" not in shown
    assert "Janedoe" not in shown
    (entry,) = [entry for entry in incident.timeline if entry.kind == "notice"]
    assert entry.text == (
        "An authority published an evacuation order. This is Beacon's reading of the "
        "document. Read the document for what it says."
    )
    assert entry.details["streets"] == ["Hayes St"]


def test_a_notices_words_are_shown_as_a_reading_when_the_whole_text_passes(rows: Rows) -> None:
    fire(rows)
    notice = rows.notice(
        6,
        "evacuation_order",
        streets=("Hayes St", "Gough St"),
        issuer="San Francisco Department of Emergency Management",
        title="Evacuation Order: 400 block of Hayes St",
        areas=["Both sides of Hayes St between Gough St and Octavia St", "Civic Center Plaza"],
        instruction="Leave now, on foot. Do not use Hayes St.",
        summary="Everyone on Hayes St between Gough St and Octavia St is ordered to leave.",
        incomplete=["title", "streets"],
    )

    (incident,) = incidents(rows, 10, names=NAMES)

    (official,) = incident.official
    assert (official.reading, official.source.url, official.state) == (
        True,
        notice.url,
        "published",
    )
    assert official.issuer == "San Francisco Department of Emergency Management"
    assert official.instruction == "Leave now, on foot. Do not use Hayes St."
    assert official.summary == (
        "Everyone on Hayes St between Gough St and Octavia St is ordered to leave."
    )
    assert official.areas == (
        "Both sides of Hayes St between Gough St and Octavia St",
        "Civic Center Plaza",
    )
    # A title in title case has capitals in the middle of a sentence, as a name
    # has, and is left out. What the row says is not all there is said too.
    assert (official.headline, official.withheld) == (None, ("headline",))
    assert official.incomplete == ("headline", "streets")
    # The timeline's line still shows none of the words.
    (line,) = [entry for entry in incident.timeline if entry.kind == "notice"]
    assert "Leave" not in repr(line)


def test_a_notices_areas_are_shown_all_together_or_not_at_all(rows: Rows) -> None:
    fire(rows)
    rows.notice(
        6,
        "evacuation_order",
        areas=["Hayes St between Gough St and Octavia St", "432 Hayes St, the home of Jane Doe"],
        instruction="Leave now.",
    )

    (incident,) = incidents(rows, 10, names=NAMES)

    (official,) = incident.official
    assert (official.areas, official.instruction) == ((), "Leave now.")
    assert official.withheld == ("areas",)


def test_nothing_is_read_of_a_press_release_whatever_its_row_holds(rows: Rows) -> None:
    fire(rows)
    # Ingest keeps no text of a press release. A row that held one all the same,
    # in words that would pass, is shown none of.
    rows.notice(
        6,
        "press_release",
        streets=(),
        issuer="San Francisco Fire Department",
        title="The fire is out.",
        areas=["Hayes St"],
        instruction="Leave now.",
        summary="The fire on Hayes St is out.",
        incomplete=["summary"],
    )

    (incident,) = incidents(rows, 10, names=NAMES)

    (official,) = incident.official
    assert (official.type, official.reading) == ("press_release", True)
    assert (
        official.issuer,
        official.headline,
        official.instruction,
        official.summary,
        official.areas,
        official.withheld,
        official.incomplete,
    ) == (None, None, None, None, (), (), ())


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


def test_an_all_clear_is_published_beside_the_order_and_is_not_said_to_lift_it(rows: Rows) -> None:
    fire(rows)
    # The order is for the 400 block and the all-clear for the 1800 block of the
    # same street, which only the documents say: a notice's row keeps no block.
    order = rows.notice(5, "evacuation_order")
    (signal,) = incidents(rows, 10)[0].signals
    assert signal.text == (
        "An evacuation order was published by an authority. This is Beacon's reading "
        "of the document. Read the document for what it says."
    )

    clear = rows.notice(40, "all_clear")
    (incident,) = incidents(rows, 45)

    assert [(item.type, item.state, item.ended_by) for item in incident.official] == [
        ("evacuation_order", "published", None),
        ("all_clear", "published", None),
    ]
    # The order is still a signal, and the all-clear is said beside it.
    (signal,) = incident.signals
    assert signal.kind == "official_notice"
    assert "An all-clear was also published by an authority." in signal.text
    assert "Beacon does not work out what an all-clear covers" in signal.text
    assert [source.evidence_id for source in signal.sources] == [order.id, clear.id]
    assert "lift" not in repr(incident)


def test_a_notice_ends_only_at_the_time_its_own_document_gave(rows: Rows) -> None:
    fire(rows)
    row = rows.store.add(
        [
            NewEvidence(
                kind="notice",
                source="notices",
                ref="order-with-an-end",
                occurred_at=at(5),
                until=at(30),
                confidence="high",
                origin="mock",
                model="fake",
                url="https://notices.example/end.pdf",
                data={"notice_type": "shelter_in_place", "hazard": "fire", "streets": ["Hayes St"]},
            )
        ]
    )[0]

    assert [(i.source.evidence_id, i.state) for i in incidents(rows, 20)[0].official] == [
        (row.id, "published")
    ]
    (incident,) = incidents(rows, 31)
    assert [(item.state, item.ended_by) for item in incident.official] == [("ended", None)]
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
    # Taps, and not people: three taps can be one person.
    assert entry.text == (
        '3 taps of "What\'s that?" came from nearby while this was going on. Beacon '
        "cannot tell whether they are from different people."
    )
    assert "people nearby" not in repr(incident)
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


def test_a_closure_gives_the_point_only_where_it_lists_the_incidents_own_road_closed(
    rows: Rows,
) -> None:
    fire(rows)
    # The event is the incident's by its street, which this version lists as open:
    # the road it closes is another, and its point is that road's.
    rows.traffic(2, point=FAR, roads=(("Hayes St", False), ("Lincoln Way", True)))
    (incident,) = incidents(rows, 3, NO_BLOCKS)
    assert (incident.point, incident.point_from, incident.point_source) == (None, None, None)
    assert len(incident.timeline) == 2
    # A road closed somewhere else is no closure at this incident.
    assert incident.signals == ()

    # A version that closes Hayes St says where the incident is, and is a signal.
    closing = rows.traffic(4, created=2)
    (incident,) = incidents(rows, 5, NO_BLOCKS)
    assert [signal.kind for signal in incident.signals] == ["closure"]

    # A later one in which Hayes St is open again and another road is closed does
    # not move the incident, and is no closure at it. The point is still 511's,
    # and goes with 511's credit when no line beside it carries one.
    rows.traffic(6, created=2, point=FAR, roads=(("Hayes St", False), ("Lincoln Way", True)))
    (incident,) = incidents(rows, 7, NO_BLOCKS)
    assert (incident.point, incident.point_from) == (HAYES, "closure")
    assert incident.signals == ()
    assert incident.point_source is not None
    assert (incident.point_source.evidence_id, incident.point_source.credit) == (
        closing.id,
        CREDIT_511,
    )


def test_a_closure_near_the_point_on_a_road_no_call_named_is_a_signal(rows: Rows) -> None:
    fire(rows)
    rows.traffic(2, road="Ivy St", point=Point(HAYES.lat + 0.001, HAYES.lon))
    # Slow traffic near the point, with no road closed, is none.
    rows.traffic(3, "511.org/2", road="Linden St", closed=False)

    (incident,) = incidents(rows, 5)

    assert (incident.point_from, incident.point_source) == ("blocks", None)
    assert [signal.kind for signal in incident.signals] == ["closure"]
    assert len(incident.signals[0].sources) == 1


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
    (signal,) = incident.signals
    assert (signal.kind, signal.confidence) == ("aircraft_circling", "high")


def test_a_circling_aircraft_is_trusted_as_little_as_its_weakest_position(rows: Rows) -> None:
    fire(rows)
    positions = []
    for step in range(13):
        angle = math.tau * step * 30 / 108
        point = Point(
            HAYES.lat + 400 * math.cos(angle) / 111_320,
            HAYES.lon + 400 * math.sin(angle) / (111_320 * math.cos(math.radians(HAYES.lat))),
        )
        # One position in the middle of the track is worth less than the rest.
        confidence = "low" if step == 6 else "high"
        positions.append(rows.aircraft(1 + step / 2, point, confidence=confidence))

    (incident,) = incidents(rows, 10)

    (entry,) = [entry for entry in incident.timeline if entry.kind == "aircraft"]
    (signal,) = incident.signals
    cited = [positions[0].id, positions[6].id, positions[-1].id]
    for claim in (entry, signal):
        assert claim.confidence == "low"
        # The weak position is among the sources, so the claim's confidence is one
        # of its sources'.
        assert [source.evidence_id for source in claim.sources] == cited
        assert [source.confidence for source in claim.sources] == ["high", "low", "high"]


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


def test_an_alert_applies_only_where_its_area_holds_the_place(rows: Rows) -> None:
    fire(rows)
    # Drawn around a middle 300 m north of the fire, and reaching only 150 m from it.
    north = Point(HAYES.lat + 300 / 111_320, HAYES.lon)
    rows.alert(1, "near", point=north, geometry=area(north, 150))
    # Drawn around the fire, with the fire's own block cut out of it.
    rows.alert(2, "hole", geometry=area(HAYES, 400, hole_m=100))
    # An alert with a middle and no area drawn says nothing of which places it holds.
    rows.alert(3, "undrawn", geometry=None)
    inside = rows.alert(4, "inside", geometry=area(HAYES, 400))

    (incident,) = incidents(rows, 10)

    assert [item.source.evidence_id for item in incident.official] == [inside.id]
    (signal,) = incident.signals
    assert (signal.kind, signal.text) == (
        "official_alert",
        "An official alert whose area holds this place is in force.",
    )


def test_a_text_that_may_have_been_cut_is_left_out_whole_and_said_to_be(rows: Rows) -> None:
    fire(rows)
    cut = "Residents may return" + "." * (nws.MAX_TEXT - 20)
    assert len(cut) == nws.MAX_TEXT
    rows.alert(1, "long", instruction=cut, headline="h" * nws.MAX_HEADLINE)
    rows.alert(2, "short", instruction="x" * (nws.MAX_TEXT - 1))
    rows.traffic(3, headline="c" * traffic511.MAX_HEADLINE)

    (incident,) = incidents(rows, 10)

    long, short = incident.official
    assert (long.instruction, long.headline, long.incomplete) == (
        None,
        None,
        ("headline", "instruction"),
    )
    assert (len(short.instruction or ""), short.incomplete) == (nws.MAX_TEXT - 1, ())
    assert "Residents may return" not in repr(incident)
    (closure,) = [entry for entry in incident.timeline if entry.kind == "closure"]
    assert closure.text == (
        "511 lists an event. Its text is longer than Beacon keeps: read it at 511.org."
    )
    assert "ccc" not in repr(incident)


def test_a_notice_that_names_no_street_is_nobodys_unless_it_is_a_press_release(
    rows: Rows,
) -> None:
    fire(rows)
    order = rows.notice(5, "evacuation_order")
    # An all-clear for somewhere Beacon cannot tell: it names no street at all.
    rows.notice(20, "all_clear", streets=())
    press = rows.notice(30, "press_release", streets=())

    (incident,) = incidents(rows, 40)

    assert [(item.source.evidence_id, item.state) for item in incident.official] == [
        (order.id, "published"),
        (press.id, "published"),
    ]
    assert [signal.kind for signal in incident.signals] == ["official_notice"]


def test_notices_are_not_said_to_have_come_one_after_another(rows: Rows) -> None:
    fire(rows)
    # An order, an all-clear, and a second order, the last two at the same minute:
    # which came into effect when, and which holds, is in the documents.
    first = rows.notice(5, "evacuation_order")
    clear = rows.notice(20, "all_clear")
    second = rows.notice(20, "evacuation_order")

    (incident,) = incidents(rows, 30)

    (signal,) = incident.signals
    assert signal.text == (
        "2 evacuation orders were published, each by an authority. An all-clear was "
        "also published by an authority. Beacon does not work out what an all-clear "
        "covers, or which notice holds: the documents say. This is Beacon's reading "
        "of the document. Read the document for what it says."
    )
    for word in ("after", "before", "later", "then", "lift"):
        assert word not in signal.text
    assert [source.evidence_id for source in signal.sources] == [first.id, second.id, clear.id]


def test_a_warning_is_named_as_a_warning_and_never_as_an_order(rows: Rows) -> None:
    fire(rows)
    warning = rows.notice(5, "evacuation_warning")
    (signal,) = incidents(rows, 10)[0].signals
    assert signal.text == (
        "An evacuation warning was published by an authority. This is Beacon's "
        "reading of the document. Read the document for what it says."
    )
    assert "order" not in signal.text
    # What an app branches on does not call it an order either.
    assert signal.kind == "official_notice"

    rows.notice(6, "shelter_in_place")
    (signal,) = incidents(rows, 10)[0].signals
    assert signal.text.startswith(
        "An evacuation warning and a shelter-in-place order were published, each by "
        "an authority. This is"
    )
    assert signal.sources[0].evidence_id == warning.id


def test_crews_saying_all_clear_does_not_end_an_alert_or_lift_an_order(rows: Rows) -> None:
    fire(rows)
    alert = rows.alert(2, "shelter")
    order = rows.notice(3, "evacuation_order")
    assert incidents(rows, 5)[0].conflicts == ()

    said = rows.radio(10, "fire", "under_control", units=("E36",), details=("all_clear",))
    (incident,) = incidents(rows, 12)

    by_alert, by_order = incident.conflicts
    assert (by_alert.kind, by_order.kind) == ("all_clear_on_radio", "all_clear_on_radio")
    assert by_alert.text == (
        "Crews said all clear on the radio after this official alert was issued. The "
        "alert, whose area holds this place, is in force. An official source outranks "
        "radio traffic: the alert is what holds, and what crews say on the radio does "
        "not end it."
    )
    assert [source.evidence_id for source in by_alert.official] == [alert.id]
    assert [source.evidence_id for source in by_alert.radio] == [said.id]
    assert by_order.text == (
        "Crews said all clear on the radio after an evacuation order that names a "
        "street of this incident was published by an authority. An official source "
        "outranks radio traffic: what crews say on the radio lifts no official notice. "
        "Whether the notice still holds is for its authority to say. This is Beacon's "
        "reading of the document. Read the document for what it says."
    )
    assert [source.url for source in by_order.official] == [order.url]
    # Each is trusted as little as the weakest row it rests on: a call heard clear.
    assert (by_alert.confidence, by_order.confidence) == ("medium", "medium")

    # Once the alert is cancelled, the radio is at odds with the order only.
    rows.alert(20, "gone", message_type="Cancel", replaces=("shelter",), point=None)
    (incident,) = incidents(rows, 25)
    assert [conflict.official[0].evidence_id for conflict in incident.conflicts] == [order.id]


def test_an_all_clear_said_before_the_alert_or_the_order_is_not_at_odds_with_it(
    rows: Rows,
) -> None:
    fire(rows)
    early = rows.radio(1, "fire", "under_control", units=("E36",), details=("all_clear",))
    rows.alert(2, "shelter")
    rows.notice(3, "evacuation_order")

    # Crews who said all clear before either was issued said nothing about them.
    (incident,) = incidents(rows, 5)
    told = {source.evidence_id for entry in incident.timeline for source in entry.sources}
    assert early.id in told
    assert incident.conflicts == ()


def test_an_all_clear_is_set_against_only_what_was_issued_before_it(rows: Rows) -> None:
    fire(rows)
    alert = rows.alert(2, "shelter")
    said = rows.radio(
        2.5, "fire", "under_control", units=("E36",), details=("all_clear",), clarity="partial"
    )
    rows.notice(3, "evacuation_order")

    (conflict,) = incidents(rows, 5)[0].conflicts

    assert [source.evidence_id for source in conflict.official] == [alert.id]
    assert [source.evidence_id for source in conflict.radio] == [said.id]
    # A call that was only partly made out: whether the two disagree is as unsure.
    assert conflict.confidence == "low"


def test_a_notice_about_another_hazard_than_the_radios_is_said_to_outrank_it(rows: Rows) -> None:
    first = rows.radio(0, "other", "dispatched", units=("E36",), street="Hayes St", block=400)
    called = rows.radio(1, "fire", "working", units=("E36",))
    rows.notice(5, "advisory", hazard="smoke")  # smoke goes with a fire
    rows.notice(6, "advisory", hazard="other")  # says nothing
    rows.notice(7, "press_release", hazard="hazmat", streets=())  # keeps no hazard to weigh
    notice = rows.notice(8, "shelter_in_place", hazard="hazmat")

    (incident,) = incidents(rows, 10)

    (conflict,) = incident.conflicts
    assert conflict.kind == "hazard"
    assert conflict.text == (
        "Radio traffic called this a fire. A notice an authority published later, "
        "which names a street of this incident, is about hazardous materials, as "
        "Beacon reads it. Where the two are about the same thing, the official notice "
        "outranks radio traffic. This is Beacon's reading of the document. Read the "
        "document for what it says."
    )
    assert [source.evidence_id for source in conflict.official] == [notice.id]
    # The call that named it a fire, not the first call.
    assert [source.evidence_id for source in conflict.radio] == [called.id]
    assert first.id != called.id
    # A reading at `high` and a call heard clear at `medium`.
    assert conflict.confidence == "medium"


def test_a_notice_is_not_set_against_what_the_radio_said_after_it(rows: Rows) -> None:
    rows.radio(0, "other", "dispatched", units=("E36",), street="Hayes St", block=400)
    rows.notice(1, "shelter_in_place", hazard="hazmat")
    # Crews got there after the notice and called it a fire: the later word is the
    # radio's, and the notice does not outrank what it was written before.
    rows.radio(2, "fire", "working", units=("E36",))

    (incident,) = incidents(rows, 10)

    assert incident.category == "fire"
    assert incident.conflicts == ()


def test_a_signal_carries_its_sources_its_confidence_and_what_its_source_requires(
    rows: Rows,
) -> None:
    fire(rows, alarm=2, details=("evacuation",))
    closure = rows.traffic(2)
    notice = rows.notice(3, "evacuation_order")
    alert = rows.alert(4, "shelter")
    for minute in (5, 6, 7):
        rows.tap(minute)

    (incident,) = incidents(rows, 10)

    by_kind = {signal.kind: signal for signal in incident.signals}
    assert {kind: signal.confidence for kind, signal in by_kind.items()} == {
        "alarm": "medium",
        "evacuation_on_radio": "medium",
        "official_notice": "high",
        "official_alert": "official",
        "closure": "official",
        "taps": "low",
    }
    assert all(signal.sources and signal.text for signal in incident.signals)
    # 511's credit goes with the signal, wherever the signal is shown.
    (source,) = by_kind["closure"].sources
    assert (source.evidence_id, source.credit) == (closure.id, CREDIT_511)
    # The order is a reading, said to be one, with the document's link.
    (source,) = by_kind["official_notice"].sources
    assert (source.evidence_id, source.url, source.model) == (notice.id, notice.url, "fake")
    assert "This is Beacon's reading of the document." in by_kind["official_notice"].text
    assert by_kind["official_alert"].sources[0].evidence_id == alert.id
    # Taps are one source that names none of them.
    (source,) = by_kind["taps"].sources
    assert (source.evidence_id, source.source, source.stand_in) == (None, "app", False)


def test_an_event_that_closes_no_road_says_nothing_of_where_an_incident_is(rows: Rows) -> None:
    fire(rows)
    rows.tap(2)
    slow = rows.traffic(3, closed=False)

    (incident,) = incidents(rows, 5, NO_BLOCKS)

    # The event is on the incident's street, so it is a line of its timeline.
    assert [entry.sources[0].evidence_id for entry in incident.timeline[1:]] == [slow.id]
    assert (incident.point, incident.point_from, incident.taps) == (None, None, 0)
    assert incident.signals == ()


def test_an_incident_the_radio_cleared_takes_nothing_more_from_its_place(rows: Rows) -> None:
    fire(rows)
    rows.tap(2)
    rows.radio(10, "fire", "cleared", units=("E36", "T6"))
    # After the clearance: a tap, a closure, and a helicopter are not this incident's.
    rows.tap(15)
    rows.traffic(20)
    rows.aircraft(22, HAYES, altitude_m=200)
    # A notice still follows: an all-clear and a press release come after.
    order = rows.notice(5, "evacuation_order")
    clear = rows.notice(30, "all_clear")

    (incident,) = incidents(rows, 40)

    assert (incident.status, incident.taps, incident.aircraft) == ("cleared", 1, ())
    assert [entry.kind for entry in incident.timeline] == [
        "radio",
        "taps",
        "notice",
        "radio",
        "notice",
    ]
    assert [(item.source.evidence_id, item.state) for item in incident.official] == [
        (order.id, "published"),
        (clear.id, "published"),
    ]


def test_a_cancellation_of_an_alert_of_the_place_says_so(rows: Rows) -> None:
    fire(rows)
    first = rows.alert(1, "first")
    cancel = rows.alert(8, "gone", message_type="Cancel", replaces=("first",), point=None)

    (incident,) = incidents(rows, 10)

    assert [(item.source.evidence_id, item.state, item.ended_by) for item in incident.official] == [
        (first.id, "cancelled", cancel.id)
    ]
    line = incident.timeline[-1]
    assert line.text == "A later message cancelled an alert that applied here."
    assert line.details["revises_alert_here"] is True


def test_one_low_position_is_one_sighting_and_no_more_is_said_of_it(rows: Rows) -> None:
    fire(rows)
    rows.aircraft(2, HAYES, icao24="once01", altitude_m=250)
    for minute in (4, 5, 6):
        rows.aircraft(minute, HAYES, icao24="more01", altitude_m=250, category=None)

    (incident,) = incidents(rows, 10)

    assert [entry.text for entry in incident.timeline if entry.kind == "aircraft"] == [
        "A helicopter (TEST1) was seen low nearby, once, as low as 250 m.",
        "An aircraft (TEST1) was seen low nearby over about 2 min, as low as 250 m.",
    ]


def test_a_later_message_about_an_alert_is_a_line_and_not_an_alert_for_the_place(
    rows: Rows,
) -> None:
    fire(rows)
    first = rows.alert(1, "first")
    # The update is drawn for somewhere else, and the cancellation for nowhere.
    second = rows.alert(5, "second", message_type="Update", replaces=("first",), point=FAR)
    rows.alert(8, "third", message_type="Cancel", replaces=("second",), point=None)

    (incident,) = incidents(rows, 10)

    assert [(item.source.evidence_id, item.state) for item in incident.official] == [
        (first.id, "replaced")
    ]
    first_line, updated, cancelled = [e for e in incident.timeline if e.kind == "alert"]
    assert (first_line.details["message_type"], first_line.details["state"]) == (
        "Alert",
        "replaced",
    )
    # The later messages are lines that say an alert of this place was updated or
    # cancelled. Nothing of theirs is shown, and they are given no standing: the
    # update is in force somewhere else, which is not here.
    assert updated.text == (
        "A later message updated an alert that applied here. The later message can "
        "be for another area: Beacon does not say that it applies here."
    )
    assert updated.details == {
        "message_type": "Update",
        "revises": ["first"],
        "revises_alert_here": True,
    }
    assert (updated.sources[0].evidence_id, updated.confidence) == (second.id, "official")
    # The cancellation names the update, which was drawn for somewhere else: it is
    # not said to have cancelled an alert that applied here.
    assert cancelled.text == (
        "A later message cancelled a message that had revised an alert that applied "
        "here. Beacon does not say that the message it cancelled applied here."
    )
    assert cancelled.details == {
        "message_type": "Cancel",
        "revises": ["second"],
        "revises_alert_here": False,
    }
    shown = repr(incident)
    for word in ("Headline of second", "Instruction of second", "Headline of third", "in_force"):
        assert word not in shown
    assert incident.signals == ()
