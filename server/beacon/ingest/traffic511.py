"""Road closures and traffic incidents in San Francisco, from 511 SF Bay.

`GET https://api.511.org/traffic/events` answers with the events of the nine Bay Area
counties in the Open511 format: what happened, how bad, which roads, and where. It
needs an API token (https://511.org/open-data/token), and allows 60 requests an hour
for each token. The format is in the "511 SF Bay Open Data Specification: Traffic
Event and WZDx", version 1.8, and 511's data agreement asks whoever shows the data to
say "data provided by 511.org" beside it.

An `ACTIVE` event can be planned work that has not begun, so the poller asks for the
events in effect now (`in_effect_on=now`) and keeps each one's schedule. The
specification retired the filters by place, so every event of the Bay Area is read,
and the ones outside the city are left out here.

Each version of an event is one row: 511 updates an event in place, and a row is
never changed, so an update is a new row under the event's id and its `updated`
time. 511 does not say when an event is over: when one stops being listed, a row
records that Beacon no longer sees it listed, and another if it is listed again, as
night work is every night (`beacon.ingest.listing`).

An answer comes in pages, and an event counts as no longer listed only when every
page was read and each said plainly where it stood: its `pagination` object repeats
the offset that was asked for, and either names a next page or does not. A page that
says anything else is read for its events, and no further page is asked for. The
poll is then a *partial* one, and so is one that stopped at `_MAX_PAGES`: events on
the pages that were not read are missing from it until a later poll reads them,
nothing counts as no longer listed, and the poll's report says so, which is logged
and shown by `GET /api/sources`. That shape is the specification's and its example's.
No real answer has been read yet, there being no token.

The token goes in the query string, which is where 511 reads it, so the address of a
request is never logged or put in an error (`beacon.ingest.http`).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from beacon.errors import raise_bare
from beacon.evidence import Evidence, EvidenceStore, NewEvidence, Origin
from beacon.ingest.area import SAN_FRANCISCO, Bounds, centre
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.listing import compare_listing, listed_again_row, not_listed_row
from beacon.ingest.runner import PollReport

SOURCE = "511"
# 511's documentation writes the address with http. The token is in the query, so
# Beacon asks over https and nothing else.
URL = "https://api.511.org/traffic/events"

# A token allows 60 requests an hour. One request every two minutes is half of that,
# and a poll that took several pages waits that long for each of them.
SECONDS_PER_REQUEST = 120.0

_MAX_BYTES = 10_000_000
# The specification lets a server cap a page, at no fewer than 500 events.
_PAGE = 500
# The whole Bay Area has a few hundred events in effect. Past this many pages the
# answer is taken as not read in full, and nothing counts as missing from it.
_MAX_PAGES = 4
MAX_HEADLINE = 1_000
# A schedule is kept as 511 sent it when it is no longer than this as JSON.
_MAX_SCHEDULE = 4_000
_EVENT_TYPES = frozenset(
    {"CONSTRUCTION", "SPECIAL_EVENT", "INCIDENT", "WEATHER_CONDITION", "ROAD_CONDITION"}
)
_SEVERITIES = frozenset({"MINOR", "MODERATE", "MAJOR", "SEVERE", "UNKNOWN"})
# 511's own extension fields say when every lane of a road is shut.
_ALL_LANES = "all lanes"
_SHUT = frozenset({"blocked", "closed", "remain closed", "remains closed"})


class Traffic511:
    name = SOURCE

    def __init__(
        self,
        http: SourceHttp,
        *,
        api_key: str,
        bounds: Bounds = SAN_FRANCISCO,
    ) -> None:
        if not api_key:
            raise ValueError("511 needs an API token.")
        self._http = http
        self._api_key = api_key
        self._bounds = bounds
        # How long to wait before the next poll. It is set after each poll from the
        # number of requests the poll made, so the quota is kept by requests.
        self.interval_seconds = SECONDS_PER_REQUEST

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is where its client's answers
        come from and cannot be set: what a stand-in for the network answered is a
        stand-in's, whoever asks."""
        return self._http.origin

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        events: list[NewEvidence] = []
        unreadable = 0
        offset = 0
        complete = False
        requests = 0
        try:
            for _page in range(_MAX_PAGES):
                requests += 1
                body = fetch(
                    self._http,
                    "GET",
                    URL,
                    source=SOURCE,
                    max_bytes=_MAX_BYTES,
                    params={
                        "api_key": self._api_key,
                        "format": "json",
                        # Only what is happening now, not work planned for later.
                        "in_effect_on": "now",
                        "limit": str(_PAGE),
                        "offset": str(offset),
                    },
                )
                page = parse_events(
                    read_json(body, SOURCE), self.origin, self._bounds, offset=offset
                )
                events += page.events
                unreadable += page.unreadable
                if page.following == "none":
                    complete = True
                    break
                if page.following == "unclear" or page.count == 0:
                    # The page does not say where it stands, or holds nothing and
                    # says another follows. Its events are kept, and the answer is
                    # not taken as the whole list.
                    break
                offset += page.count
        finally:
            self.interval_seconds = SECONDS_PER_REQUEST * requests
        listing = compare_listing(
            store,
            poller=self.name,
            source=SOURCE,
            listed={event.data["event_id"]: event.ref for event in events},
            complete=complete and unreadable == 0,
            now=now,
            origin=self.origin,
        )
        changes = [_change(row, now, "not_listed", listing.origin) for row in listing.gone]
        changes += [_change(row, now, "listed_again", listing.origin) for row in listing.back]
        added = store.add([*events, *changes], state=(self.name, listing.state), now=now)
        return PollReport(added=len(added), unreadable=unreadable, partial=not complete)


