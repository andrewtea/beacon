"""Does this affect me? What Beacon has near a place a person saved.

The answer is a distance and no more. Code measures how far the point Beacon has
for each open incident (its block's, or a 511 closure's) is from the saved place,
and says that, with how roughly both are known. It does not say that a place is
inside or outside the area of an order or an alert: a notice's row keeps streets and
no area, and the saved place is known only to about a block. So every answer carries
`NOT_AN_AREA`, and what an authority published is shown as published, with its link,
by the incident's own page.

What is not listed, and `NOTHING_NEAR` says so: an incident Beacon has no place
for, because nothing says how far it is; an incident whose record is closed; and a
medical response, which is its crews and its block, and of which nothing is made.
"""

from collections.abc import Sequence
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

NOTHING_NEAR = (
    "Beacon's record has no incident that is open in it, with a known place, within "
    "a kilometre of this place. That is what the record has now, not an official "
    "all-clear: an incident whose record is closed or that Beacon has no place for "
    "is not listed, and neither is a medical response."
)
NOT_AN_AREA = (
    "A distance here is between the point Beacon has for an incident, which is its "
    "block's or a road closure's, and this place as it was saved, rounded to about a "
    "block. It is rough. Being near an incident is not being inside the area of an "
    "official order or alert, and being farther from one is not being outside it. "
    "Read what the authority published for the area it covers."
)


@dataclass(frozen=True)
class Near:
    """An open incident that Beacon has within `REACH_M` of a place."""

    incident: Incident
    # Rounded to 50 m. Zero is anything nearer than 25 m.
    distance_m: int

    @property
    def text(self) -> str:
        """How far it is, in a sentence. Only that: not whether the place is in
        anybody's area, and not what is happening there."""
        if self.distance_m <= 0:
            return "Beacon has this incident about where this place is."
        return f"Beacon has this incident about {self.distance_m} m from this place."


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
