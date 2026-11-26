"""Road closures and traffic incidents in San Francisco, from 511 SF Bay.

`GET https://api.511.org/traffic/events` answers with the active events of the nine
Bay Area counties in the Open511 format: what happened, how bad, which roads, and
where. It needs an API token (https://511.org/open-data/token), and allows 60
requests an hour for each token. The format is in the "511 SF Bay Open Data
Specification: Traffic Event and WZDx", version 1.8.

Each version of an event is one row: 511 updates an event in place, and a row is
never changed, so an update is a new row under the event's id and its `updated`
time. When an event stops being listed, a row says it cleared.

The token goes in the query string, which is where 511 reads it, so the address of a
request is never logged or put in an error (`beacon.ingest.http`).
"""

from datetime import datetime
from typing import Any

import httpx2

from beacon.evidence import Evidence, EvidenceStore, NewEvidence, Origin
from beacon.ingest.area import SAN_FRANCISCO, Bounds, centre
from beacon.ingest.http import SourceError, fetch, read_json
from beacon.ingest.listing import no_longer_listed
from beacon.ingest.runner import PollReport

SOURCE = "511"
# 511's documentation writes the address with http. The token is in the query, so
# Beacon asks over https and nothing else.
URL = "https://api.511.org/traffic/events"

_MAX_BYTES = 10_000_000
# The specification lets a server cap a page, at no fewer than 500 events.
_PAGE = 500
# The whole Bay Area has a few hundred active events. Past this many pages the
# answer is taken as not read in full, and nothing counts as missing from it.
_MAX_PAGES = 4
_MAX_TEXT = 1_000
_EVENT_TYPES = frozenset(
    {"CONSTRUCTION", "SPECIAL_EVENT", "INCIDENT", "WEATHER_CONDITION", "ROAD_CONDITION"}
)
_SEVERITIES = frozenset({"MINOR", "MODERATE", "MAJOR", "UNKNOWN"})


class Traffic511:
    name = SOURCE
    # Thirty polls an hour, which is half of what a token allows when every poll
    # is one page.
    interval_seconds = 120.0

    def __init__(
        self,
        http: httpx2.Client,
        *,
        api_key: str,
        origin: Origin,
        bounds: Bounds = SAN_FRANCISCO,
    ) -> None:
        if not api_key:
            raise ValueError("511 needs an API token.")
        self._http = http
        self._api_key = api_key
        self._origin: Origin = origin
        self._bounds = bounds

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        events: list[NewEvidence] = []
        unreadable = 0
        offset = 0
        complete = False
        for _page in range(_MAX_PAGES):
            body = fetch(
                self._http,
                "GET",
                URL,
                source=SOURCE,
                max_bytes=_MAX_BYTES,
                params={
                    "api_key": self._api_key,
                    "format": "json",
                    "limit": str(_PAGE),
                    "offset": str(offset),
                },
            )
            page = parse_events(read_json(body, SOURCE), self._origin, self._bounds)
            events += page.events
            unreadable += page.unreadable
            if not page.more:
                complete = True
                break
            offset += page.count
        gone, state = no_longer_listed(
            store,
            poller=self.name,
            source=SOURCE,
            listed={event.data["event_id"]: event.ref for event in events},
            complete=complete and unreadable == 0,
            now=now,
        )
        cleared = [_cleared(row, now) for row in gone]
        added = store.add([*events, *cleared], state=(self.name, state), now=now)
        return PollReport(added=len(added), unreadable=unreadable)


class Page:
    """One page of an events answer."""

    def __init__(self, events: list[NewEvidence], unreadable: int, count: int, more: bool) -> None:
        # The active events inside the bounds.
        self.events = events
        self.unreadable = unreadable
        # How many events the page held, read or not, which is how far to move on.
        self.count = count
        self.more = more


