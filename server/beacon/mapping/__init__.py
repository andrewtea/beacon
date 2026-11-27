"""The map of one incident: what Beacon can place, and where.

    from beacon.mapping import drawn

    features = drawn(incident, engine.store.get)

A map is opened from an incident's card and shows that incident and nothing else:
it is no map of the city. Everything on it is a `Feature`, which is something a
source said, with the sentence for it, how far it is trusted, the rows it rests
on, and where it is. `features` says what is drawn and how each is worded.
"""

from beacon.mapping.features import (
    MAX_PATH,
    MAX_RING_POINTS,
    NO_MAP_OF_MEDICAL,
    NO_POINT,
    Feature,
    Position,
    drawn,
)

__all__ = [
    "MAX_PATH",
    "MAX_RING_POINTS",
    "NO_MAP_OF_MEDICAL",
    "NO_POINT",
    "Feature",
    "Position",
    "drawn",
]
