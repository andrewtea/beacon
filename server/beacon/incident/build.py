"""Building incidents: the calls of each one, and what else was said about its place.

`group_calls` (in `calls`) decides which radio calls are one incident. This module
gives each incident its point, attaches what other sources said about the same place
at the same time, and writes its timeline. All of it is plain code: no model is asked,
and the same rows give the same incidents.

What is attached, and why it is taken to belong:

- **A 511 event** that was created while the incident was going on, on a road the
  incident's calls name or within `CLOSURE_M` of its point. Road work that was there
  all day is not, because it was not created then.
- **An NWS alert drawn for an area** that holds the incident's point, or whose middle
  is within `ALERT_M` of it, while the incident was going on. A city-wide alert, which
  has no area of its own, is about the city and not about this incident.
- **A notice** that names one of the incident's streets and was published while it
  was going on or in the hours after. A press release keeps no streets, so it is
  attached only when exactly one incident of its hazard is in its time.
- **Taps** from near the point while it was going on, which are counted and never
  listed.
- **Aircraft** that circled or came low over the point.

A medical response gets none of these. It is its crews and its block.

An incident's point is the middle of its block when somebody listed the block
(`beacon.incident.places`), else the middle of a closure 511 lists on its street,
else nothing. With no point it is matched to nothing by distance.

The wording here is where the product's rules about words are kept:

- A medical call is written as "Medical response", its units, and its block.
- What crews say on the radio about an evacuation, a shelter, or an all-clear is
  written as radio traffic and said not to be an official order.
- An official alert's instruction is the authority's own words, shown as such
  (`Official.instruction`). Beacon writes no instruction itself.
- A notice is written as a document of its type that was published, with its link.
  Its words are a model's reading, and none of them is shown here: the redaction
  pass has to read them first.
- That a source stopped listing something is written as what Beacon saw, not as an
  all-clear.
- Everything from 511 carries the credit its data agreement requires.
"""

import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from beacon.evidence import Confidence, Evidence, Kind, Point
from beacon.incident.aircraft import NEAR_M, Track, tracks
from beacon.incident.calls import IDLE, CallGroup, group_calls
from beacon.incident.inforce import Standing, lifted, standings
from beacon.incident.model import (
    CONFIDENCE_RANK,
    Credit,
    Entry,
    Incident,
    Official,
    Signal,
    Source,
)
from beacon.incident.places import Blocks
from beacon.notice import ORDERS
from beacon.radio.vocabulary import MEDICAL_EVENT, clean_street, place_in_words

# How far from an incident's point a closure, an alert's middle, and a tap may be.
CLOSURE_M = 300.0
ALERT_M = 500.0
TAP_M = 400.0
# How long before an incident's first call, and after its last, something said about
# its place is still taken to be about it.
BEFORE = timedelta(minutes=10)
# A notice is published well after the calls: an all-clear, a press release.
NOTICE_AFTER = timedelta(hours=6)
# An incident with an official alert in force stays open this long after its last row.
OFFICIAL_OPEN = timedelta(hours=6)
# This many units named is a large response.
MANY_UNITS = 8
# This many taps near an incident is a signal of its own.
MANY_TAPS = 3

CREDIT_511 = Credit("data provided by 511.org", "https://511.org")

# Hands back the rows of one kind within so many metres of a point, between two
# moments. Taps and aircraft are asked for this way, incident by incident: there can
# be a great many of either, few of them near anything, and read with the other rows
# they would crowd those out of what the engine reads at once.
Nearby = Callable[[Kind, Point, float, datetime, datetime], Sequence[Evidence]]