def parse_events(payload: Any, origin: Origin, bounds: Bounds = SAN_FRANCISCO) -> Page:
    """The active events of an answer that lie inside `bounds`, how many events
    could not be read, and whether the answer says another page follows. An event
    outside the bounds, or one that is not active, is left out and not counted.

    Raises SourceError when the answer is not an events object."""
    listed = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        raise SourceError(SOURCE, "sent a reply Beacon cannot read")
    events: list[NewEvidence] = []
    unreadable = 0
    for item in listed:
        if isinstance(item, dict) and item.get("status") not in (None, "ACTIVE"):
            continue
        event = _event(item, origin) if isinstance(item, dict) else None
        if event is None or event.point is None:
            # Without a position there is no telling whether it is in the city.
            unreadable += 1
        elif bounds.holds(event.point):
            events.append(event)
    pagination = payload.get("pagination")
    next_url = pagination.get("next_url") if isinstance(pagination, dict) else None
    more = isinstance(next_url, str) and bool(next_url) and bool(listed)
    return Page(events, unreadable, len(listed), more)


def _event(item: dict[str, Any], origin: Origin) -> NewEvidence | None:
    event_id = _text(item.get("id"), 200)
    headline = _text(item.get("headline"), _MAX_TEXT)
    created = _time(item.get("created"))
    updated = _time(item.get("updated")) or created
    if event_id is None or headline is None or updated is None:
        return None
    geography = item.get("geography")
    roads = [
        {
            "name": _text(road.get("name"), 200),
            "from": _text(road.get("from"), 200),
            "to": _text(road.get("to"), 200),
            "direction": _text(road.get("direction"), 50),
            "state": _text(road.get("state"), 50),
        }
        for road in (item.get("roads") if isinstance(item.get("roads"), list) else [])
        if isinstance(road, dict)
    ]
    areas = item.get("areas")
    subtypes = item.get("event_subtypes")
    return NewEvidence(
        kind="traffic",
        source=SOURCE,
        # A row for each version of the event.
        ref=f"{event_id}@{updated.isoformat()}",
        occurred_at=updated,
        point=centre(geography),
        place=_place(roads),
        confidence="official",
        origin=origin,
        data={
            "event_id": event_id,
            "headline": headline,
            "event_type": _one_of(item.get("event_type"), _EVENT_TYPES),
            "subtypes": [
                subtype
                for value in (subtypes if isinstance(subtypes, list) else [])
                if (subtype := _text(value, 100)) is not None
            ],
            "severity": _one_of(item.get("severity"), _SEVERITIES),
            "created": None if created is None else created.isoformat(),
            "updated": updated.isoformat(),
            "roads": roads,
            # Whether any road is closed outright, and not only narrowed.
            "closure": any((road["state"] or "").upper() == "CLOSED" for road in roads),
            "areas": [
                name
                for area in (areas if isinstance(areas, list) else [])
                if isinstance(area, dict) and (name := _text(area.get("name"), 100)) is not None
            ],
            "geometry": geography if centre(geography) is not None else None,
        },
    )


def _place(roads: list[dict[str, str | None]]) -> str | None:
    """The first road in the source's words: `Hayes St from Gough St to Octavia St`."""
    for road in roads:
        if road["name"] is None:
            continue
        place = road["name"]
        if road["from"] is not None:
            place += f" from {road['from']}" if road["to"] is not None else f" at {road['from']}"
        if road["from"] is not None and road["to"] is not None:
            place += f" to {road['to']}"
        return place
    return None


def _cleared(event: Evidence, now: datetime) -> NewEvidence:
    """The row that says an event is no longer listed: the road is open again, or the
    incident is over."""
    return NewEvidence(
        kind="traffic",
        source=SOURCE,
        ref=f"{event.ref}#cleared",
        occurred_at=now,
        point=event.point,
        place=event.place,
        confidence="official",
        origin=event.origin,
        data={
            "event_id": event.data.get("event_id"),
            "headline": event.data.get("headline"),
            # The reference of the last version of the event that was listed.
            "clears": event.ref,
        },
    )


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _one_of(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None