@dataclass(frozen=True)
class Page:
    """One page of an events answer."""

    # The active events inside the bounds.
    events: list[NewEvidence]
    unreadable: int
    # How many events the page held, read or not, which is how far to move on.
    count: int
    # Whether the answer says another page follows, says none does, or does not say
    # either in a way Beacon can rely on.
    following: Literal["more", "none", "unclear"]


def parse_events(
    payload: Any, origin: Origin, bounds: Bounds = SAN_FRANCISCO, *, offset: int = 0
) -> Page:
    """The active events of an answer that lie inside `bounds`, how many events
    could not be read, and what the answer says follows it. An event outside the
    bounds, or an archived one, is left out and not counted. An event with no
    status, or one the specification does not have, is counted as unreadable: it
    may be an active event. `offset` is the offset the page was asked for.

    Raises SourceError when the answer is not an events object."""
    listed = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    events: list[NewEvidence] = []
    unreadable = 0
    for item in listed:
        status = item.get("status") if isinstance(item, dict) else None
        if status == "ARCHIVED":
            continue
        event = _event(item, origin) if status == "ACTIVE" else None
        if event is None or event.point is None:
            # Without a position there is no telling whether it is in the city.
            unreadable += 1
        elif bounds.holds(event.point):
            events.append(event)
    return Page(events, unreadable, len(listed), _following(payload.get("pagination"), offset))


def _following(pagination: Any, offset: int) -> Literal["more", "none", "unclear"]:
    """What a page's `pagination` object says comes after it. It is relied on only
    when it is an object that repeats the offset that was asked for, and whose
    `next_url` is an address or is absent. The address itself is never asked:
    Beacon moves on by its own count."""
    if not isinstance(pagination, dict):
        return "unclear"
    at = pagination.get("offset")
    # An offset written as text is read as the number it is.
    if isinstance(at, str) and at.isascii() and at.isdigit():
        at = int(at)
    if isinstance(at, bool) or not isinstance(at, int) or at != offset:
        return "unclear"
    next_url = pagination.get("next_url")
    if next_url is None:
        return "none"
    return "more" if isinstance(next_url, str) and next_url.strip() else "unclear"


def _event(item: dict[str, Any], origin: Origin) -> NewEvidence | None:
    event_id = _text(item.get("id"), 200)
    headline = _text(item.get("headline"), MAX_HEADLINE)
    created = _time(item.get("created"))
    updated = _time(item.get("updated")) or created
    if event_id is None or headline is None or updated is None:
        return None
    geography = item.get("geography")
    listed_roads = item.get("roads")
    roads = (
        [_road(road) for road in listed_roads if isinstance(road, dict)]
        if (isinstance(listed_roads, list))
        else []
    )
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
            "closure": any(road["closed"] for road in roads),
            "areas": [
                name
                for area in (areas if isinstance(areas, list) else [])
                if isinstance(area, dict) and (name := _text(area.get("name"), 100)) is not None
            ],
            # When the event is in effect, as 511 sent it. The specification names
            # the field `schedule` and its own example names it `schedules`.
            "schedule": _schedule(item.get("schedule", item.get("schedules"))),
            "geometry": geography if centre(geography) is not None else None,
        },
    )


def _road(road: dict[str, Any]) -> dict[str, Any]:
    state = _text(road.get("state"), 50)
    lane_type = _text(road.get("+lane_type"), 50)
    lane_status = _text(road.get("+lane_status"), 50)
    all_lanes_shut = (lane_type or "").lower() == _ALL_LANES and (
        lane_status or ""
    ).lower() in _SHUT
    return {
        "name": _text(road.get("name"), 200),
        "from": _text(road.get("from"), 200),
        "to": _text(road.get("to"), 200),
        "direction": _text(road.get("direction"), 50),
        "state": state,
        "lane_type": lane_type,
        "lane_status": lane_status,
        # 511 says a road is closed by its state, or by all of its lanes being shut.
        "closed": (state or "").upper() == "CLOSED" or all_lanes_shut,
    }


def _schedule(value: Any) -> Any:
    if not isinstance(value, dict | list):
        return None
    try:
        size = len(json.dumps(value))
    except (TypeError, ValueError):
        return None
    return value if size <= _MAX_SCHEDULE else None


def _place(roads: list[dict[str, Any]]) -> str | None:
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


def _change(event: Evidence, now: datetime, change: str, origin: Origin) -> NewEvidence:
    """The row that says an event is no longer listed, or is listed again after
    that. Beacon saw it; 511 did not announce it."""
    fields = (not_listed_row if change == "not_listed" else listed_again_row)(event, now, origin)
    return NewEvidence(
        **fields,
        data={
            "change": change,
            # The reference of the last version of the event that was listed.
            "about": event.ref,
            "event_id": event.data.get("event_id"),
            "headline": event.data.get("headline"),
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