_LABELS = {
    "openmhz": "Fire and EMS dispatch radio, as Ember heard it",
    "nws": "National Weather Service alert feed",
    "511": "511 SF Bay traffic events",
    "notices": "Official notice, as Ember read it",
    "opensky": "OpenSky Network aircraft positions",
    "app": "Taps in the Beacon app",
}
_CATEGORY_WORDS = {
    "fire": "Fire",
    "hazmat": "Hazardous materials call",
    "rescue": "Rescue",
    "collision": "Collision",
    "alarm": "Alarm",
    "utility": "Utility call",
    "service": "Service call",
    "other": "Fire department call",
    "medical": "Medical response",
}
_STATUS_WORDS = {
    "dispatched": "units dispatched",
    "en_route": "units on the way",
    "on_scene": "units on scene",
    "working": "crews at work",
    "under_control": "reported under control",
    "cleared": "units cleared",
    "cancelled": "call cancelled",
    "unknown": "radio traffic",
}
_NOT_AN_ORDER = "That is radio traffic, not an official order."
_DETAIL_WORDS = {
    "smoke_showing": "Smoke showing.",
    "fire_showing": "Fire showing.",
    "working_fire": "Working fire declared.",
    "additional_alarm": "Additional alarm called.",
    "exposure_threatened": "Neighbouring buildings threatened.",
    "rescue_in_progress": "Rescue in progress.",
    "evacuation": f"Crews mention an evacuation. {_NOT_AN_ORDER}",
    "shelter_in_place": f"Crews mention sheltering in place. {_NOT_AN_ORDER}",
    "police_requested": "Police requested.",
    "utilities_requested": "Utilities requested.",
    "road_closed": "Crews report a road closed.",
    "aircraft_overhead": "Crews mention an aircraft overhead.",
    "knocked_down": "Fire reported knocked down.",
    "overhaul": "Overhaul under way.",
    "all_clear": f"Crews say all clear. {_NOT_AN_ORDER}",
}
_NOTICE_WORDS = {
    "evacuation_order": "an evacuation order",
    "evacuation_warning": "an evacuation warning",
    "shelter_in_place": "a shelter-in-place order",
    "road_closure": "a road closure notice",
    "all_clear": "an all-clear",
    "advisory": "an advisory",
    "press_release": "a press release",
}
_READING = "This is Beacon's reading of the document. Read the document for what it says."
# Which hazards a notice can be about for it to be about an incident of a category.
_HAZARDS: dict[str, set[str]] = {
    "fire": {"fire", "smoke"},
    "hazmat": {"hazmat", "smoke"},
    "utility": {"utility"},
    "rescue": {"flood", "earthquake", "weather"},
}
_NOT_LISTED = {
    "alert": (
        "NWS no longer lists this alert, before the time it said it would end. That "
        "is what Beacon saw, not an official all-clear."
    ),
    "traffic": (
        "511 no longer lists this event. That is what Beacon saw, not an official reopening."
    ),
}
_LISTED_AGAIN = {"alert": "NWS lists this alert again.", "traffic": "511 lists this event again."}


def build_incidents(
    rows: Sequence[Evidence],
    *,
    blocks: Blocks,
    now: datetime,
    nearby: Nearby | None = None,
) -> list[Incident]:
    """The incidents the rows tell of at `now`, newest first. `rows` holds the
    radio, traffic, alert, and notice rows to read. Taps and aircraft are asked for
    through `nearby`, incident by incident; without it an incident has neither."""
    known = [row for row in rows if row.occurred_at <= now]
    states = standings(known, now)
    by_kind: dict[str, list[Evidence]] = {}
    for row in sorted(known, key=_order):
        by_kind.setdefault(row.kind, []).append(row)
    groups = group_calls(by_kind.get("radio", []))
    events = _events(by_kind.get("traffic", []))
    press = _press_matches(groups, by_kind.get("notice", []))
    incidents = [
        _incident(group, by_kind, events, states, press, blocks, now, nearby) for group in groups
    ]
    return sorted(incidents, key=lambda incident: (incident.opened_at, incident.id), reverse=True)


def incident_id(opener: Evidence) -> str:
    """The id of the incident a row opened: a digest of the row's source and its
    reference there, so the same story gives the same id in any store."""
    return "inc-" + hashlib.sha256(f"{opener.source}\x1f{opener.ref}".encode()).hexdigest()[:16]


