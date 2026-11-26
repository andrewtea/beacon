"""The area Beacon covers, and reading positions out of GeoJSON."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from beacon.evidence import Point


@dataclass(frozen=True)
class Bounds:
    """A box of latitudes and longitudes."""

    south: float
    west: float
    north: float
    east: float

    def holds(self, point: Point) -> bool:
        return self.south <= point.lat <= self.north and self.west <= point.lon <= self.east


# San Francisco with Treasure Island, and a margin over the water on each side.
SAN_FRANCISCO = Bounds(south=37.70, west=-122.53, north=37.84, east=-122.35)


def positions(geometry: Any) -> Iterator[Point]:
    """Every position of a GeoJSON geometry, in the order it gives them. A position
    is longitude first. Anything that is not a position is passed over."""
    if not isinstance(geometry, dict):
        return
    if geometry.get("type") == "GeometryCollection":
        for inner in geometry.get("geometries") or ():
            yield from positions(inner)
        return
    yield from _walk(geometry.get("coordinates"))


def centre(geometry: Any) -> Point | None:
    """The middle of a geometry's box, which is near enough for a block or a few.
    None for a geometry with no positions."""
    found = list(positions(geometry))
    if not found:
        return None
    lats = [point.lat for point in found]
    lons = [point.lon for point in found]
    return Point((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)


def _walk(coordinates: Any) -> Iterator[Point]:
    if not isinstance(coordinates, list):
        return
    if len(coordinates) >= 2 and all(_is_number(value) for value in coordinates[:2]):
        try:
            yield Point(lat=coordinates[1], lon=coordinates[0])
        except ValueError:
            return
        return
    for inner in coordinates:
        yield from _walk(inner)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
