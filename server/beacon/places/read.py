"""Reading the places a request holds.

A place is `{"lat": ..., "lon": ...}` and nothing else: no name, no accuracy. It is
rounded to three decimals of a degree, about a block, before anything is done with
it, as a tap's place is, whatever the app sent. An error about one has a fixed text
and repeats nothing: a saved place can be a home.
"""

import contextlib
import json
import math
from collections.abc import Mapping
from typing import Any

from beacon.errors import raise_bare
from beacon.evidence import Point
from beacon.ingest.area import SAN_FRANCISCO, Bounds
from beacon.places.check import MAX_PLACES

# The decimal places of a degree a place is rounded to: the same as a tap's.
DECIMALS = 3
# The most a request about places may hold. Five places are well under it.
MAX_BODY_BYTES = 2_048
# The most of each mark of JSON's structure such a body may hold: it is refused
# before it is parsed when it has more.
_MOST_MARKS = (
    (b"{", MAX_PLACES + 1),
    (b"[", 1),
    (b",", 2 * MAX_PLACES),
    (b":", 2 * MAX_PLACES + 1),
)
_FIELDS = frozenset({"lat", "lon"})

SHAPE = (
    "A request about places is a JSON object with places: a list of one to five "
    "places, each with lat and lon in degrees and nothing else."
)
ELSEWHERE = "Beacon covers San Francisco, and one of these places is somewhere else."


class PlacesError(Exception):
    """A request about places cannot be used. `status` is the HTTP status to answer
    with, and the text is fixed: it repeats nothing that was sent."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse(body: bytes) -> Mapping[str, Any]:
    """The JSON object of a request's body. Raises PlacesError for a body with more
    structure than such a request has, which is not parsed, and for one that is not
    a JSON object in UTF-8."""
    found: Any = None
    if not any(body.count(mark) > most for mark, most in _MOST_MARKS):
        # A decoder's error quotes what it could not read. It is handled and gone
        # before anything is raised.
        with contextlib.suppress(ValueError, RecursionError):
            found = json.loads(body.decode("utf-8"))
    if not isinstance(found, Mapping):
        raise_bare(PlacesError(422, SHAPE))
    return found


def read_places(sent: Any, bounds: Bounds = SAN_FRANCISCO) -> tuple[Point, ...]:
    """The places of a request, in the order they were sent, each rounded to about
    a block. Raises PlacesError for what is not one to `MAX_PLACES` places, and
    for a place outside the area Beacon covers."""
    points: list[Point] = []
    if isinstance(sent, list) and 1 <= len(sent) <= MAX_PLACES:
        for place in sent:
            point = _point(place)
            if point is None:
                points = []
                break
            points.append(point)
    if not points:
        raise_bare(PlacesError(422, SHAPE))
    if not all(bounds.holds(point) for point in points):
        raise_bare(PlacesError(422, ELSEWHERE))
    return tuple(points)


def _point(place: Any) -> Point | None:
    if not (isinstance(place, Mapping) and place.keys() == _FIELDS):
        return None
    lat, lon = place["lat"], place["lon"]
    if not (_is_number(lat) and _is_number(lon)):
        return None
    try:
        return Point(round(float(lat), DECIMALS), round(float(lon), DECIMALS))
    except ValueError:
        # Not a latitude or a longitude. The error names the number, and is gone
        # before anything is raised.
        return None


def _is_number(value: Any) -> bool:
    """Whether this is a number a place can be, as a tap's is read: an integer of
    any size is valid JSON, and one too large to be a float is not one of these."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -1_000_000_000 <= value <= 1_000_000_000
    return isinstance(value, float) and math.isfinite(value)