def _incident(
    group: CallGroup,
    by_kind: dict[str, list[Evidence]],
    events: dict[object, list[Evidence]],
    states: dict[int, Standing],
    press: dict[int, int],
    blocks: Blocks,
    now: datetime,
    nearby: Nearby | None,
) -> Incident:
    place = place_in_words(group.street, group.block, group.cross_street)
    # The row that was stored first names the incident, so a call that is stored
    # late does not rename it.
    opener = min(group.rows, key=lambda row: row.id)
    entries = [_radio_entry(row) for row in group.rows]
    official: list[Official] = []
    signals: list[Signal] = []
    track_list: list[Track] = []
    taps = 0
    point = blocks.point(group.street, group.block)
    point_from = "blocks" if point is not None else None
    streets = _streets(group)
    start, end = group.opened_at - BEFORE, group.last_at + IDLE
    members = list(group.rows)
    if not group.medical:
        closures = []
        for versions in events.values():
            created = _created(versions)
            if not start <= created <= end:
                continue
            named = _roads(versions) & set(streets)
            near = point is not None and any(
                row.point is not None and row.point.distance_m(point) <= CLOSURE_M
                for row in versions
            )
            if named or near:
                closures.append(versions)
                if point is None and named and versions[-1].point is not None:
                    point, point_from = versions[-1].point, "closure"
        alerts = _alerts_at(by_kind.get("alert", []), point, start, end)
        notices = _notices_for(group, by_kind.get("notice", []), streets, press, start)
        changes = _changes(by_kind, closures, alerts)
        for versions in closures:
            entries += [_traffic_entry(row, states, first=row is versions[0]) for row in versions]
            members += versions
        for alert in alerts:
            entries.append(_alert_entry(alert, states))
            if alert.data.get("message_type") != "Cancel":
                official.append(_official_alert(alert, states[alert.id]))
        entries += [_change_entry(change) for change in changes]
        ended = lifted(notices)
        for notice in notices:
            entries.append(_notice_entry(notice, streets))
            official.append(_official_notice(notice, ended.get(notice.id), now))
        members += [*alerts, *changes, *notices]
        if point is not None and nearby is not None:
            until = min(end, now)
            near_taps = sorted(nearby("tap", point, TAP_M, start, until), key=_order)
            taps = len(near_taps)
            track_list = [
                track
                for track in tracks(nearby("aircraft", point, NEAR_M, start, until), point)
                if track.circling or (track.low and track.nearest_m <= NEAR_M / 2)
            ]
            for track in track_list:
                entries.append(_aircraft_entry(track))
                members += track.rows
            if near_taps:
                entries.append(_taps_entry(near_taps))
        signals = _signals(group, official, closures, states, track_list, taps)
    entries.sort(key=lambda entry: entry.at)
    in_force = any(item.state == "in_force" for item in official)
    updated_at = max(entry.at for entry in entries)
    return Incident(
        id=incident_id(opener),
        category=MEDICAL_EVENT if group.medical else group.category,
        title=_title(group, place),
        status=group.status,
        open=(not group.over and now - group.last_at <= IDLE)
        or (in_force and now - updated_at <= OFFICIAL_OPEN),
        opened_at=group.opened_at,
        updated_at=updated_at,
        place=place,
        point=point,
        point_from=point_from,  # type: ignore[arg-type]
        streets=streets,
        units=tuple(sorted(group.named, key=_unit_order)),
        alarm=group.alarm,
        timeline=tuple(entries),
        official=tuple(official),
        aircraft=tuple(track_list),
        taps=taps,
        signals=tuple(signals),
        stand_in=any(row.stand_in for row in members),
        evidence_ids=tuple(sorted({row.id for row in members})),
    )


def source_of(row: Evidence) -> Source:
    """The source line of a claim that rests on one row."""
    return Source(
        kind=row.kind,
        source=row.source,
        label=_LABELS.get(row.source, row.source),
        confidence=row.confidence,
        at=row.occurred_at,
        evidence_id=row.id,
        url=row.url,
        stand_in=row.stand_in,
        model=row.model,
        credit=CREDIT_511 if row.source == "511" else None,
    )


