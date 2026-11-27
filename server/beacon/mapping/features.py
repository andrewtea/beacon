"""What is drawn on the map of one incident.

Four kinds of thing, each of which the incident already has as a line of its
timeline or as its place. The map adds where each is, and no claim of its own:

- `place`: the point Beacon has for the incident. It is the point of the hundred
  block named on the radio, from the list of blocks, or else where 511 listed a
  closure on the incident's street. The sentence says which, and that it marks a
  block and no address.
- `traffic_event`: a 511 event of the incident's, as its version in force has it,
  in 511's own headline. Only the version in force is drawn: an earlier one can be
  about a road that is open again.
- `aircraft`: an aircraft the incident's timeline has, with the positions it was
  seen at near the place.
- `alert_area`: the area an authority drew for an alert that is in force for the
  place. An alert that is not known to be in force is not drawn, and neither is one
  that was issued for a whole zone, which has no drawn area.

Nothing of a tap is drawn: a tap's point is its sender's. A notice has no place and
is not drawn. A medical response has no map (`NO_MAP_OF_MEDICAL`).
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from beacon.evidence import Confidence, Evidence, Point
from beacon.incident import Entry, Incident, Source, weakest
from beacon.radio.vocabulary import MEDICAL_EVENT

# The most positions of one aircraft that are drawn: the newest ones.
MAX_PATH = 240
# The most points the drawn area of one alert may have. An area with more is not
# drawn, and its feature says so: an area that is thinned can leave a street out.
MAX_RING_POINTS = 600

FeatureKind = Literal["place", "traffic_event", "aircraft", "alert_area"]

NO_MAP_OF_MEDICAL = "Beacon draws no map of a medical response."
# The rule, and not a finding about this incident: whether the radio named a block
# at all is not looked at here.
NO_POINT = (
    "Beacon has no point for this incident, so it draws no map of it. A point is "
    "that of the block named on the radio, when the block is on Beacon's list, or "
    "else where 511 listed a road closure on the incident's street."
)
_PLACE_FROM_BLOCKS = (
    "The point Beacon has for the block named on the radio. It marks the block, not an address."
)
_PLACE_FROM_511 = (
    "Where 511 listed a road closure on this incident's street. Beacon has no point "
    "for the block named on the radio, so this is where the incident is drawn. It "
    "marks no address."
)
_PATH_CUT = f"Only its newest {MAX_PATH} positions are drawn."
_AREA_TOO_LARGE = (
    "Its area has more points than Beacon draws, so it is not drawn. The alert says "
    "which area it covers."
)


@dataclass(frozen=True)
class Position:
    """Where an aircraft was seen, and when."""

    point: Point
    at: datetime


@dataclass(frozen=True)
class Feature:
    """One thing on the map: what a source said, and where it is."""

    kind: FeatureKind
    # The sentence for it, written by code. For a traffic event, an aircraft, and
    # an alert it is the line the incident's timeline has for it.
    text: str
    at: datetime
    # How far it is trusted, and the rows it rests on, each with its credit.
    confidence: Confidence
    sources: tuple[Source, ...]
    # A `place` and a `traffic_event` are a point.
    point: Point | None = None
    # An `aircraft` is the positions it was seen at, oldest first.
    path: tuple[Position, ...] = ()
    # An `alert_area` is rings of points: each ring is closed, and a ring inside
    # another is a hole in it.
    rings: tuple[tuple[Point, ...], ...] = ()
    # What is not drawn of it, in a sentence, or None.
    note: str | None = None


def drawn(incident: Incident, row: Callable[[int], Evidence | None]) -> tuple[Feature, ...]:
    """What is drawn on the map of `incident`, its place first. `row` reads an
    evidence row by its id (`EvidenceStore.get`): a 511 event's point and an
    alert's area are in the rows, and not in the incident.

    Nothing for an incident with no point, and nothing for a medical response."""
    if incident.category == MEDICAL_EVENT or incident.point is None:
        return ()
    features = [_place(incident)]
    # The line the timeline has for a row, by the line's kind and the row's id.
    lines = {
        (entry.kind, source.evidence_id): entry
        for entry in incident.timeline
        for source in entry.sources
        if source.evidence_id is not None
    }
    for entry in incident.timeline:
        # Only the version of a 511 event that is in force: its line is in the
        # present tense, and an earlier version can be about another road.
        if entry.kind != "closure" or entry.details.get("state") != "in_force":
            continue
        found = _row_of(entry, row, "traffic")
        if found is not None and found.point is not None:
            features.append(_of_line("traffic_event", entry, point=found.point))
    for track in incident.aircraft:
        line = lines.get(("aircraft", track.rows[0].id))
        if line is None:
            continue
        seen = [Position(found.point, found.occurred_at) for found in track.rows if found.point]
        features.append(
            _of_line(
                "aircraft",
                line,
                path=tuple(seen[-MAX_PATH:]),
                note=_PATH_CUT if len(seen) > MAX_PATH else None,
            )
        )
    for official in incident.official:
        # `unknown` is not `in_force`: an alert that may have been ended is not
        # drawn as an area that holds.
        if official.kind != "alert" or official.state != "in_force":
            continue
        row_id = official.source.evidence_id
        line = lines.get(("alert", row_id))
        found = None if row_id is None else row(row_id)
        if line is None or found is None or found.kind != "alert":
            continue
        rings = tuple(_rings(found.data.get("geometry")))
        if not rings:
            # Issued for a whole zone: there is no area of its own to draw.
            continue
        too_large = sum(len(ring) for ring in rings) > MAX_RING_POINTS
        features.append(
            _of_line(
                "alert_area",
                line,
                rings=() if too_large else rings,
                note=_AREA_TOO_LARGE if too_large else None,
            )
        )
    return tuple(features)


def _place(incident: Incident) -> Feature:
    assert incident.point is not None
    if incident.point_source is not None:
        # The point is a 511 row's: it is trusted as that row is, and carries
        # 511's credit.
        return Feature(
            kind="place",
            text=_PLACE_FROM_511,
            at=incident.point_source.at,
            confidence=incident.point_source.confidence,
            sources=(incident.point_source,),
            point=incident.point,
        )
    # The block is the one the radio named, so the point rests on the calls.
    calls = [entry for entry in incident.timeline if entry.kind == "radio"]
    return Feature(
        kind="place",
        text=_PLACE_FROM_BLOCKS,
        at=incident.opened_at,
        # With none of its calls among the lines that were read, it is trusted
        # as little as anything is.
        confidence=weakest([entry.confidence for entry in calls]) if calls else "low",
        sources=tuple(source for entry in calls for source in entry.sources),
        point=incident.point,
    )


def _of_line(kind: FeatureKind, line: Entry, **where: Any) -> Feature:
    """A feature that is a line of the timeline: its words, its confidence, and
    its sources are the line's."""
    return Feature(
        kind=kind,
        text=line.text,
        at=line.at,
        confidence=line.confidence,
        sources=line.sources,
        **where,
    )


