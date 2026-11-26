"""Building incidents: the calls of each one, and what else was said about its place.

`group_calls` (in `calls`) decides which radio calls are one incident. This module
gives each incident its point, attaches what other sources said about the same place
at the same time, and writes its timeline. All of it is plain code: no model is asked,
and the same rows give the same incidents.

What is attached, and why it is taken to belong:

- **A 511 event** that was created while the incident was going on, on a road the
  incident's calls name or within `CLOSURE_M` of its point. Road work that was there
  all day is not, because it was not created then.
- **An NWS alert drawn for an area** that holds the incident's point, holes left out,
  while the incident was going on. Being near the middle of an alert's area is not
  being in it, and a city-wide alert, which has no area of its own, is about the
  city and not about this incident. A later message that updates or cancels such an
  alert is a line of the timeline.
- **A notice** that names one of the incident's streets and was published while it
  was going on or in the hours after. A press release keeps no streets, so it is
  attached when exactly one incident of its hazard is in its time; any other notice
  that names no street is attached to nothing.
- **Taps** from near the point while it was going on, which are counted and never
  listed.
- **Aircraft** that circled or came low over the point.

An incident the radio has cleared takes nothing more by time and place: a closure, a
tap, or an aircraft after that can be the next incident's. A notice still follows,
because an all-clear and a press release come after.

A medical response gets none of these. It is its crews and its block.

An incident's point is the middle of its block when somebody listed the block
(`beacon.incident.places`), else the middle of an event in which 511 lists a road of
its as closed, else nothing. With no point it is matched to nothing by distance.

The wording here is where the product's rules about words are kept:

- A medical call is written as "Medical response", its units, and its block.
- What crews say on the radio about an evacuation, a shelter, or an all-clear is
  written as radio traffic and said not to be an official order.
- An official alert's instruction is the authority's own words, shown as such
  (`Official.instruction`). Beacon writes no instruction itself. A text that is as
  long as its poller keeps may have been cut, so it is left out whole and named in
  `Official.incomplete`: the original is where to read it.
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
from beacon.incident.inforce import Standing, lifted, notice_streets, standings
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
from beacon.ingest import nws, traffic511
from beacon.notice import ORDERS
from beacon.radio.vocabulary import MEDICAL_EVENT, clean_street, place_in_words

# How far from an incident's point a closure and a tap may be.
CLOSURE_M = 300.0
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
# moments, and whether there were more of them than it hands back. Taps and aircraft
# are asked for this way, incident by incident: there can be a great many of either,
# few of them near anything, and read with the other rows they would crowd those out
# of what the engine reads at once.
Nearby = Callable[[Kind, Point, float, datetime, datetime], tuple[Sequence[Evidence], bool]]

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
    unresolved_before: datetime | None = None,
) -> list[Incident]:
    """The incidents the rows tell of at `now`, newest first. `rows` holds the
    radio, traffic, alert, and notice rows to read. Taps and aircraft are asked for
    through `nearby`, incident by incident; without it an incident has neither.

    `unresolved_before` says that the rows which can end an alert or a closure were
    not all read, and from when they were: a message from that moment or earlier
    that nothing read here ends is then `unknown`, and is not said to be in force."""
    known = [row for row in rows if row.occurred_at <= now]
    states = standings(known, now)
    if unresolved_before is not None:
        by_id = {row.id: row for row in known}
        for row_id, standing in list(states.items()):
            if standing.in_force and by_id[row_id].occurred_at <= unresolved_before:
                states[row_id] = Standing("unknown")
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
    reference there. The row that opens an incident is the one of its rows that was
    stored first, so the id is this store's: another store that took the same calls
    in another order can name the incident by another of them."""
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
    near_taps: list[Evidence] = []
    taps_more = aircraft_more = False
    point = blocks.point(group.street, group.block)
    point_from = "blocks" if point is not None else None
    streets = _streets(group)
    start = group.opened_at - BEFORE
    # Once the radio has cleared it, what happens at its place is no longer taken
    # to be about it: the next thing there can be the next incident's.
    end = group.last_at if group.over else group.last_at + IDLE
    members = list(group.rows)
    if not group.medical:
        closures = []
        for versions in events.values():
            if not start <= _created(versions) <= end:
                continue
            named = _roads(versions) & set(streets)
            near = point is not None and any(
                row.point is not None and row.point.distance_m(point) <= CLOSURE_M
                for row in versions
            )
            if not (named or near):
                continue
            closures.append(versions)
            # Only a version that lists a road as closed says where the incident is.
            closed = [
                row for row in versions if row.data.get("closure") is True and row.point is not None
            ]
            if point is None and named and closed:
                point, point_from = closed[-1].point, "closure"
        alerts, revisions = _alerts_at(by_kind.get("alert", []), point, start, end)
        notices = _notices_for(group, by_kind.get("notice", []), streets, press, start)
        changes = _changes(by_kind, closures, alerts)
        for versions in closures:
            entries += [_traffic_entry(row, states, first=row is versions[0]) for row in versions]
            members += versions
        for alert in alerts:
            entries.append(_alert_entry(alert, states))
            official.append(_official_alert(alert, states[alert.id]))
        # A later message that updates or cancels one of them is a line of the
        # timeline. It is not listed as an alert for this place: its own area or
        # time may not be this incident's.
        entries += [_alert_entry(revision, states) for revision in revisions]
        entries += [_change_entry(change) for change in changes]
        ended = lifted(notices)
        for notice in notices:
            entries.append(_notice_entry(notice, streets))
            official.append(_official_notice(notice, ended.get(notice.id), now))
        members += [*alerts, *revisions, *changes, *notices]
        if point is not None and nearby is not None:
            until = min(end, now)
            found, taps_more = nearby("tap", point, TAP_M, start, until)
            near_taps = sorted(found, key=_order)
            positions, aircraft_more = nearby("aircraft", point, NEAR_M, start, until)
            track_list = [
                track
                for track in tracks(positions, point)
                if track.circling or (track.low and track.nearest_m <= NEAR_M / 2)
            ]
            for track in track_list:
                entries.append(_aircraft_entry(track))
                members += track.rows
            if near_taps:
                entries.append(_taps_entry(near_taps, taps_more))
        signals = _signals(group, official, closures, states, track_list, near_taps, taps_more)
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
        taps=len(near_taps),
        taps_more=taps_more,
        aircraft_more=aircraft_more,
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
    headline = _whole(row.data.get("headline"), traffic511.MAX_HEADLINE)
    lists = "511 lists" if first else "511 updated"
    return Entry(
        at=row.occurred_at,
        kind="closure",
        # The headline is 511's own words.
        text=f"{lists}: {headline}"
        if headline is not None
        else f"{lists} an event here. Its text is longer than Beacon keeps: read it at 511.org.",
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
    headline = _whole(data.get("headline"), nws.MAX_HEADLINE)
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
            "streets": [street for street in streets if street in notice_streets(row)],
            "incomplete": row.data.get("incomplete") or [],
        },
    )