def weakest(confidences: Sequence[Confidence]) -> Confidence:
    """The least of several confidences: a claim is trusted no more than the
    weakest row it rests on."""
    return max(confidences, key=CONFIDENCE_RANK.index)


def _title(group: CallGroup, place: str | None) -> str:
    words = _CATEGORY_WORDS["medical" if group.medical else group.category]
    return (
        f"{words} at the {place}"
        if place and group.block is not None
        else (f"{words} on {place}" if place else f"{words}, place not known")
    )


def _streets(group: CallGroup) -> tuple[str, ...]:
    """Every street the incident's calls name at its own place."""
    named: dict[str, None] = {}
    for row in group.rows:
        if row.data.get("street") == group.street:
            for key in ("street", "cross_street"):
                street = row.data.get(key)
                if isinstance(street, str):
                    named[street] = None
    return tuple(named)


def _radio_entry(row: Evidence) -> Entry:
    data = row.data
    units = [unit for unit in data.get("units") or [] if isinstance(unit, str)]
    medical = data.get("category") == "medical"
    if medical:
        # Its crews and its block, and nothing more, whatever the row holds.
        parts = [f"{_CATEGORY_WORDS['medical']}."]
        details: dict[str, Any] = {"event": MEDICAL_EVENT, "units": units, "place": row.place}
    else:
        status = data.get("status") if data.get("status") in _STATUS_WORDS else "unknown"
        tags = [tag for tag in data.get("details") or [] if tag in _DETAIL_WORDS]
        category = _CATEGORY_WORDS.get(data.get("category"), _CATEGORY_WORDS["other"])
        parts = [f"{category}: {_STATUS_WORDS[status]}."]
        alarm = data.get("alarm")
        if isinstance(alarm, int) and not isinstance(alarm, bool):
            parts.append(f"Alarm level {alarm}.")
        parts += [_DETAIL_WORDS[tag] for tag in tags]
        details = {
            "event": data.get("category"),
            "status": status,
            "details": tags,
            "units": units,
            "alarm": alarm,
            "place": row.place,
        }
    if units:
        parts.append(f"Units: {', '.join(units)}.")
    if row.place:
        parts.append(f"Place: {row.place}.")
    if data.get("clarity") == "partial":
        parts.append("Only part of the call could be made out.")
    details["clarity"] = data.get("clarity")
    return Entry(
        at=row.occurred_at,
        kind="medical" if medical else "radio",
        text=" ".join(parts),
        confidence=row.confidence,
        sources=(source_of(row),),
        details=details,
    )


def _traffic_entry(row: Evidence, states: dict[int, Standing], *, first: bool) -> Entry:
    headline = row.data.get("headline")
    return Entry(
        at=row.occurred_at,
        kind="closure",
        # The headline is 511's own words.
        text=f"{'511 lists' if first else '511 updated'}: {headline}",
        confidence=row.confidence,
        sources=(source_of(row),),
        details={
            "event_id": row.data.get("event_id"),
            "headline": headline,
            "roads": row.data.get("roads"),
            "closure": row.data.get("closure"),
            "severity": row.data.get("severity"),
            "state": states[row.id].state,
        },
    )


def _alert_entry(row: Evidence, states: dict[int, Standing]) -> Entry:
    data = row.data
    sender = data.get("sender") or "An authority"
    verb = {"Update": "updated its alert", "Cancel": "cancelled its alert"}.get(
        data.get("message_type"), "issued an alert"
    )
    # The event and the headline are the authority's own words.
    headline = data.get("headline")
    text = f"{sender} {verb}, {data.get('event')}" + (f": {headline}" if headline else ".")
    return Entry(
        at=row.occurred_at,
        kind="alert",
        text=text,
        confidence=row.confidence,
        sources=(source_of(row),),
        details={
            "event": data.get("event"),
            "message_type": data.get("message_type"),
            "severity": data.get("severity"),
            "urgency": data.get("urgency"),
            "sender": data.get("sender"),
            "headline": headline,
            "until": None if row.until is None else row.until.isoformat(),
            "state": states[row.id].state,
        },
    )


