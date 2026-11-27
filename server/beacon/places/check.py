"""Does this affect me? What Beacon has near a place a person saved.

The answer is a distance and no more. Code measures how far the point Beacon has
for each open incident (its block's, or a 511 closure's) is from the saved place,
and says that, with how roughly both are known. It does not say that a place is
inside or outside the area of an order or an alert: a notice's row keeps streets and
no area, and the saved place is known only to about a block. So every answer carries
`NOT_AN_AREA`, and what an authority published is shown as published, with its link,
by the incident's own page.

What is not listed, and `NOTHING_NEAR` says so: an incident Beacon has no point
for, because nothing says how far it is; an incident whose record is closed; and a
medical response, which is its crews and its block, and of which nothing is made.
`NOTHING_NEAR` is the same sentence whichever of those there are, or none: it says
what is listed, and not whether there is a medical response near.

`Admission` is how many checks the route works on at once.
"""

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from beacon.evidence import Point
from beacon.incident import Incident
from beacon.radio.vocabulary import MEDICAL_EVENT

# How far from a saved place an incident is listed.
REACH_M = 1_000.0
# The most places one request may hold.
MAX_PLACES = 5
# A distance is said to the nearest of these: both ends are known to about a block.
_ROUND_M = 50
# The most checks worked on at once: read from the network, or answered.
MAX_CHECKS_AT_ONCE = 8

# Says what the reading lists and no more: not that there is no incident, which a
# medical response at the place itself would make false, and a reading that did not
# take in every row could not know.
NOTHING_NEAR = (
    "Beacon lists no incident within a kilometre of this saved place in this reading. "
    "That is not an official all-clear. A medical response, an incident whose record "
    "in Beacon is closed, and one Beacon has no point for are not listed here."
)
NOT_AN_AREA = (
    "A distance here is between the point Beacon has for an incident, which is its "
    "block's or a road closure's, and this place as it was saved, rounded to about a "
    "block. It is rough. A distance does not tell you whether this place is inside or "
    "outside the area of an official order or alert. What an authority published says "
    "which area it covers."
)
# Said with a distance that is measured from a point of 511's. "Listed", not
# "lists": the point is from the version of a 511 event that had a road of the
# incident's closed, and a later version can have it open again. The point is
# still where the incident is taken to be; whether 511 lists a closure now is the
# timeline's to say.
_FROM_511 = ", going by where 511 listed a road closure on its street"


@dataclass(frozen=True)
class Near:
    """An open incident that Beacon has within `REACH_M` of a place."""

    incident: Incident
    # Rounded to 50 m. Zero is anything nearer than 25 m.
    distance_m: int

    @property
    def text(self) -> str:
        """How far it is, in a sentence, and that the distance is measured from a
        point of 511's when it is. Only that: not whether the place is in anybody's
        area, and not what is happening there."""
        by = _FROM_511 if self.incident.point_from == "closure" else ""
        if self.distance_m <= 0:
            return f"Beacon has this incident about where this place is{by}."
        return f"Beacon has this incident about {self.distance_m} m from this place{by}."


def near(point: Point, incidents: Sequence[Incident]) -> tuple[Near, ...]:
    """The open incidents Beacon has within `REACH_M` of `point`, nearest first.
    One with no known place is not listed, because nothing says how far it is, and
    neither is a medical response."""
    found: list[tuple[float, Near]] = []
    for incident in incidents:
        if not incident.open or incident.point is None or incident.category == MEDICAL_EVENT:
            continue
        distance = incident.point.distance_m(point)
        if distance <= REACH_M:
            rounded = round(distance / _ROUND_M) * _ROUND_M
            found.append((distance, Near(incident, rounded)))
    return tuple(item for _distance, item in sorted(found, key=lambda pair: pair[0]))


class Admission:
    """The places among the checks being worked on at once. A check takes one before
    its body is read and gives it back when its answer is made, so that only so many
    bodies are in memory, and only so many answers are waited for, at once. Nobody
    waits for a place: a check that finds none is refused."""

    def __init__(self, most: int = MAX_CHECKS_AT_ONCE) -> None:
        self._most = most
        self._held = 0
        self._counting = threading.Lock()

    @property
    def held(self) -> int:
        with self._counting:
            return self._held

    def admit(self) -> Callable[[], None] | None:
        """Take a place, and return what gives it back, once: calling it again does
        nothing. None when every place is held."""
        with self._counting:
            if self._held >= self._most:
                return None
            self._held += 1
        held = [True]

        def give_back() -> None:
            with self._counting:
                if held[0]:
                    held[0] = False
                    self._held -= 1

        return give_back
