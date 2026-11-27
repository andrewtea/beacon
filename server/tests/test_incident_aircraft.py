"""Aircraft tracks, and whether one circles."""

import math
from pathlib import Path
from typing import Any

import pytest

from beacon.evidence import Point
from beacon.incident import tracks
from tests.incident_rows import HAYES, Rows, mock_store


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


def offset(point: Point, north_m: float, east_m: float) -> Point:
    return Point(
        point.lat + north_m / 111_320.0,
        point.lon + east_m / (111_320.0 * math.cos(math.radians(point.lat))),
    )


def circle(
    rows: Rows,
    middle: Point,
    radius_m: float,
    *,
    minutes: float,
    icao24: str,
    category: str | None = "rotorcraft",
    **fields: Any,
) -> None:
    steps = int(minutes * 2)
    for step in range(steps + 1):
        # A turn every 108 seconds, seen every 30: a few points of each turn.
        angle = math.tau * step * 30 / 108
        point = offset(middle, radius_m * math.cos(angle), radius_m * math.sin(angle))
        rows.aircraft(step / 2, point, icao24=icao24, category=category, **fields)


def test_an_aircraft_that_goes_round_is_circling_and_one_that_passes_is_not(rows: Rows) -> None:
    circle(rows, HAYES, 450, minutes=6, icao24="round1")
    # Straight over the place, west to east, in the same six minutes.
    for step in range(13):
        rows.aircraft(step / 2, offset(HAYES, 50, -900 + 150 * step), icao24="pass01")
    # Hanging over one spot.
    for step in range(13):
        rows.aircraft(step / 2, offset(HAYES, 200 + step, 200), icao24="hover1", altitude_m=900)

    found = {track.icao24: track for track in tracks(rows.store.search(), HAYES)}

    assert {name: track.circling for name, track in found.items()} == {
        "round1": True,
        "pass01": False,
        "hover1": False,
    }
    assert found["round1"].nearest_m == pytest.approx(450, abs=5)
    assert (found["round1"].low, found["hover1"].low) == (True, False)
    assert found["pass01"].nearest_m == pytest.approx(50, abs=5)


def test_an_aircraft_circling_something_beside_the_place_is_still_seen_to_circle(
    rows: Rows,
) -> None:
    circle(rows, offset(HAYES, 300, 300), 300, minutes=6, icao24="beside")

    (track,) = tracks(rows.store.search(), HAYES)

    assert track.circling


def test_one_turn_is_too_short_to_call_circling_and_a_gap_makes_two_visits(rows: Rows) -> None:
    circle(rows, HAYES, 450, minutes=2.5, icao24="short1")
    for minute in (0, 1, 20, 21):
        rows.aircraft(minute, offset(HAYES, 100, 100), icao24="twice1")
    # Too far away to be over the place at all.
    rows.aircraft(0, offset(HAYES, 5000, 0), icao24="far001")

    found = tracks(rows.store.search(), HAYES)

    assert [(track.icao24, track.positions, track.circling) for track in found] == [
        ("short1", 6, False),
        ("twice1", 2, False),
        ("twice1", 2, False),
    ]
