"""What is drawn on the map of one incident.

Four kinds of thing, each of which the incident already has as a line of its
timeline or as its place. The map adds where each is, and no claim of its own:

- `place`: the point Beacon has for the incident. It is the point of the hundred
  block named on the radio, from the list of blocks, or else a point Beacon took
  from a 511 event that listed a street of the incident's as closed. The sentence
  says which. The second is approximate, and says so: the radio need have named no
  block, and the point is the middle of the box around what 511 drew for the event,
  which Beacon worked out and which need not lie on the road.
- `traffic_event`: a 511 event of the incident's, as its version in force has it,
  in 511's own headline. Only the version in force is drawn: an earlier one can be
  about a road that is open again. It is drawn at the same kind of middle, and its
  note says so.
- `aircraft`: an aircraft the incident's timeline has, with the positions it was
  seen at near the place. Its sources are the rows of every position that is drawn,
  with the rows its sentence rests on.
- `alert_area`: the area an authority drew for an alert that is in force for the
  place. An alert that is not known to be in force is not drawn, and neither is one
  that was issued for a whole zone, which has no drawn area. **An area is drawn
  whole or not at all**: with one ring that cannot be read, a hole included, none
  of it is drawn and the note says so, because an area without one of its holes
  shows a place as inside it that the alert leaves out. An area is handed on as its
  polygons, each with its own holes, and never as one list of rings: a place is in
  the area when it is inside the outline of some polygon and in none of that
  polygon's holes, which is how the engine reads it (`build._inside`), and a list of
  rings read by parity would have a second polygon cut a hole in the first where
  they overlap.

Nothing of a tap is drawn: a tap's point is its sender's. A notice has no place and
is not drawn. A medical response has no map (`NO_MAP_OF_MEDICAL`).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from beacon.evidence import Confidence, Evidence, Point
from beacon.incident import Entry, Incident, Source, source_of, weakest
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
    "that of the block named on the radio, when one was named and is on Beacon's "
    "list of blocks, or else one Beacon takes from a 511 event that listed a street "
    "of the incident's as closed."
)
_PLACE_FROM_BLOCKS = (
    "The point Beacon's list of blocks has for the block named on the radio. It marks "
    "the block, not an address."
)
# Not "where 511 listed the closure": 511 gave the event a shape, and the point is
# the middle of the box around it, which Beacon worked out. And not "the block":
# the radio need have named none.
_PLACE_FROM_511 = (
    "An approximate point, which Beacon took from a 511 event that listed a street of "
    "this incident's as closed: the middle of what 511 drew for the event. Beacon has "
    "no point of its own for this incident. The point need not lie on the road, and it "
    "marks no address."
)
_EVENT_POINT = (
    "Drawn at the middle of what 511 drew for the event, which Beacon worked out. It "
    "need not lie on the road."
)
_PATH_CUT = f"Only its newest {MAX_PATH} positions are drawn."
_AREA_TOO_LARGE = (
    "Its area has more points than Beacon draws, so it is not drawn. The alert says "
    "which area it covers."
)
_AREA_UNREADABLE = (
    "Beacon could not read every part of this alert's area, so none of it is drawn: "
    "an area drawn in part can show a place as inside it that the alert leaves out. "
    "The alert says which area it covers."
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
    # An `alert_area` is polygons. Each is closed rings of points: its outline
    # first, and then the holes in it. A place is in the area when it is inside
    # the outline of some polygon and in none of that polygon's holes.
    polygons: tuple[tuple[tuple[Point, ...], ...], ...] = ()
    # What is not drawn of it, or how roughly it is placed, in a sentence, or None.
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
            features.append(_of_line("traffic_event", entry, point=found.point, note=_EVENT_POINT))
    for track in incident.aircraft:
        line = lines.get(("aircraft", track.rows[0].id))
        if line is None:
            continue
        seen = [found for found in track.rows if found.point is not None]
        plotted = seen[-MAX_PATH:]
        # The rows the sentence rests on, and the row of every position that is
        # drawn: a bend in the path is some row's, and that row is named.
        named = {source.evidence_id for source in line.sources}
        sources = (
            *line.sources,
            *(source_of(found) for found in plotted if found.id not in named),
        )
        features.append(
            Feature(
                kind="aircraft",
                text=line.text,
                at=line.at,
                # The line's is already the least among every row of the track.
                confidence=line.confidence,
                sources=sources,
                path=tuple(
                    Position(found.point, found.occurred_at)
                    for found in plotted
                    if found.point is not None
                ),
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
        geometry = found.data.get("geometry")
        if geometry is None:
            # Issued for a whole zone: there is no area of its own to draw. The
            # engine has no such alert as applying to a place; this is for one
            # that came to be listed some other way.
            continue
        polygons = _polygons(geometry)
        if polygons is None:
            note: str | None = _AREA_UNREADABLE
        elif sum(len(ring) for rings in polygons for ring in rings) > MAX_RING_POINTS:
            note = _AREA_TOO_LARGE
        else:
            note = None
        features.append(
            _of_line(
                "alert_area",
                line,
                polygons=polygons if note is None and polygons else (),
                note=note,
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


def _polygons(geometry: Any) -> tuple[tuple[tuple[Point, ...], ...], ...] | None:
    """The polygons of a GeoJSON polygon or multi-polygon, each as its rings (its
    outline, then its holes), every one of them, or None when any part of it
    cannot be read: a polygon that is not a list of rings, a ring that is not
    closed or has fewer than four positions, a position that is not one. Nothing
    is left out to make the rest fit, and the polygons are kept apart: which
    polygon a ring is of says whether it is an outline or a hole. Only the two
    kinds the engine reads an area from (`build._inside`) are read here."""
    if not isinstance(geometry, dict):
        return None
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Polygon":
        given = [coordinates]
    elif kind == "MultiPolygon" and isinstance(coordinates, list) and coordinates:
        given = coordinates
    else:
        return None
    polygons: list[tuple[tuple[Point, ...], ...]] = []
    for polygon in given:
        if not isinstance(polygon, list) or not polygon:
            return None
        rings: list[tuple[Point, ...]] = []
        for ring in polygon:
            points = _ring(ring)
            if points is None:
                return None
            rings.append(points)
        polygons.append(tuple(rings))
    return tuple(polygons)


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
    # A ring ends where it began.
    return tuple(points) if points[0] == points[-1] else None
