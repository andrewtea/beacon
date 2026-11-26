"""Aircraft over a place: positions put together into tracks, and whether one circles.

OpenSky gives one row for each position of each aircraft, about every thirty seconds.
A `Track` is one aircraft's positions near a place over a span of time, and what
plain arithmetic says about it: how long it stayed, how near and how low it came, and
whether it went round. Deciding that an aircraft is circling is done here, in code
that is tested, and by no model.

An aircraft is **circling** when it stayed near the place for `CIRCLING_MINUTES` and
went round: seen from the middle of its own positions, it was in at least six of the
eight parts of the compass, at a distance that is a circle and not a hover
(`_MIN_RADIUS_M`). Six and not all eight, because a position every thirty seconds
catches a turn of a minute or two at only a few points. One that passed straight
over was on two sides of that middle. The middle is the track's own
and not the place, so an aircraft that circles something beside the place is still
seen to circle.

What this cannot say: whose aircraft it is. A row has a callsign and OpenSky's
category (`rotorcraft`), and Beacon has no list of fire, police, or news aircraft.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from beacon.evidence import Evidence, Point

# How near a position has to be to count as over a place.
NEAR_M = 1_000.0
# How long an aircraft has to stay near a place before it can be said to circle.
CIRCLING_MINUTES = 3.0
# Below this an aircraft is low enough to be noticed from the street as low.
LOW_M = 500.0
# Positions of one aircraft further apart than this are two visits.
_GAP = timedelta(minutes=5)
_SECTORS = 8
_MIN_SECTORS = 6
# Positions this near their own middle, on average, are a hover and not a circle.
_MIN_RADIUS_M = 100.0


@dataclass(frozen=True)
class Track:
    """One aircraft's stay near a place."""

    icao24: str
    callsign: str | None
    # OpenSky's category, such as `rotorcraft`, or None.
    category: str | None
    first_seen: datetime
    last_seen: datetime
    positions: int
    # The nearest it came to the place, and its lowest and highest, in metres.
    nearest_m: float
    lowest_m: float | None
    highest_m: float | None
    circling: bool
    # Whether it came nearer than `NEAR_M` while lower than `LOW_M`.
    low: bool
    # The rows it is made of, oldest first.
    rows: tuple[Evidence, ...]

    @property
    def minutes(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds() / 60


def tracks(rows: Iterable[Evidence], place: Point, *, near_m: float = NEAR_M) -> list[Track]:
    """The tracks the aircraft rows among `rows` make near `place`, in the order
    they began. Only positions within `near_m` of the place are counted."""
    near: dict[str, list[Evidence]] = {}
    for row in rows:
        address = row.data.get("icao24")
        if row.kind != "aircraft" or row.point is None or not isinstance(address, str):
            continue
        if row.point.distance_m(place) <= near_m:
            near.setdefault(address, []).append(row)
    found: list[Track] = []
    for address, positions in near.items():
        positions.sort(key=lambda row: (row.occurred_at, row.id))
        visit: list[Evidence] = []
        for row in positions:
            if visit and row.occurred_at - visit[-1].occurred_at > _GAP:
                found.append(_track(address, visit, place))
                visit = []
            visit.append(row)
        found.append(_track(address, visit, place))
    return sorted(found, key=lambda track: (track.first_seen, track.icao24))


def _track(address: str, rows: list[Evidence], place: Point) -> Track:
    heights = [
        height
        for row in rows
        if isinstance(height := row.data.get("altitude_m"), int | float)
        and not isinstance(height, bool)
    ]
    distances = [row.point.distance_m(place) for row in rows if row.point is not None]
    minutes = (rows[-1].occurred_at - rows[0].occurred_at).total_seconds() / 60
    points = [row.point for row in rows if row.point is not None]
    middle = Point(
        sum(point.lat for point in points) / len(points),
        sum(point.lon for point in points) / len(points),
    )
    sides = {_sector(middle, point) for point in points}
    radius = sum(point.distance_m(middle) for point in points) / len(points)
    callsign = next(
        (name for row in rows if isinstance(name := row.data.get("callsign"), str) and name), None
    )
    category = next(
        (kind for row in rows if isinstance(kind := row.data.get("category"), str) and kind), None
    )
    return Track(
        icao24=address,
        callsign=callsign,
        category=category,
        first_seen=rows[0].occurred_at,
        last_seen=rows[-1].occurred_at,
        positions=len(rows),
        nearest_m=min(distances),
        lowest_m=min(heights) if heights else None,
        highest_m=max(heights) if heights else None,
        # On nearly every side of its own middle: it went round.
        circling=minutes >= CIRCLING_MINUTES
        and len(sides) >= _MIN_SECTORS
        and radius >= _MIN_RADIUS_M,
        low=bool(heights) and min(heights) < LOW_M,
        rows=tuple(rows),
    )


def _sector(middle: Point, position: Point) -> int:
    """Which eighth of the compass around `middle` a position is in."""
    north = position.lat - middle.lat
    east = (position.lon - middle.lon) * math.cos(math.radians(middle.lat))
    return int((math.degrees(math.atan2(east, north)) % 360) // (360 / _SECTORS)) % _SECTORS
