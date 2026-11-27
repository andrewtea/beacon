"""Saved places: what Beacon has near a place a person saved.

    from beacon.places import near, read_places

    points = read_places(sent)            # each rounded to about a block
    found = near(points[0], snapshot.incidents)

A place a person saves stays on their device, with the name they gave it. The app
sends a place here only to ask what is near it, rounded to about a block and with
no name, and **nothing of it is kept**: no row, no file, no log line (the route is
one of `PRIVATE_PATHS`). So there is nothing here to open or close.

`check` says what is near a place and how it is worded, and how many checks are
worked on at once (`Admission`); `read` reads the places a request holds.
"""

from beacon.places.check import (
    MAX_CHECKS_AT_ONCE,
    MAX_PLACES,
    NOT_AN_AREA,
    NOTHING_NEAR,
    REACH_M,
    Admission,
    Near,
    near,
)
from beacon.places.read import MAX_BODY_BYTES, SHAPE, PlacesError, parse, read_places

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_CHECKS_AT_ONCE",
    "MAX_PLACES",
    "NOTHING_NEAR",
    "NOT_AN_AREA",
    "REACH_M",
    "SHAPE",
    "Admission",
    "Near",
    "PlacesError",
    "near",
    "parse",
    "read_places",
]
