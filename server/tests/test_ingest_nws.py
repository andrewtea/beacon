"""The NWS alerts poller, against hand-written answers in the API's format."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.evidence import EvidenceStore
from beacon.ingest.http import SourceError, SourceHttp
from beacon.ingest.nws import NwsAlerts, parse_alerts

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)


def minutes(count: float) -> datetime:
    return NOON + timedelta(minutes=count)


def feature(name: str, **changes: Any) -> dict[str, Any]:
    """An alert as `/alerts/active` sends it, with the fields Beacon reads."""
    properties = {
        "id": f"urn:oid:{name}",
        "areaDesc": "San Francisco",
        "geocode": {"SAME": ["006075"], "UGC": ["CAZ006", "not a zone"]},
        "references": [],
        # Pacific time, as NWS San Francisco sends it: 11:50 is 19:50 UTC.
        "sent": "2026-11-27T11:50:00-08:00",
        "onset": "2026-11-27T12:00:00-08:00",
        "expires": "2026-11-27T16:00:00-08:00",
        "ends": "2026-11-27T22:00:00-08:00",
        "status": "Actual",
        "messageType": "Alert",
        "severity": "Moderate",
        "certainty": "Likely",
        "urgency": "Expected",
        "event": "Wind Advisory",
        "senderName": "NWS San Francisco CA",
        "headline": "Wind Advisory until 10 PM",
        "description": "West winds 20 to 30 mph.",
        "instruction": "Secure outdoor objects.",
        "response": "Execute",
    }
    geometry = changes.pop("geometry", None)
    return {
        "id": f"https://api.weather.gov/alerts/urn:oid:{name}",
        "type": "Feature",
        "geometry": geometry,
        "properties": properties | changes,
    }


def collection(*features: Any) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


class Feed:
    """Stands in for api.weather.gov: answers with whatever `listed` holds."""

    def __init__(self) -> None:
        self.listed: list[Any] = []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=collection(*self.listed))


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "mock")


@pytest.fixture
def feed() -> Feed:
    return Feed()


@pytest.fixture
def poller(feed: Feed) -> NwsAlerts:
    return NwsAlerts(SourceHttp(httpx2.MockTransport(feed)))


def refs(store: EvidenceStore) -> list[str]:
    return [row.ref for row in store.search()]


def test_the_poller_asks_for_real_alerts_in_san_francisco_and_says_who_it_is(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    report = poller.poll(store, NOON)

    (request,) = feed.requests
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "api.weather.gov",
        "/alerts/active",
    )
    assert dict(request.url.params) == {"zone": "CAZ006,CAC075", "status": "actual"}
    assert request.headers["user-agent"].startswith("Beacon (https://github.com/andrewtea/beacon")
    assert (report.added, report.unreadable) == (0, 0)


def test_an_alert_becomes_an_official_row_with_its_times_and_text(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("wind.1")]

    report = poller.poll(store, NOON)

    (alert,) = store.search()
    assert report.added == 1
    assert (alert.kind, alert.source, alert.ref) == ("alert", "nws", "urn:oid:wind.1")
    assert alert.url == "https://api.weather.gov/alerts/urn:oid:wind.1"
    assert alert.occurred_at == datetime(2026, 11, 27, 19, 50, tzinfo=UTC)
    # When the hazard ends (06:00 UTC the next day), not when the message expires.
    assert alert.until == datetime(2026, 11, 28, 6, 0, tzinfo=UTC)
    assert (alert.point, alert.place) == (None, "San Francisco")
    # The poller was given a stand-in for the network, so the row is a stand-in's.
    assert (alert.confidence, alert.origin, alert.model) == ("official", "mock", None)
    assert alert.data == {
        "event": "Wind Advisory",
        "message_type": "Alert",
        "severity": "Moderate",
        "urgency": "Expected",
        "certainty": "Likely",
        "response": "Execute",
        "sender": "NWS San Francisco CA",
        "headline": "Wind Advisory until 10 PM",
        "description": "West winds 20 to 30 mph.",
        "instruction": "Secure outdoor objects.",
        "onset": "2026-11-27T12:00:00-08:00",
        "expires": "2026-11-27T16:00:00-08:00",
        "ends": "2026-11-27T22:00:00-08:00",
        "replaces": [],
        "zones": ["CAZ006"],
        "geometry": None,
    }


def test_an_alert_drawn_for_an_area_is_placed_at_its_middle() -> None:
    polygon = {
        "type": "Polygon",
        # Longitude first. The box runs 37.774 to 37.780 north and 122.429 to 122.418 west.
        "coordinates": [
            [
                [-122.429, 37.774],
                [-122.418, 37.774],
                [-122.418, 37.780],
                [-122.429, 37.780],
                [-122.429, 37.774],
            ]
        ],
    }

    (alert,), unreadable = parse_alerts(
        collection(feature("shelter.1", geometry=polygon, ends=None)), "mock"
    )

    assert unreadable == 0
    assert (alert.point.lat, alert.point.lon) == pytest.approx((37.777, -122.4235))
    assert alert.data["geometry"] == polygon
    # With no end of the hazard, the message's own expiry is when it stops applying.
    assert alert.until == datetime(2026, 11, 28, 0, 0, tzinfo=UTC)
    assert alert.origin == "mock"


def test_a_message_that_is_not_an_actual_alert_is_left_out() -> None:
    alerts, unreadable = parse_alerts(
        collection(
            feature("test.1", status="Test"),
            feature("exercise.1", status="Exercise"),
            feature("system.1", status="System"),
            feature("draft.1", status="Draft"),
            feature("real.1"),
        ),
        "live",
    )

    assert [alert.ref for alert in alerts] == ["urn:oid:real.1"]
    assert unreadable == 0


def test_a_message_whose_status_cannot_be_told_is_unreadable_and_not_left_out() -> None:
    without = feature("no-status.1")
    del without["properties"]["status"]

    alerts, unreadable = parse_alerts(
        collection(without, feature("odd.1", status="Actuel"), feature("null.1", status=None)),
        "live",
    )

    # Any of them may be a real alert, so the answer was not read in full.
    assert (alerts, unreadable) == ([], 3)


def test_an_alert_beacon_cannot_read_is_counted_and_the_rest_are_kept() -> None:
    alerts, unreadable = parse_alerts(
        collection(
            feature("no-time.1", sent="yesterday"),
            # A time with no zone could be any of 24 moments.
            feature("no-zone.1", sent="2026-11-27T11:50:00"),
            feature("no-event.1", event=""),
            "not a feature",
            {"type": "Feature", "properties": None},
            feature("fine.1", severity="Catastrophic", references="none", geocode=None),
        ),
        "live",
    )

    assert unreadable == 5
    (fine,) = alerts
    # A word outside the ones NWS documents is not kept.
    assert fine.data["severity"] is None
    assert (fine.data["replaces"], fine.data["zones"]) == ([], [])


@pytest.mark.parametrize("payload", [[], {"features": None}, {"title": "no features"}, "text"])
def test_an_answer_that_is_not_a_feature_collection_is_an_error(payload: Any) -> None:
    with pytest.raises(SourceError, match=r"^nws sent a reply Beacon cannot read\.$"):
        parse_alerts(payload, "live")


def test_an_alert_that_stays_listed_is_stored_once(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("wind.1")]

    first = poller.poll(store, NOON)
    second = poller.poll(store, minutes(2))

    assert (first.added, second.added) == (1, 0)
    assert refs(store) == ["urn:oid:wind.1"]


def at(ref: str, change: str, count: float) -> str:
    return f"{ref}#{change}@{minutes(count).isoformat()}"


def test_an_alert_dropped_before_its_end_is_recorded_as_no_longer_listed_after_two_polls(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("wind.1"), feature("shelter.1", event="Shelter In Place Warning")]
    poller.poll(store, NOON)

    feed.listed = [feature("wind.1")]
    once = poller.poll(store, minutes(2))
    # Missing from one answer can be the feed's mistake.
    assert once.added == 0

    twice = poller.poll(store, minutes(4))
    assert twice.added == 1
    gone = store.find("nws", at("urn:oid:shelter.1", "not-listed", 4))
    assert gone is not None
    assert gone.occurred_at == minutes(4)
    # Nobody announced an all-clear. Beacon saw the alert missing, and says no more.
    assert (gone.kind, gone.confidence, gone.until) == ("alert", "medium", None)
    assert gone.data == {
        "change": "not_listed",
        "about": "urn:oid:shelter.1",
        "event": "Shelter In Place Warning",
        "sender": "NWS San Francisco CA",
    }

    # It is said once, and the alert that is still listed is not touched.
    assert poller.poll(store, minutes(6)).added == 0
    assert refs(store) == [
        "urn:oid:wind.1",
        "urn:oid:shelter.1",
        at("urn:oid:shelter.1", "not-listed", 4),
    ]


def test_an_alert_that_is_listed_again_and_dropped_again_has_a_row_for_each_change(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    shelter = feature("shelter.1")
    feed.listed = [shelter]
    poller.poll(store, NOON)
    feed.listed = []
    poller.poll(store, minutes(2))
    poller.poll(store, minutes(4))

    # The same message, under the same id, is back.
    feed.listed = [shelter]
    assert poller.poll(store, minutes(6)).added == 1
    assert poller.poll(store, minutes(8)).added == 0
    back = store.find("nws", at("urn:oid:shelter.1", "listed-again", 6))
    assert back.data["change"] == "listed_again"
    assert back.data["about"] == "urn:oid:shelter.1"
    # NWS lists it, which is NWS speaking, and it runs to the end it gave.
    assert (back.confidence, back.until) == ("official", datetime(2026, 11, 28, 6, 0, tzinfo=UTC))

    feed.listed = []
    poller.poll(store, minutes(10))
    poller.poll(store, minutes(12))

    assert refs(store) == [
        "urn:oid:shelter.1",
        at("urn:oid:shelter.1", "not-listed", 4),
        at("urn:oid:shelter.1", "listed-again", 6),
        at("urn:oid:shelter.1", "not-listed", 12),
    ]


def test_an_alert_listed_twice_without_a_status_is_not_taken_as_gone(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("shelter.1")]
    poller.poll(store, NOON)

    # Still in the answer, with a status Beacon cannot read.
    broken = feature("shelter.1")
    del broken["properties"]["status"]
    feed.listed = [broken]
    for count in (2, 4, 6):
        assert poller.poll(store, minutes(count)).unreadable == 1

    assert refs(store) == ["urn:oid:shelter.1"]


def test_an_alert_that_comes_back_after_one_missed_answer_is_not_taken_as_gone(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("wind.1")]
    poller.poll(store, NOON)
    feed.listed = []
    poller.poll(store, minutes(2))
    feed.listed = [feature("wind.1")]
    poller.poll(store, minutes(4))
    feed.listed = []
    poller.poll(store, minutes(6))

    # Missing twice, but never twice in a row.
    assert refs(store) == ["urn:oid:wind.1"]


def test_an_alert_that_ran_to_its_end_is_not_recorded_as_dropped(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("wind.1")]
    poller.poll(store, NOON)
    feed.listed = []

    # 06:00 UTC the next day is when it said it would end.
    ended = datetime(2026, 11, 28, 6, 0, tzinfo=UTC)
    poller.poll(store, ended)
    poller.poll(store, ended + timedelta(minutes=2))

    assert refs(store) == ["urn:oid:wind.1"]


def test_an_update_is_a_new_row_and_the_message_it_replaces_is_not_taken_as_gone(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("shelter.1")]
    poller.poll(store, NOON)

    feed.listed = [
        feature(
            "shelter.2",
            messageType="Update",
            sent="2026-11-27T12:01:00-08:00",
            references=[{"identifier": "urn:oid:shelter.1", "sent": "2026-11-27T11:50:00-08:00"}],
        )
    ]
    poller.poll(store, minutes(2))
    poller.poll(store, minutes(4))
    poller.poll(store, minutes(6))

    assert refs(store) == ["urn:oid:shelter.1", "urn:oid:shelter.2"]
    update = store.find("nws", "urn:oid:shelter.2")
    assert update.data["message_type"] == "Update"
    assert update.data["replaces"] == ["urn:oid:shelter.1"]

    # The update itself is watched, and its being dropped in its turn is recorded.
    feed.listed = []
    poller.poll(store, minutes(8))
    poller.poll(store, minutes(10))
    assert refs(store)[-1] == at("urn:oid:shelter.2", "not-listed", 10)


def test_a_cancellation_is_a_row_and_its_own_leaving_the_list_is_not_news(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("shelter.1")]
    poller.poll(store, NOON)
    feed.listed = [
        feature(
            "shelter.2",
            messageType="Cancel",
            sent="2026-11-27T12:01:00-08:00",
            references=[{"identifier": "urn:oid:shelter.1"}],
        )
    ]
    poller.poll(store, minutes(2))
    feed.listed = []
    poller.poll(store, minutes(4))
    poller.poll(store, minutes(6))

    assert refs(store) == ["urn:oid:shelter.1", "urn:oid:shelter.2"]
    assert store.find("nws", "urn:oid:shelter.2").data["message_type"] == "Cancel"


def test_nothing_counts_as_missing_from_an_answer_that_was_not_read_in_full(
    store: EvidenceStore, feed: Feed, poller: NwsAlerts
) -> None:
    feed.listed = [feature("shelter.1")]
    poller.poll(store, NOON)

    # The alert may be the one that could not be read.
    feed.listed = [feature("broken.1", sent=None)]
    for count in (2, 4, 6):
        assert poller.poll(store, minutes(count)).unreadable == 1
    assert refs(store) == ["urn:oid:shelter.1"]

    # Read in full again, it has to be missing twice from then on.
    feed.listed = []
    poller.poll(store, minutes(8))
    assert refs(store) == ["urn:oid:shelter.1"]
    poller.poll(store, minutes(10))
    assert refs(store) == ["urn:oid:shelter.1", at("urn:oid:shelter.1", "not-listed", 10)]


def test_a_failed_poll_stores_nothing_and_forgets_nothing(store: EvidenceStore, feed: Feed) -> None:
    feed.listed = [feature("shelter.1")]
    answers = [feed, lambda request: httpx2.Response(503, text="down"), feed, feed]
    poller = NwsAlerts(
        SourceHttp(httpx2.MockTransport(lambda request: answers.pop(0)(request))),
    )
    poller.poll(store, NOON)
    feed.listed = []

    with pytest.raises(SourceError, match=r"^nws answered HTTP 503\.$"):
        poller.poll(store, minutes(2))
    # An outage is not an answer without the alert.
    poller.poll(store, minutes(4))
    assert refs(store) == ["urn:oid:shelter.1"]
    poller.poll(store, minutes(6))
    assert refs(store) == ["urn:oid:shelter.1", at("urn:oid:shelter.1", "not-listed", 6)]
