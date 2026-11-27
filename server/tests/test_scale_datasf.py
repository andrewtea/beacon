"""What Beacon keeps of DataSF's dispatch records, and the lists made from DataSF's
streets. The rows are invented, in DataSF's shape."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from beacon.evidence import Point
from beacon.incident.places import read_blocks
from beacon.ingest import SourceError
from beacon.radio.vocabulary import Known, read_names
from beacon.scale import datasf, lists
from tests.scale_day import (
    DAY,
    FELL_GOUGH,
    HAYES_GOUGH,
    ROSTER,
    DataSf,
    call,
    centerlines,
    unit_row,
)

BOX = ("CALL BOX: 301 EDDY ST,SF", -122.41, 37.78)


def test_a_call_is_read_from_the_rows_of_its_units_with_times_in_utc() -> None:
    rows = [
        unit_row("260620001", "T06", "Structure Fire / Smoke in Building", "08:02:10",
                 HAYES_GOUGH, on_scene="08:07:00", available="09:30:00", alarms="2"),
        unit_row("260620001", "E36", "Structure Fire / Smoke in Building", "08:01:30",
                 HAYES_GOUGH, on_scene="08:05:00", available="09:10:00", alarms="2",
                 received_dttm=f"{DAY}T08:00:05.000"),
    ]  # fmt: skip

    (record,), unreadable = datasf.read_records(rows)

    assert unreadable == datasf.Unread()
    assert (record.number, record.kind, record.alarms) == ("260620001", "fire", 2)
    # DataSF's times are San Francisco's wall clock, which was eight hours behind.
    assert record.received_at == datetime(2026, 3, 3, 16, 0, 5, tzinfo=UTC)
    assert record.dispatched_at == datetime(2026, 3, 3, 16, 1, 30, tzinfo=UTC)
    assert record.on_scene_at == datetime(2026, 3, 3, 16, 5, tzinfo=UTC)
    assert record.cleared_at == datetime(2026, 3, 3, 17, 30, tzinfo=UTC)
    # In the order they were dispatched, each in its stored form.
    assert record.units == ("E36", "T6")
    assert (record.streets, record.point) == (
        ("Hayes St", "Gough St"),
        Point(37.7769, -122.4230),
    )


def test_a_medical_call_is_its_crews_its_intersection_and_its_times() -> None:
    rows = call("260620002", "Medical Incident", "09:00:00", FELL_GOUGH, ("M552", "E05"),
                on_scene="09:06:00", alarms="1")  # fmt: skip

    (record,), _ = datasf.read_records(rows)

    assert (record.kind, record.medical, record.alarms) == ("medical", True, None)
    assert record.units == ("M552", "E5")
    # A unit is still out, so the call is not said to be cleared.
    assert record.cleared_at is None
    # Nothing of how the patient was: not in the record, and not in what is kept.
    kept = repr(record) + repr(record.as_data())
    for private in ("Code 3", "Transport", "Life-Threatening", "priority"):
        assert private not in kept


def test_only_the_columns_beacon_keeps_are_asked_for_and_only_the_days() -> None:
    site = DataSf(call("260620002", "Medical Incident", "09:00:00", FELL_GOUGH, ("M552",)))
    http = site.http()

    rows = datasf.fetch_day(http, date.fromisoformat(DAY))

    http.close()
    (request,) = site.requests
    assert request.url.params["$select"].split(",") == list(datasf.COLUMNS)
    assert request.url.params["$where"] == f"call_date = '{DAY}T00:00:00'"
    for private in ("disposition", "priority", "call_type_group", "als_unit", "zipcode"):
        assert private not in request.url.params["$select"]
    assert set(rows[0]) <= set(datasf.COLUMNS)


def test_a_day_is_read_a_page_at_a_time_and_more_than_a_day_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(datasf, "PAGE_ROWS", 2)
    monkeypatch.setattr(datasf, "MAX_PAGES", 3)
    five = [unit_row(f"26062000{n}", "E36", "Alarms", "08:00:00", HAYES_GOUGH) for n in range(5)]
    site = DataSf(five)
    http = site.http()

    assert len(datasf.fetch_day(http, date.fromisoformat(DAY))) == 5
    assert [request.url.params["$offset"] for request in site.requests] == ["0", "2", "4"]

    site.rows = five + five
    with pytest.raises(SourceError) as failure:
        datasf.fetch_day(http, date.fromisoformat(DAY))
    http.close()
    assert str(failure.value) == "datasf lists more rows for one day than Beacon reads."
    assert (failure.value.__context__, failure.value.__cause__) == (None, None)


@pytest.mark.parametrize(
    ("call_type", "kind"),
    [
        ("Medical Incident", "medical"),
        ("Structure Fire / Smoke in Building", "fire"),
        ("Outside Fire", "fire"),
        ("Smoke Investigation (Outside)", "fire"),
        ("Alarms", "alarm"),
        ("Traffic Collision", "collision"),
        ("Gas Leak (Natural and LP Gases)", "utility"),
        ("Water Rescue", "rescue"),
        ("Elevator / Escalator Rescue", "rescue"),
        ("HazMat", "hazmat"),
        ("Citizen Assist / Service Call", "service"),
        ("Other", "other"),
        ("Something DataSF adds later", "other"),
    ],
)
def test_a_call_type_of_datasfs_is_one_of_beacons_categories(call_type: str, kind: str) -> None:
    assert datasf.kind_of(call_type) == kind


def test_what_cannot_be_read_is_counted_and_what_is_not_an_intersection_has_no_streets() -> None:
    rows = [
        unit_row("260620003", "E36", "Alarms", "08:00:00", BOX),
        unit_row("260620004", "SCRT1", "Other", "08:10:00", None),
        {"call_number": "260620005", "unit_id": "E36", "call_type": "Alarms"},
        {"unit_id": "E36", "call_type": "Alarms", "dispatch_dttm": f"{DAY}T08:00:00.000"},
        unit_row("26; DROP", "E36", "Alarms", "08:00:00", HAYES_GOUGH),
        "no row at all",
        7,
    ]  # fmt: skip

    records, unreadable = datasf.read_records(rows)

    # One call with a number and no time. And four rows that are no call's Beacon
    # can tell: no number, a number that is none, and two that are no rows. Rows
    # are counted as rows: nothing says how many calls they are of.
    assert unreadable == datasf.Unread(calls=1, rows=4)
    box, no_place = records
    # A call box's address names a house number, and is not kept as a place.
    assert (box.streets, box.point) == ((), Point(37.78, -122.41))
    # A unit whose id is not a type code and a number is left out.
    assert (no_place.units, no_place.streets, no_place.point) == ((), (), None)


def test_a_field_that_only_looks_like_a_number_or_a_time_is_left_out_and_raises_nothing() -> None:
    # A digit `int` cannot read, and times at the edges of what a date can hold.
    odd = call("260620007", "Alarms", "10:00:00", HAYES_GOUGH, ("E36",), alarms="\u00b2")
    late = call("260620008", "Alarms", "10:00:00", HAYES_GOUGH, ("E36",))
    late[0]["received_dttm"] = "9999-12-31T23:59:59"
    early = call("260620009", "Alarms", "10:00:00", HAYES_GOUGH, ("E36",))
    early[0]["dispatch_dttm"] = "0001-01-01T00:00:00+14:00"
    numbered = call("\u0662\u0666\u0660", "Alarms", "10:00:00", HAYES_GOUGH, ("E36",))

    records, unreadable = datasf.read_records([*odd, *late, *early, *numbered])

    assert [(record.number, record.alarms) for record in records] == [("260620007", None)]
    assert unreadable == datasf.Unread(calls=2, rows=1)

    # An integer of hundreds of digits is a number JSON can hold and a float cannot.
    huge = 10**400
    far = call("260620010", "Alarms", "10:00:00", HAYES_GOUGH, ("E36",))
    far[0]["case_location"] = {"type": "Point", "coordinates": [huge, 37.77]}
    (placed,), _ = datasf.read_records(far)
    assert placed.point is None
    assert lists._number(huge) is None
    line = {"type": "LineString", "coordinates": [[-122.42, huge], [-122.42, 37.77]]}
    assert lists._middle(line) is None


def test_the_records_a_run_keeps_are_read_back_as_they_were() -> None:
    rows = [
        *call("260620001", "Structure Fire / Smoke in Building", "08:01:30", HAYES_GOUGH,
              ("E36", "T06"), on_scene="08:05:00", available="09:10:00", alarms="2"),
        *call("260620002", "Medical Incident", "09:00:00", None, ("M552",)),
    ]  # fmt: skip
    records, _ = datasf.read_records(rows)

    assert datasf.records_from_data([record.as_data() for record in records]) == records
    for broken in ("no list", [{"number": "1"}], [None]):
        with pytest.raises(ValueError, match="records a run kept"):
            datasf.records_from_data(broken)


def test_the_lists_are_a_citys_streets_its_blocks_middles_and_its_units(tmp_path: Path) -> None:
    rows = [
        *centerlines(),
        # A stretch with no house numbers, one with no line, and one with no name
        # Beacon keeps: none gives a block, and the first two still name a street.
        {"streetname": "LINDEN ST", "lf_fadd": "0.0", "lf_toadd": "0.0", "rt_fadd": "0.0",
         "rt_toadd": "0.0", "line": {"type": "LineString",
                                     "coordinates": [[-122.42, 37.77], [-122.43, 37.77]]}},
        {"streetname": "IVY ST", "lf_fadd": "1.0", "lf_toadd": "99.0", "rt_fadd": "0.0",
         "rt_toadd": "98.0"},
        {"streetname": "UNNAMED 042", "lf_fadd": "1.0", "lf_toadd": "99.0", "rt_fadd": "2.0",
         "rt_toadd": "98.0", "line": {"type": "LineString",
                                      "coordinates": [[-122.42, 37.77], [-122.43, 37.77]]}},
    ]  # fmt: skip

    made = lists.lists_of(rows, ROSTER)

    assert {"Hayes St", "Gough St", "Geary Blvd", "17th Ave", "Linden St", "Ivy St"} <= set(
        made.streets
    )
    # Each in its stored form, and what is not shaped as a unit left out.
    assert made.units == ("B2", "E31", "E36", "E5", "M552", "T12", "T6")
    by_block = {(street, block): (lat, lon) for street, block, lat, lon in made.blocks}
    assert set(by_block) == {
        ("Hayes St", 400), ("Hayes St", 500), ("Gough St", 300), ("Fell St", 300),
        ("Octavia St", 300), ("Geary Blvd", 5200), ("17th Ave", 400),
    }  # fmt: skip
    # The middle of the stretch its numbers are on.
    assert by_block[("Hayes St", 400)] == pytest.approx((37.7768, -122.42375), abs=1e-6)

    # Written in the forms the server's settings take.
    made.write(tmp_path / "lists")
    streets = read_names((tmp_path / "lists" / lists.STREETS_FILE).read_text())
    units = read_names((tmp_path / "lists" / lists.UNITS_FILE).read_text())
    known, left_out = Known.kept(streets=streets, units=units)
    blocks, bad_lines = read_blocks((tmp_path / "lists" / lists.BLOCKS_FILE).read_text())
    assert (left_out, bad_lines) == (0, 0)
    assert (known.streets, known.units) == (frozenset(made.streets), frozenset(made.units))
    assert blocks.point("Hayes St", 400) == Point(37.7768, -122.42375)


def test_the_roster_is_read_from_the_days_before_the_day_that_is_scored() -> None:
    site = DataSf([])
    http = site.http()

    assert lists.fetch_roster(http, date.fromisoformat(DAY)) == list(ROSTER)
    assert len(lists.fetch_centerlines(http)) == len(centerlines())

    http.close()
    roster, streets = site.requests
    # Thirty days, up to the day and without it: the roster is not read off the answers.
    assert roster.url.params["$where"] == (
        "call_date >= '2026-02-01T00:00:00' AND call_date < '2026-03-03T00:00:00'"
    )
    assert streets.url.params["$where"] == "active = true"