def _change_entry(row: Evidence) -> Entry:
    words = _NOT_LISTED if row.data.get("change") == "not_listed" else _LISTED_AGAIN
    return Entry(
        at=row.occurred_at,
        kind="alert" if row.kind == "alert" else "closure",
        text=words[row.kind],
        confidence=row.confidence,
        sources=(source_of(row),),
        details={"change": row.data.get("change"), "about": row.data.get("about")},
    )


def _notice_entry(row: Evidence, streets: Sequence[str]) -> Entry:
    notice_type = row.data.get("notice_type")
    words = _NOTICE_WORDS.get(notice_type, "a notice")
    return Entry(
        at=row.occurred_at,
        kind="notice",
        text=f"An authority published {words}. {_READING}",
        confidence=row.confidence,
        sources=(source_of(row),),
        details={
            "notice_type": notice_type,
            "hazard": row.data.get("hazard"),
            # The streets of the incident that the notice names too. They are the
            # incident's own names, which were on a list when a call gave them. A
            # street only the notice names is a word a model wrote, and is not shown.
            "streets": [street for street in streets if street in _clean_streets(row)],
            "incomplete": row.data.get("incomplete") or [],
        },
    )


def _official_alert(row: Evidence, standing: Standing) -> Official:
    return Official(
        kind="alert",
        type=str(row.data.get("event")),
        state=standing.state,  # type: ignore[arg-type]
        ended_by=None if standing.by is None else standing.by.id,
        issued_at=row.occurred_at,
        until=row.until,
        issuer=row.data.get("sender"),
        headline=row.data.get("headline"),
        instruction=row.data.get("instruction"),
        reading=False,
        source=source_of(row),
    )


def _official_notice(row: Evidence, lifted_by: Evidence | None, now: datetime) -> Official:
    state = "published"
    if lifted_by is not None:
        state = "lifted"
    elif row.until is not None and row.until <= now:
        state = "ended"
    return Official(
        kind="notice",
        type=str(row.data.get("notice_type")),
        state=state,  # type: ignore[arg-type]
        ended_by=None if lifted_by is None else lifted_by.id,
        issued_at=row.occurred_at,
        until=row.until,
        # A notice's issuer, title, areas, instruction, and summary are words a
        # model wrote. None of them is shown before the redaction pass has read them.
        issuer=None,
        headline=None,
        instruction=None,
        reading=True,
        source=source_of(row),
    )


def _aircraft_entry(track: Track) -> Entry:
    what = "A helicopter" if track.category == "rotorcraft" else "An aircraft"
    name = f" ({track.callsign})" if track.callsign else ""
    did = "circled nearby" if track.circling else "passed low nearby"
    minutes = max(1, round(track.minutes))
    height = "" if track.lowest_m is None else f", as low as {round(track.lowest_m)} m"
    return Entry(
        at=track.first_seen,
        kind="aircraft",
        text=f"{what}{name} {did} for about {minutes} min{height}.",
        confidence=weakest([row.confidence for row in track.rows]),
        # The first and the last position stand for the track.
        sources=tuple(
            source_of(row)
            for row in {track.rows[0].id: track.rows[0], track.rows[-1].id: track.rows[-1]}.values()
        ),
        details={
            "icao24": track.icao24,
            "callsign": track.callsign,
            "category": track.category,
            "first_seen": track.first_seen.isoformat(),
            "last_seen": track.last_seen.isoformat(),
            "circling": track.circling,
            "low": track.low,
            "nearest_m": round(track.nearest_m),
            "lowest_m": track.lowest_m,
        },
    )


def _taps_entry(near: Sequence[Evidence]) -> Entry:
    """One line for all the taps from near an incident, oldest first in `near`."""
    count = len(near)
    people = "1 person" if count == 1 else f"{count} people"
    return Entry(
        at=near[0].occurred_at,
        kind="taps",
        text=f'{people} nearby pressed "What\'s that?" while this was going on.',
        confidence="low",
        sources=(
            Source(
                kind="tap",
                source="app",
                label=_LABELS["app"],
                confidence="low",
                at=near[0].occurred_at,
                evidence_id=None,
                url=None,
                # A person's tap is nobody's stand-in.
                stand_in=False,
            ),
        ),
        details={"count": count, "last_at": near[-1].occurred_at.isoformat()},
    )


