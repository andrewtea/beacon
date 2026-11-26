"""Which radio calls are one incident."""

from pathlib import Path

import pytest

from beacon.incident import group_calls
from tests.incident_rows import Rows, mock_store


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


def groups(rows: Rows):  # type: ignore[no-untyped-def]
    return group_calls(rows.store.search())


def test_a_call_that_names_only_units_joins_the_incident_those_units_are_on(rows: Rows) -> None:
    rows.radio(0, "fire", "dispatched", units=("E36", "T6"), street="Hayes St", block=400)
    rows.radio(3, "fire", "on_scene", units=("E36",))
    rows.radio(4, "alarm", "dispatched", units=("E22",), street="Irving St", block=1900)
    rows.radio(5, "alarm", "on_scene", units=("E22",))

    fire, alarm = groups(rows)

    assert (fire.street, fire.block, fire.status, len(fire.rows)) == (
        "Hayes St",
        400,
        "on_scene",
        2,
    )
    assert (alarm.street, alarm.category, alarm.status, len(alarm.rows)) == (
        "Irving St",
        "alarm",
        "on_scene",
        2,
    )


def test_a_call_at_another_place_is_another_incident_whatever_units_it_shares(rows: Rows) -> None:
    rows.radio(0, "fire", "working", units=("E36", "T6"), street="Hayes St", block=400)
    # E36 is sent on to a second call. It is not the fire, and takes E36 with it.
    rows.radio(10, "alarm", "dispatched", units=("E36",), street="Fell St", block=400)
    rows.radio(12, "alarm", "on_scene", units=("E36",))
    rows.radio(13, "fire", "under_control", units=("T6",))

    fire, alarm = groups(rows)

    assert ([row.data["status"] for row in fire.rows]) == ["working", "under_control"]
    assert ([row.data["status"] for row in alarm.rows]) == ["dispatched", "on_scene"]
    assert (fire.street, alarm.street) == ("Hayes St", "Fell St")


def test_a_medical_call_at_a_fires_block_with_a_fires_unit_is_not_the_fire(rows: Rows) -> None:
    rows.radio(0, "fire", "working", units=("E36", "E21"), street="Hayes St", block=400, alarm=2)
    rows.radio(5, "medical", units=("M52", "E21"), street="Hayes St", block=400)
    rows.radio(6, "fire", "working", units=("E21",), details=("fire_showing",))
    rows.radio(7, "medical", units=("M52",))

    fire, medical = groups(rows)

    assert (fire.medical, medical.medical) == (False, True)
    assert [row.data["category"] for row in fire.rows] == ["fire", "fire"]
    assert [row.data["category"] for row in medical.rows] == ["medical", "medical"]
    # Nothing of the fire is the medical response's: no alarm, no status.
    assert (medical.alarm, medical.status, medical.category) == (None, "unknown", "medical")


def test_an_incident_takes_its_place_from_the_first_call_of_its_units_that_gives_one(
    rows: Rows,
) -> None:
    rows.radio(0, "fire", "en_route", units=("E36",))
    rows.radio(2, "fire", "on_scene", units=("E36",), street="Hayes St", block=400)

    (fire,) = groups(rows)

    assert (fire.street, fire.block, len(fire.rows)) == ("Hayes St", 400, 2)


def test_calls_that_say_nothing_open_no_incident(rows: Rows) -> None:
    rows.radio(0, "unintelligible", clarity="unintelligible")
    rows.radio(1, "administrative", units=("E36",))
    rows.radio(2, "fire", "working")
    # Units cleared from something nobody heard begin.
    rows.radio(3, "fire", "cleared", units=("E9",))

    assert groups(rows) == []


def test_an_incident_that_went_quiet_takes_no_more_calls(rows: Rows) -> None:
    rows.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    rows.radio(44, "fire", "working", units=("E36",))
    rows.radio(90, "fire", "dispatched", units=("E5",), street="Hayes St", block=400)

    first, second = groups(rows)

    assert (len(first.rows), len(second.rows)) == (2, 1)


def test_a_new_dispatch_to_a_block_whose_incident_was_cleared_is_a_new_incident(rows: Rows) -> None:
    rows.radio(0, "alarm", "dispatched", units=("E36",), street="Hayes St", block=400)
    rows.radio(5, "alarm", "cleared", units=("E36",))
    rows.radio(15, "fire", "dispatched", units=("E36",), street="Hayes St", block=400)
    # A report from the same block that is no new dispatch is about the latest one.
    rows.radio(16, "fire", "working", units=("T6",), street="Hayes St", block=400)

    alarm, fire = groups(rows)

    assert (alarm.status, alarm.over, len(alarm.rows)) == ("cleared", True, 2)
    assert (fire.category, fire.status, len(fire.rows)) == ("fire", "working", 2)


def test_clearing_some_units_does_not_clear_an_incident_others_are_still_on(rows: Rows) -> None:
    rows.radio(0, "fire", "working", units=("E36", "T6"), street="Hayes St", block=400, alarm=2)
    rows.radio(10, "fire", "under_control", units=("E36",))
    # A later report of an earlier stage does not take the incident back.
    rows.radio(11, "fire", "on_scene", units=("T6",))
    (fire,) = groups(rows)
    assert fire.status == "under_control"

    rows.radio(20, "fire", "cleared", units=("T6",))
    (fire,) = groups(rows)
    assert (fire.status, fire.over, fire.active) == ("clearing", False, {"E36"})

    rows.radio(25, "fire", "cleared", units=("E36",))
    (fire,) = groups(rows)
    assert (fire.status, fire.over, fire.alarm) == ("cleared", True, 2)


def test_a_call_stored_late_is_put_where_its_time_puts_it(rows: Rows) -> None:
    rows.radio(5, "fire", "on_scene", units=("E36",))
    rows.radio(0, "fire", "dispatched", units=("E36",), street="Hayes St", block=400)

    (fire,) = groups(rows)

    assert [row.data["status"] for row in fire.rows] == ["dispatched", "on_scene"]
    assert fire.status == "on_scene"