def _official_alert(row: Evidence, standing: Standing) -> Official:
    kept = {
        "headline": _whole(row.data.get("headline"), nws.MAX_HEADLINE),
        "instruction": _whole(row.data.get("instruction"), nws.MAX_TEXT),
    }
    return Official(
        kind="alert",
        type=str(row.data.get("event")),
        state=standing.state,  # type: ignore[arg-type]
        ended_by=None if standing.by is None else standing.by.id,
        issued_at=row.occurred_at,
        until=row.until,
        issuer=row.data.get("sender"),
        headline=kept["headline"],
        instruction=kept["instruction"],
        incomplete=tuple(
            name for name, text in kept.items() if text is None and row.data.get(name) is not None
        ),
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
    # Only what the positions show: one position is one sighting, and says nothing
    # of how long the aircraft was there or which way it went.
    if track.circling:
        did = f"circled nearby for about {max(1, round(track.minutes))} min"
    elif track.positions == 1:
        did = "was seen low nearby, once"
    elif track.minutes >= 1:
        did = f"was seen low nearby over about {round(track.minutes)} min"
    else:
        did = "was seen low nearby"
    height = "" if track.lowest_m is None else f", as low as {round(track.lowest_m)} m"
    ends = {track.rows[0].id: track.rows[0], track.rows[-1].id: track.rows[-1]}
    return Entry(
        at=track.first_seen,
        kind="aircraft",
        text=f"{what}{name} {did}{height}.",
        confidence=weakest([row.confidence for row in track.rows]),
        # The first and the last position stand for the track.
        sources=tuple(source_of(row) for row in ends.values()),
        details={
            "icao24": track.icao24,
            "callsign": track.callsign,
            "category": track.category,
            "first_seen": track.first_seen.isoformat(),
            "last_seen": track.last_seen.isoformat(),
            "positions": track.positions,
            "circling": track.circling,
            "low": track.low,
            "nearest_m": round(track.nearest_m),
            "lowest_m": track.lowest_m,
        },
    )


def _taps_entry(near: Sequence[Evidence], more: bool) -> Entry:
    """One line for all the taps from near an incident, oldest first in `near`.
    `more` says there were more of them than were read."""
    count = len(near)
    return Entry(
        at=near[0].occurred_at,
        kind="taps",
        text=f'{_taps_words(count, more)} of "What\'s that?" came from nearby while this was '
        "going on. Beacon cannot tell whether they are from different people.",
        confidence="low",
        sources=(_taps_source(near[0].occurred_at),),
        details={
            "count": count,
            "at_least": more,
            "last_at": near[-1].occurred_at.isoformat(),
        },
    )


def _signals(
    group: CallGroup,
    official: Sequence[Official],
    closures: Sequence[Sequence[Evidence]],
    states: dict[int, Standing],
    track_list: Sequence[Track],
    near_taps: Sequence[Evidence],
    taps_more: bool,
) -> list[Signal]:
    signals: list[Signal] = []

    def of(rows: Sequence[Evidence]) -> list[Source]:
        return [source_of(row) for row in rows]

    if group.alarm is not None and group.alarm >= 2:
        rows = [row for row in group.rows if row.data.get("alarm") == group.alarm]
        signals.append(_signal("alarm", f"Alarm level {group.alarm} on the radio.", of(rows)))
    evacuating = [row for row in group.rows if "evacuation" in (row.data.get("details") or [])]
    if evacuating:
        signals.append(
            _signal(
                "evacuation_on_radio",
                f"Crews mention an evacuation. {_NOT_AN_ORDER}",
                of(evacuating),
            )
        )
    if len(group.named) >= MANY_UNITS:
        # The calls that named a unit for the first time.
        seen: set[str] = set()
        naming = []
        for row in group.rows:
            units = {unit for unit in row.data.get("units") or [] if isinstance(unit, str)}
            if units - seen:
                naming.append(row)
            seen |= units
        signals.append(
            _signal("many_units", f"{len(group.named)} units named on the radio.", of(naming))
        )
    orders = [
        item
        for item in official
        if item.kind == "notice"
        and item.type in ORDERS - {"all_clear"}
        and item.state == "published"
    ]
    if orders:
        signals.append(
            _signal(
                "official_order",
                "An authority published an order that no notice Beacon has read lifts. " + _READING,
                [item.source for item in orders],
            )
        )
    alerts = [item for item in official if item.kind == "alert" and item.state == "in_force"]
    if alerts:
        signals.append(
            _signal(
                "official_alert",
                "An official alert whose area holds this place is in force.",
                [item.source for item in alerts],
            )
        )
    closed = [
        versions[-1]
        for versions in closures
        if versions[-1].data.get("closure") is True and states[versions[-1].id].in_force
    ]
    if closed:
        signals.append(_signal("closure", "511 lists a road closed.", of(closed)))
    circling = [track for track in track_list if track.circling]
    if circling:
        signals.append(
            _signal(
                "aircraft_circling",
                "An aircraft circled nearby.",
                of([track.rows[0] for track in circling]),
            )
        )
    if len(near_taps) >= MANY_TAPS:
        signals.append(
            _signal(
                "taps",
                f"{_taps_words(len(near_taps), taps_more)} from nearby.",
                [_taps_source(near_taps[0].occurred_at)],
            )
        )
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
) -> tuple[list[Evidence], list[Evidence]]:
    """The alert messages that apply to `point` while the incident was going on,
    and the later messages that update or cancel them.

    A message applies when the area it was drawn for holds the point, holes left
    out, and its span touches the incident's. Being near the middle of an area is
    not being in it. A message that revises one of those is handed back apart: it
    belongs on the timeline, and whether it applies here itself is not known."""
    if point is None:
        return [], []
    found: list[Evidence] = []
    for row in alerts:
        if "change" in row.data or row.data.get("message_type") == "Cancel":
            continue
        if not _inside(row.data.get("geometry"), point):
            continue
        if row.occurred_at <= end and (row.until is None or row.until >= start):
            found.append(row)
    refs = {row.ref for row in found}
    revisions: list[Evidence] = []
    # In the order they were sent, so a revision of a revision is found too.
    for row in sorted(alerts, key=_order):
        named = row.data.get("replaces")
        if "change" in row.data or row.ref in refs or not isinstance(named, list):
            continue
        if refs & {ref for ref in named if isinstance(ref, str)}:
            revisions.append(row)
            refs.add(row.ref)
    return sorted(found, key=_order), revisions


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
    """The notices about an incident: the ones that name one of its streets, and a
    press release matched to it (`_press_matches`). A notice that names no street
    and is not a press release is about nowhere Beacon can tell, and is nobody's."""
    found = []
    for row in notices:
        if row.data.get("notice_type") in (None, "other"):
            # Ember did not take the document for a notice.
            continue
        if not start <= row.occurred_at <= group.last_at + NOTICE_AFTER:
            continue
        if set(notice_streets(row)) & set(streets) or press.get(row.id) == id(group):
            found.append(row)
    return found