def _row_of(entry: Entry, row: Callable[[int], Evidence | None], kind: str) -> Evidence | None:
    for source in entry.sources:
        if source.evidence_id is None:
            continue
        found = row(source.evidence_id)
        if found is not None and found.kind == kind:
            return found
    return None


def _rings(geometry: Any) -> Iterator[tuple[Point, ...]]:
    """The rings of a GeoJSON polygon, multi-polygon, or collection of them, each
    as it is given. A ring with a position that is not one is left out whole, and
    so is anything that is not a polygon."""
    if not isinstance(geometry, dict):
        return
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "GeometryCollection":
        for inner in geometry.get("geometries") or ():
            yield from _rings(inner)
        return
    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon":
        polygons = coordinates if isinstance(coordinates, list) else []
    else:
        return
    for polygon in polygons:
        if not isinstance(polygon, list):
            continue
        for ring in polygon:
            points = _ring(ring)
            if points is not None:
                yield points


def _ring(ring: Any) -> tuple[Point, ...] | None:
    if not isinstance(ring, list) or len(ring) < 4:
        return None
    points: list[Point] = []
    for position in ring:
        if not isinstance(position, list) or len(position) < 2:
            return None
        try:
            # GeoJSON has the longitude first.
            points.append(Point(lat=position[1], lon=position[0]))
        except ValueError:
            return None
    return tuple(points)
