"""National Weather Service alerts for San Francisco.

`GET https://api.weather.gov/alerts/active` answers with the alerts in force as a
GeoJSON feature collection. It carries weather alerts and the civil messages NWS
relays for local authorities, such as a shelter-in-place warning. The API needs no
key, only a User-Agent that says who is asking, and its data is public domain.

Each alert message is one row. An update is a new message with a new id that names
the one it replaces, so it is a new row. NWS can cancel an alert with a `Cancel`
message, which is a row like any other and as official. Mostly it does not, and an
alert that is dropped is just gone: when one stops being listed before the time it
said it would end, a row records that Beacon no longer sees it listed, and another
if it is listed again (`beacon.ingest.listing`).
"""

from datetime import datetime
from typing import Any

from beacon.errors import raise_bare
from beacon.evidence import Evidence, EvidenceStore, NewEvidence, Origin
from beacon.ingest.area import centre
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.listing import compare_listing, listed_again_row, not_listed_row
from beacon.ingest.runner import PollReport

SOURCE = "nws"
URL = "https://api.weather.gov/alerts/active"
# NWS asks every client to say who it is, and blocks the ones that do not.
USER_AGENT = "Beacon (https://github.com/andrewtea/beacon)"
# San Francisco's forecast zone and its county, which alerts are issued for. Both are
# named "San Francisco" by https://api.weather.gov/zones, checked 2026-11-26.
SAN_FRANCISCO_ZONES = ("CAZ006", "CAC075")

_MAX_BYTES = 5_000_000
# An alert's text is kept whole up to this length, which is longer than NWS writes.
# A stored text of this length may have been cut, and a reader that relays it as the
# authority's words leaves such a text out (`beacon.incident.build`).
MAX_TEXT = 8_000
MAX_HEADLINE = 500
_SEVERITIES = frozenset({"Extreme", "Severe", "Moderate", "Minor", "Unknown"})
_URGENCIES = frozenset({"Immediate", "Expected", "Future", "Past", "Unknown"})
_CERTAINTIES = frozenset({"Observed", "Likely", "Possible", "Unlikely", "Unknown"})
_MESSAGE_TYPES = frozenset({"Alert", "Update", "Cancel"})
# The statuses of a message that is not a real alert.
_NOT_ACTUAL = frozenset({"Exercise", "System", "Test", "Draft"})


class NwsAlerts:
    name = SOURCE
    interval_seconds = 120.0

    def __init__(
        self,
        http: SourceHttp,
        *,
        zones: tuple[str, ...] = SAN_FRANCISCO_ZONES,
    ) -> None:
        self._http = http
        self._zones = zones

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is where its client's answers
        come from and cannot be set: what a stand-in for the network answered is a
        stand-in's, whoever asks."""
        return self._http.origin

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        body = fetch(
            self._http,
            "GET",
            URL,
            source=SOURCE,
            max_bytes=_MAX_BYTES,
            # Only real alerts: a test or an exercise message is never evidence.
            params={"zone": ",".join(self._zones), "status": "actual"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        )
        alerts, unreadable = parse_alerts(read_json(body, SOURCE), self.origin)
        replaced = {ref for alert in alerts for ref in alert.data["replaces"]}
        listing = compare_listing(
            store,
            poller=self.name,
            source=SOURCE,
            # A cancellation is itself the news that an alert ended, so its own
            # leaving the list is not reported.
            listed={
                alert.ref: alert.ref for alert in alerts if alert.data["message_type"] != "Cancel"
            },
            replaced=replaced,
            complete=unreadable == 0,
            now=now,
            origin=self.origin,
        )
        changes = [_change(row, now, "not_listed", listing.origin) for row in listing.gone]
        changes += [_change(row, now, "listed_again", listing.origin) for row in listing.back]
        added = store.add([*alerts, *changes], state=(self.name, listing.state), now=now)
        return PollReport(added=len(added), unreadable=unreadable)


def parse_alerts(payload: Any, origin: Origin) -> tuple[list[NewEvidence], int]:
    """The alerts of an `/alerts/active` answer, and how many of its features could
    not be read. A test, exercise, system, or draft message is left out and not
    counted. A feature with no status, or one NWS does not document, is counted as
    unreadable: it may be a real alert.

    Raises SourceError when the answer is not a feature collection."""
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    alerts: list[NewEvidence] = []
    unreadable = 0
    for feature in features:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        status = properties.get("status") if isinstance(properties, dict) else None
        if isinstance(status, str) and status in _NOT_ACTUAL:
            continue
        alert = _alert(feature, properties, origin) if status == "Actual" else None
        if alert is None:
            unreadable += 1
        else:
            alerts.append(alert)
    return alerts, unreadable


def _alert(
    feature: dict[str, Any], properties: dict[str, Any], origin: Origin
) -> NewEvidence | None:
    ref = _text(properties.get("id"), 300)
    event = _text(properties.get("event"), 200)
    sent = _time(properties.get("sent"))
    if ref is None or event is None or sent is None:
        return None
    ends = _time(properties.get("ends"))
    expires = _time(properties.get("expires"))
    url = _text(feature.get("id"), 500)
    geometry = feature.get("geometry")
    geocode = properties.get("geocode")
    zones = geocode.get("UGC") if isinstance(geocode, dict) else None
    references = properties.get("references")
    return NewEvidence(
        kind="alert",
        source=SOURCE,
        ref=ref,
        url=url if url is not None and url.startswith("https://") else None,
        occurred_at=sent,
        # `ends` is when the hazard is over, and `expires` when the message is.
        until=ends or expires,
        point=centre(geometry),
        place=_text(properties.get("areaDesc"), 500),
        confidence="official",
        origin=origin,
        data={
            "event": event,
            "message_type": _one_of(properties.get("messageType"), _MESSAGE_TYPES),
            "severity": _one_of(properties.get("severity"), _SEVERITIES),
            "urgency": _one_of(properties.get("urgency"), _URGENCIES),
            "certainty": _one_of(properties.get("certainty"), _CERTAINTIES),
            "response": _text(properties.get("response"), 50),
            "sender": _text(properties.get("senderName"), 200),
            "headline": _text(properties.get("headline"), MAX_HEADLINE),
            "description": _text(properties.get("description"), MAX_TEXT),
            "instruction": _text(properties.get("instruction"), MAX_TEXT),
            "onset": _iso(_time(properties.get("onset"))),
            "expires": _iso(expires),
            "ends": _iso(ends),
            # The ids of the earlier messages this one updates or cancels.
            "replaces": [
                identifier
                for reference in (references if isinstance(references, list) else [])
                if isinstance(reference, dict)
                and (identifier := _text(reference.get("identifier"), 300)) is not None
            ],
            "zones": [
                zone for zone in (zones if isinstance(zones, list) else []) if _is_zone(zone)
            ],
            # The area the alert was drawn for, when it was drawn for less than a zone.
            "geometry": geometry if centre(geometry) is not None else None,
        },
    )


def _change(alert: Evidence, now: datetime, change: str, origin: Origin) -> NewEvidence:
    """The row that says an alert stopped being listed before it was due to end, or
    is listed again after that. Beacon saw it; NWS did not announce it."""
    fields = (not_listed_row if change == "not_listed" else listed_again_row)(alert, now, origin)
    return NewEvidence(
        **fields,
        url=alert.url,
        data={
            "change": change,
            # The reference of the alert this is about.
            "about": alert.ref,
            "event": alert.data.get("event"),
            "sender": alert.data.get("sender"),
        },
    )


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _one_of(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _is_zone(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 6 and value.isalnum()


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()