def _signals(
    group: CallGroup,
    official: Sequence[Official],
    closures: Sequence[Sequence[Evidence]],
    states: dict[int, Standing],
    track_list: Sequence[Track],
    taps: int,
) -> list[Signal]:
    signals: list[Signal] = []
    if group.alarm is not None and group.alarm >= 2:
        rows = [row for row in group.rows if row.data.get("alarm") == group.alarm]
        signals.append(Signal("alarm", f"Alarm level {group.alarm}.", _ids(rows)))
    evacuating = [row for row in group.rows if "evacuation" in (row.data.get("details") or [])]
    if evacuating:
        signals.append(
            Signal(
                "evacuation_on_radio",
                f"Crews mention an evacuation. {_NOT_AN_ORDER}",
                _ids(evacuating),
            )
        )
    if len(group.named) >= MANY_UNITS:
        signals.append(Signal("many_units", f"{len(group.named)} units named.", _ids(group.rows)))
    orders = [
        item
        for item in official
        if item.kind == "notice"
        and item.type in ORDERS - {"all_clear"}
        and item.state == "published"
    ]
    if orders:
        signals.append(
            Signal(
                "official_order",
                "An authority published an order that no notice Beacon has read lifts.",
                tuple(item.source.evidence_id for item in orders if item.source.evidence_id),
            )
        )
    alerts = [item for item in official if item.kind == "alert" and item.state == "in_force"]
    if alerts:
        signals.append(
            Signal(
                "official_alert",
                "An official alert for this place is in force.",
                tuple(item.source.evidence_id for item in alerts if item.source.evidence_id),
            )
        )
    closed = [
        versions[-1]
        for versions in closures
        if versions[-1].data.get("closure") and states[versions[-1].id].in_force
    ]
    if closed:
        signals.append(Signal("closure", "511 lists a road closed.", _ids(closed)))
    circling = [track for track in track_list if track.circling]
    if circling:
        signals.append(
            Signal(
                "aircraft_circling",
                "An aircraft circled nearby.",
                tuple(track.rows[0].id for track in circling),
            )
        )
    if taps >= MANY_TAPS:
        signals.append(Signal("taps", f"{taps} taps from nearby.", ()))
    return signals


def _events(traffic: Sequence[Evidence]) -> dict[object, list[Evidence]]:
    """The versions of each 511 event, oldest first, without the rows that record
    a change."""
    events: dict[object, list[Evidence]] = {}
    for row in traffic:
        if "change" not in row.data:
            events.setdefault((row.source, row.data.get("event_id")), []).append(row)
    return events


def _created(versions: Sequence[Evidence]) -> datetime:
    """When 511 says an event was created, or when its first version is from."""
    said = versions[0].data.get("created")
    if isinstance(said, str):
        try:
            created = datetime.fromisoformat(said)
        except ValueError:
            created = None
        if created is not None and created.utcoffset() is not None:
            return created
    return versions[0].occurred_at


def _roads(versions: Sequence[Evidence]) -> set[str]:
    """The roads an event's versions name, each in the form Beacon keeps a street in."""
    named: set[str] = set()
    for row in versions:
        for road in row.data.get("roads") or []:
            street = clean_street(road.get("name")) if isinstance(road, dict) else None
            if street is not None:
                named.add(street)
    return named


def _alerts_at(
    alerts: Sequence[Evidence], point: Point | None, start: datetime, end: datetime
) -> list[Evidence]:
    """The alert messages drawn for an area that holds `point`, or has its middle
    near it, and whose span touches the incident's, with the messages that cancel
    them."""
    if point is None:
        return []
    found: list[Evidence] = []
    for row in alerts:
        if "change" in row.data or row.point is None:
            continue
        here = row.point.distance_m(point) <= ALERT_M or _inside(row.data.get("geometry"), point)
        if here and row.occurred_at <= end and (row.until is None or row.until >= start):
            found.append(row)
    refs = {row.ref for row in found}
    for row in alerts:
        named = row.data.get("replaces")
        cancels = row.data.get("message_type") == "Cancel" and isinstance(named, list)
        if cancels and refs & set(named) and row not in found:
            found.append(row)
    return sorted(found, key=_order)


