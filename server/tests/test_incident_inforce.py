"""What is in force among alerts, closures, and notices."""

from pathlib import Path

import pytest

from beacon.incident import lifted, standings
from tests.incident_rows import Rows, at, mock_store


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


def states(rows: Rows, minute: float) -> dict[str, tuple[str, str | None]]:
    found = standings(rows.store.search(), at(minute))
    by_id = {row.id: row for row in rows.store.search()}
    return {
        by_id[row_id].ref: (standing.state, None if standing.by is None else standing.by.ref)
        for row_id, standing in found.items()
    }


def test_an_alert_is_ended_by_the_message_that_names_it_and_not_before(rows: Rows) -> None:
    rows.alert(0, "first")
    rows.alert(40, "second", message_type="Update", replaces=("first",))
    rows.alert(60, "third", message_type="Cancel", replaces=("second",))
    rows.alert(0, "other")

    assert states(rows, 30) == {"first": ("in_force", None), "other": ("in_force", None)}
    assert states(rows, 50)["first"] == ("replaced", "second")
    assert states(rows, 50)["second"] == ("in_force", None)
    assert states(rows, 70) == {
        "first": ("replaced", "second"),
        "second": ("cancelled", "third"),
        # A cancellation ends others, and is itself never in force.
        "third": ("cancellation", None),
        "other": ("in_force", None),
    }
    # An alert that ran to the time it gave has ended, and nobody ended it.
    assert states(rows, 121)["other"] == ("ended", None)


def test_the_latest_change_row_about_a_message_is_the_one_that_holds(rows: Rows) -> None:
    alert = rows.alert(0, "dropped")
    rows.change(10, alert, "not_listed")
    assert states(rows, 15) == {
        "dropped": ("not_listed", f"dropped#not_listed@{at(10).isoformat()}")
    }

    rows.change(20, alert, "listed_again")
    assert states(rows, 25) == {"dropped": ("in_force", None)}

    rows.change(30, alert, "not_listed")
    assert states(rows, 35)["dropped"][0] == "not_listed"


def test_only_the_latest_version_of_a_traffic_event_can_be_in_force(rows: Rows) -> None:
    first = rows.traffic(9)
    second = rows.traffic(20, created=9)
    other = rows.traffic(5, "511.org/2", road="Lincoln Way")

    assert states(rows, 30) == {
        first.ref: ("replaced", second.ref),
        second.ref: ("in_force", None),
        other.ref: ("in_force", None),
    }
    rows.change(82, second)
    assert states(rows, 90)[second.ref][0] == "not_listed"
    # A change is about the version it names, and the other event is untouched.
    assert states(rows, 90)[other.ref] == ("in_force", None)


def test_an_all_clear_lifts_the_earlier_orders_of_its_hazard_and_nothing_else(rows: Rows) -> None:
    order = rows.notice(19, "evacuation_order")
    smoke = rows.notice(20, "shelter_in_place", hazard="smoke")
    closure = rows.notice(21, "road_closure")
    clear = rows.notice(74, "all_clear")
    later = rows.notice(80, "evacuation_order")

    found = lifted([order, smoke, closure, clear, later])

    assert {row_id: by.id for row_id, by in found.items()} == {order.id: clear.id}
