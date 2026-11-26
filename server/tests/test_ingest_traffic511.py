"""The 511 traffic events poller, against hand-written answers in the Open511 format."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.evidence import EvidenceStore
from beacon.ingest.http import SourceError, http_client
from beacon.ingest.traffic511 import Traffic511, parse_events

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)


def minutes(count: float) -> datetime:
    return NOON + timedelta(minutes=count)


def event(number: int, **changes: Any) -> dict[str, Any]:
    """An event as `/traffic/events` sends it: Hayes St closed for a block."""
    fields: dict[str, Any] = {
        "url": f"/traffic/events/511.org/{number}",
        "jurisdiction_url": "http://api.511.org/jurisdictions/511.org/",
        "id": f"511.org/{number}",
        "status": "ACTIVE",
        "headline": "SFFD : Fire department activity on Hayes St. All lanes closed.",
        "event_type": "INCIDENT",
        "event_subtypes": ["Fire", 7, ""],
        "severity": "MAJOR",
        "created": "2026-11-27T19:50:00Z",
        "updated": "2026-11-27T19:55:00Z",
        # Longitude first: a line along Hayes St from Gough St toward Octavia St.
        "geography": {
            "type": "LineString",
            "coordinates": [[-122.4229, 37.7768], [-122.4244, 37.7766]],
        },
        "roads": [
            {
                "name": "Hayes St",
                "from": "Gough St",
                "to": "Octavia St",
                "direction": "",
                "state": "CLOSED",
            }
        ],
        "areas": [{"name": "San Francisco", "id": "geonames.org/5391959"}],
        "schedules": [{"start_date": "2026-11-27"}],
    }
    return {name: value for name, value in (fields | changes).items() if value is not MISSING}


MISSING = object()


class Feed:
    """Stands in for api.511.org: pages through whatever `listed` holds."""

    def __init__(self) -> None:
        self.listed: list[Any] = []
        self.requests: list[httpx2.Request] = []
        # The most events the server puts on a page, whatever limit is asked for.
        self.page = 500
        # A server that says another page follows whatever it has sent.
        self.always_more = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.params.get("api_key") != "token-abc":
            return httpx2.Response(401, text=f"Bad key in {request.url}")
        offset = int(request.url.params["offset"])
        size = min(self.page, int(request.url.params["limit"]))
        answer: dict[str, Any] = {
            "events": self.listed[offset : offset + size],
            "pagination": {"offset": offset},
        }
        if offset + size < len(self.listed) or self.always_more:
            answer["pagination"]["next_url"] = f"/traffic/events/?offset={offset + size}"
        # 511 begins its replies with a byte order mark.
        return httpx2.Response(
            200, content=b"\xef\xbb\xbf" + httpx2.Response(200, json=answer).content
        )


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "live")


@pytest.fixture
def feed() -> Feed:
    return Feed()


@pytest.fixture
def poller(feed: Feed) -> Traffic511:
    return Traffic511(http_client(httpx2.MockTransport(feed)), api_key="token-abc", origin="live")


def refs(store: EvidenceStore) -> list[str]:
    return [row.ref for row in store.search()]


def test_the_poller_asks_for_events_as_json_over_https_with_its_token(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    report = poller.poll(store, NOON)

    (request,) = feed.requests
    assert (request.method, request.url.scheme, request.url.host, request.url.path) == (
        "GET",
        "https",
        "api.511.org",
        "/traffic/events",
    )
    assert dict(request.url.params) == {
        "api_key": "token-abc",
        "format": "json",
        # Without it 511 also lists work that is planned and has not begun.
        "in_effect_on": "now",
        "limit": "500",
        "offset": "0",
    }
    assert (report.added, report.unreadable) == (0, 0)


def test_an_event_becomes_an_official_row_for_that_version_of_it(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    feed.listed = [event(149)]

    report = poller.poll(store, NOON)

    (closure,) = store.search()
    assert report.added == 1
    assert (closure.kind, closure.source) == ("traffic", "511")
    assert closure.ref == "511.org/149@2026-11-27T19:55:00+00:00"
    assert closure.occurred_at == datetime(2026, 11, 27, 19, 55, tzinfo=UTC)
    # The middle of the line.
    assert (closure.point.lat, closure.point.lon) == pytest.approx((37.7767, -122.42365))
    assert closure.place == "Hayes St from Gough St to Octavia St"
    assert (closure.confidence, closure.origin, closure.until, closure.url) == (
        "official",
        "live",
        None,
        None,
    )
    assert closure.data == {
        "event_id": "511.org/149",
        "headline": "SFFD : Fire department activity on Hayes St. All lanes closed.",
        "event_type": "INCIDENT",
        "subtypes": ["Fire"],
        "severity": "MAJOR",
        "created": "2026-11-27T19:50:00+00:00",
        "updated": "2026-11-27T19:55:00+00:00",
        "roads": [
            {
                "name": "Hayes St",
                "from": "Gough St",
                "to": "Octavia St",
                "direction": None,
                "state": "CLOSED",
                "lane_type": None,
                "lane_status": None,
                "closed": True,
            }
        ],
        "closure": True,
        "areas": ["San Francisco"],
        "schedule": [{"start_date": "2026-11-27"}],
        "geometry": {
            "type": "LineString",
            "coordinates": [[-122.4229, 37.7768], [-122.4244, 37.7766]],
        },
    }


def test_only_active_events_inside_the_city_are_kept() -> None:
    oakland = {"type": "Point", "coordinates": [-122.2360, 37.7790]}
    # On the box's western edge, which is inside it.
    edge = {"type": "Point", "coordinates": [-122.53, 37.76]}
    lane = [{"name": "US-101 N", "from": "Cesar Chavez St", "to": "", "state": "SOME_LANES_CLOSED"}]

    page = parse_events(
        {
            "events": [
                event(1, geography=oakland),
                event(2, status="ARCHIVED"),
                event(3, geography=edge, roads=lane),
                event(4, geography={"type": "Point", "coordinates": [-122.5301, 37.76]}),
            ],
            "pagination": {"offset": 0},
        },
        "mock",
    )

    (kept,) = page.events
    assert (kept.data["event_id"], kept.origin) == ("511.org/3", "mock")
    assert kept.place == "US-101 N at Cesar Chavez St"
    # A lane is closed and the road is not.
    assert kept.data["closure"] is False
    assert (page.unreadable, page.count, page.more) == (0, 4, False)


def test_an_event_whose_status_cannot_be_told_is_unreadable_and_not_left_out() -> None:
    page = parse_events(
        {"events": [event(1, status=MISSING), event(2, status="PLANNED"), event(3, status=None)]},
        "live",
    )

    # Any of them may be an active event, so the answer was not read in full.
    assert (page.events, page.unreadable) == ([], 3)


def test_a_road_is_closed_by_its_state_or_by_all_of_its_lanes_being_shut() -> None:
    def road(**fields: str) -> dict[str, str]:
        return {"name": "Hayes St", "from": "Gough St", "to": "Octavia St"} | fields

    page = parse_events(
        {
            "events": [
                # As the specification's own example has it: the state says open,
                # and 511's extension fields say every lane is closed.
                event(
                    1,
                    roads=[
                        road(state="Open", **{"+lane_type": "All Lanes", "+lane_status": "closed"})
                    ],
                ),
                event(2, roads=[road(state="Closed")]),
                event(
                    3,
                    roads=[
                        road(
                            state="SOME_LANES_CLOSED",
                            **{"+lane_type": "Left lane", "+lane_status": "closed"},
                        )
                    ],
                ),
                event(
                    4,
                    roads=[
                        road(
                            state="ALL_LANES_OPEN",
                            **{"+lane_type": "All lanes", "+lane_status": "open"},
                        )
                    ],
                ),
                event(
                    5,
                    severity="SEVERE",
                    schedule={"intervals": ["2026-11-27T19:50/"]},
                    schedules=MISSING,
                ),
            ]
        },
        "live",
    )

    assert [found.data["closure"] for found in page.events] == [True, True, False, False, True]
    assert page.events[0].data["roads"][0]["lane_status"] == "closed"
    # The specification's fourth severity, and its own name for the schedule field.
    assert page.events[4].data["severity"] == "SEVERE"
    assert page.events[4].data["schedule"] == {"intervals": ["2026-11-27T19:50/"]}


def test_an_event_beacon_cannot_read_is_counted_and_the_rest_are_kept() -> None:
    page = parse_events(
        {
            "events": [
                event(1, headline=None),
                event(2, updated="lately", created="earlier"),
                # With no position there is no telling whether it is in the city.
                event(3, geography=None),
                event(4, geography={"type": "Point", "coordinates": ["west", "north"]}),
                "not an event",
                event(5, updated=None, severity="DIRE", roads="Hayes St", areas=None),
            ]
        },
        "live",
    )

    assert page.unreadable == 5
    (fine,) = page.events
    # With no update time, the event's creation is the version.
    assert fine.ref == "511.org/5@2026-11-27T19:50:00+00:00"
    assert (fine.data["severity"], fine.data["roads"], fine.data["areas"]) == (None, [], [])
    assert fine.place is None


@pytest.mark.parametrize("payload", [[], {"events": None}, {"pagination": {}}, "text"])
def test_an_answer_that_is_not_an_events_object_is_an_error(payload: Any) -> None:
    with pytest.raises(SourceError, match=r"^511 sent a reply Beacon cannot read\.$"):
        parse_events(payload, "live")


def test_an_update_to_an_event_is_a_new_row_and_the_old_one_is_kept(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    feed.listed = [event(149)]
    poller.poll(store, NOON)
    again = poller.poll(store, minutes(2))

    feed.listed = [event(149, updated="2026-11-27T20:03:00Z", headline="Closure widened.")]
    updated = poller.poll(store, minutes(4))

    assert (again.added, updated.added) == (0, 1)
    assert refs(store) == [
        "511.org/149@2026-11-27T19:55:00+00:00",
        "511.org/149@2026-11-27T20:03:00+00:00",
    ]
    assert [row.data["headline"] for row in store.search()] == [
        "SFFD : Fire department activity on Hayes St. All lanes closed.",
        "Closure widened.",
    ]


def at(ref: str, change: str, count: float) -> str:
    return f"{ref}#{change}@{minutes(count).isoformat()}"


def test_an_event_no_longer_listed_is_recorded_as_such_after_two_polls_without_it(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    feed.listed = [event(149), event(150)]
    poller.poll(store, NOON)
    feed.listed = [event(149, updated="2026-11-27T20:01:00Z"), event(150)]
    poller.poll(store, minutes(2))

    feed.listed = [event(150)]
    assert poller.poll(store, minutes(4)).added == 0
    assert poller.poll(store, minutes(6)).added == 1

    # It is about the last version that was listed, at the place that version had.
    last = "511.org/149@2026-11-27T20:01:00+00:00"
    gone = store.find("511", at(last, "not-listed", 6))
    assert gone is not None
    assert gone.occurred_at == minutes(6)
    # 511 did not say the road is open. Beacon saw the event missing.
    assert (gone.kind, gone.confidence, gone.place) == (
        "traffic",
        "medium",
        "Hayes St from Gough St to Octavia St",
    )
    assert gone.data == {
        "change": "not_listed",
        "about": last,
        "event_id": "511.org/149",
        "headline": "SFFD : Fire department activity on Hayes St. All lanes closed.",
    }
    assert poller.poll(store, minutes(8)).added == 0
    assert len(refs(store)) == 4


def test_work_that_is_in_effect_each_night_has_a_row_each_time_it_comes_and_goes(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    # With `in_effect_on=now`, 511 lists night work while it is under way and not
    # between nights, and the event is the same version every night.
    work = event(77, event_type="CONSTRUCTION", headline="Overnight roadwork on Fell St.")
    version = "511.org/77@2026-11-27T19:55:00+00:00"
    for night in (0, 1440):
        feed.listed = [work]
        poller.poll(store, minutes(night))
        feed.listed = []
        poller.poll(store, minutes(night + 480))
        poller.poll(store, minutes(night + 482))

    assert refs(store) == [
        version,
        at(version, "not-listed", 482),
        at(version, "listed-again", 1440),
        at(version, "not-listed", 1922),
    ]
    again = store.find("511", at(version, "listed-again", 1440))
    assert (again.confidence, again.data["change"], again.data["about"]) == (
        "official",
        "listed_again",
        version,
    )

    # Back the third night as a new version: the new row says so by itself.
    feed.listed = [event(77, updated="2026-11-29T19:00:00Z")]
    assert poller.poll(store, minutes(2880)).added == 1
    assert refs(store)[-1] == "511.org/77@2026-11-29T19:00:00+00:00"


def test_every_page_is_read_before_anything_counts_as_missing(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    feed.page = 2
    feed.listed = [event(number) for number in range(1, 6)]

    report = poller.poll(store, NOON)

    assert report.added == 5
    assert [request.url.params["offset"] for request in feed.requests] == ["0", "2", "4"]
    # A token allows 60 requests an hour, and the poll made three: the next poll
    # waits long enough that the hour's requests stay at half of that.
    assert poller.interval_seconds == 360
    assert 3600 / poller.interval_seconds * len(feed.requests) <= 30
    # Events on a later page are not taken as gone.
    poller.poll(store, minutes(2))
    poller.poll(store, minutes(4))
    assert len(refs(store)) == 5


def test_an_answer_with_more_pages_than_beacon_reads_clears_nothing(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    feed.listed = [event(1)]
    poller.poll(store, NOON)

    # Nine events at two a page is five pages, and the poller reads four: event 1 is
    # not in the answer, and it may be on the page that was not read.
    feed.page = 2
    feed.listed = [event(number) for number in range(2, 11)]
    for count in (2, 4, 6):
        poller.poll(store, minutes(count))

    assert len(feed.requests) == 1 + 3 * 4
    assert poller.interval_seconds == 480
    assert not [ref for ref in refs(store) if "#not-listed" in ref]

    # Read in full again without it, twice, and it is recorded as gone.
    feed.page = 500
    poller.poll(store, minutes(8))
    assert poller.interval_seconds == 120
    poller.poll(store, minutes(10))
    assert at("511.org/1@2026-11-27T19:55:00+00:00", "not-listed", 10) in refs(store)


def test_an_empty_page_that_says_another_follows_is_not_the_whole_list(
    store: EvidenceStore, feed: Feed, poller: Traffic511
) -> None:
    feed.listed = [event(1)]
    poller.poll(store, NOON)

    # The answer contradicts itself: nothing on the page, and more to come.
    feed.listed = []
    feed.always_more = True
    for count in (2, 4, 6):
        poller.poll(store, minutes(count))

    # One request a poll: the poller does not ask for the same empty page again.
    assert len(feed.requests) == 4
    assert refs(store) == ["511.org/1@2026-11-27T19:55:00+00:00"]


def test_a_failed_poll_is_charged_for_the_requests_it_made(
    store: EvidenceStore, feed: Feed
) -> None:
    feed.page = 1
    feed.listed = [event(1), event(2), event(3)]
    answers = iter([feed, feed, lambda request: httpx2.Response(500)])
    poller = Traffic511(
        http_client(httpx2.MockTransport(lambda request: next(answers)(request))),
        api_key="token-abc",
        origin="live",
    )

    with pytest.raises(SourceError, match="answered HTTP 500"):
        poller.poll(store, NOON)

    assert poller.interval_seconds == 360
    assert refs(store) == []


def test_a_refused_token_fails_the_poll_without_being_repeated(store: EvidenceStore) -> None:
    feed = Feed()
    poller = Traffic511(
        http_client(httpx2.MockTransport(feed)), api_key="wrong-token-xyz", origin="live"
    )

    with pytest.raises(SourceError) as failure:
        poller.poll(store, NOON)

    assert str(failure.value) == "511 answered HTTP 401."
    assert failure.value.__cause__ is None
    assert refs(store) == []


def test_a_poller_without_a_token_cannot_be_made() -> None:
    with pytest.raises(ValueError, match="511 needs an API token"):
        Traffic511(http_client(), api_key="", origin="live")