def _changes(
    by_kind: dict[str, list[Evidence]],
    closures: Sequence[Sequence[Evidence]],
    alerts: Sequence[Evidence],
) -> list[Evidence]:
    """The rows that say one of these events or alerts stopped being listed, or is
    listed again."""
    about = {(row.source, row.ref) for versions in closures for row in versions}
    about |= {(row.source, row.ref) for row in alerts}
    return [
        row
        for kind in ("traffic", "alert")
        for row in by_kind.get(kind, [])
        if "change" in row.data and (row.source, row.data.get("about")) in about
    ]


def _notices_for(
    group: CallGroup,
    notices: Sequence[Evidence],
    streets: Sequence[str],
    press: dict[int, int],
    start: datetime,
) -> list[Evidence]:
    found = []
    for row in notices:
        if row.data.get("notice_type") in (None, "other"):
            # Ember did not take the document for a notice.
            continue
        if not start <= row.occurred_at <= group.last_at + NOTICE_AFTER:
            continue
        if set(_clean_streets(row)) & set(streets) or press.get(row.id) == id(group):
            found.append(row)
    return found


def _press_matches(groups: Sequence[CallGroup], notices: Sequence[Evidence]) -> dict[int, int]:
    """Which incident each notice that names no street is about: the one incident of
    its hazard in its time, when there is exactly one. The incident is named by the
    identity of its group."""
    matches: dict[int, int] = {}
    for row in notices:
        if _clean_streets(row) or row.data.get("notice_type") in (None, "other"):
            continue
        hazard = row.data.get("hazard")
        about = [
            group
            for group in groups
            if not group.medical
            and hazard in _HAZARDS.get(group.category, set())
            and group.opened_at - BEFORE <= row.occurred_at <= group.last_at + NOTICE_AFTER
        ]
        if len(about) == 1:
            matches[row.id] = id(about[0])
    return matches


def _clean_streets(row: Evidence) -> list[str]:
    streets = row.data.get("streets")
    cleaned = (clean_street(street) for street in (streets if isinstance(streets, list) else []))
    return [street for street in cleaned if street is not None]


def _inside(geometry: Any, point: Point) -> bool:
    """Whether a GeoJSON polygon or multipolygon holds a point, by its outer rings.
    A ring's positions are longitude first."""
    if not isinstance(geometry, dict):
        return False
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Polygon":
        polygons = [coordinates]
    elif geometry.get("type") == "MultiPolygon":
        polygons = coordinates if isinstance(coordinates, list) else []
    else:
        return False
    return any(
        isinstance(polygon, list) and polygon and _in_ring(polygon[0], point)
        for polygon in polygons
    )


def _in_ring(ring: Any, point: Point) -> bool:
    if not isinstance(ring, list):
        return False
    corners = [
        (corner[0], corner[1])
        for corner in ring
        if isinstance(corner, list)
        and len(corner) >= 2
        and all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in corner[:2]
        )
    ]
    inside = False
    for (lon_a, lat_a), (lon_b, lat_b) in zip(corners, corners[1:] + corners[:1], strict=False):
        if (lat_a > point.lat) != (lat_b > point.lat):
            crossing = lon_a + (point.lat - lat_a) / (lat_b - lat_a) * (lon_b - lon_a)
            if point.lon < crossing:
                inside = not inside
    return inside


def _ids(rows: Sequence[Evidence]) -> tuple[int, ...]:
    return tuple(row.id for row in rows)


def _unit_order(unit: str) -> tuple[str, int]:
    letters = unit.rstrip("0123456789")
    digits = unit[len(letters) :]
    return (letters, int(digits) if digits else 0)


def _order(row: Evidence) -> tuple[datetime, int]:
    return (row.occurred_at, row.id)