def _press_matches(groups: Sequence[CallGroup], notices: Sequence[Evidence]) -> dict[int, int]:
    """Which incident each press release is about: the one incident of its hazard
    in its time, when there is exactly one. A press release's row keeps no streets,
    so there is nothing else to match it by; it orders nothing and lifts nothing,
    so a wrong match adds a line and changes no standing. The incident is named by
    the identity of its group."""
    matches: dict[int, int] = {}
    for row in notices:
        if row.data.get("notice_type") != "press_release":
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


def _inside(geometry: Any, point: Point) -> bool:
    """Whether a GeoJSON polygon or multipolygon holds a point: inside an outer
    ring and in none of that polygon's holes. A ring's positions are longitude first."""
    if not isinstance(geometry, dict):
        return False
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Polygon":
        polygons = [coordinates]
    elif geometry.get("type") == "MultiPolygon":
        polygons = coordinates if isinstance(coordinates, list) else []
    else:
        return False
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            continue
        outer, *holes = polygon
        if _in_ring(outer, point) and not any(_in_ring(hole, point) for hole in holes):
            return True
    return False


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


def _whole(text: object, limit: int) -> str | None:
    """A text a poller stored, or None when it is as long as the poller keeps. It
    may then have been cut, and a cut text can say the opposite of the whole one:
    an instruction without its exception. So it is left out, and the original is
    where to read it."""
    return text if isinstance(text, str) and len(text) < limit else None


def _taps_source(at: datetime) -> Source:
    """The source line of a claim that rests on taps, which names none of them."""
    return Source(
        kind="tap",
        source="app",
        label=_LABELS["app"],
        confidence="low",
        at=at,
        evidence_id=None,
        url=None,
        # A person's tap is nobody's stand-in.
        stand_in=False,
    )


def _taps_words(count: int, more: bool) -> str:
    """How many taps, in words. Taps, and not people: nothing tells one sender
    from another, so three taps can be one person."""
    counted = "1 tap" if count == 1 else f"{count} taps"
    return f"At least {counted}" if more else counted


def _signal(kind: str, text: str, sources: Sequence[Source]) -> Signal:
    """A signal with the sources it rests on, trusted as little as the weakest."""
    return Signal(
        kind=kind,  # type: ignore[arg-type]
        text=text,
        confidence=weakest([source.confidence for source in sources]),
        sources=tuple(sources),
    )
