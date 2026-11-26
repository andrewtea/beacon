"""The mock scenario: an invented three-alarm fire in Hayes Valley, San Francisco.

Nothing here happened. The fire, the units, the aircraft, the alerts, and the road
closure are made up, so the repository holds no real radio traffic or recording, and
every row built from them is stored with the origin `mock`. The places are real
streets, because the app has to draw them, and no address finer than a block appears.

The story is told as what each source would have answered at a given moment, in the
source's own format, so the real pollers and parsers run on it unchanged:

    minute  0  a fire is reported on the 400 block of Hayes St
    minute  5  a helicopter arrives and circles until minute 50
    minute  9  511 lists Hayes St closed between Gough St and Octavia St
    minute 16  a shelter-in-place warning for smoke is relayed through NWS
    minute 20  511 widens the closure to Fell St
    minute 40  the warning is updated to a smaller area
    minute 70  the warning is dropped, before the time it said it would end
    minute 80  511 stops listing the closure

A wind advisory is in force throughout, a road-work event elsewhere in the city is
listed throughout, and each source also carries something Beacon should leave out: a
test alert, an event in Oakland, an airliner at cruise.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from beacon.evidence import Point

# The middle of the 400 block of Hayes St.
FIRE = Point(37.7766, -122.4236)

# Where the blocks the story's radio calls name are: the middle of each, to about
# a block. The streets are real and the positions are Beacon's own estimates. They
# stand where a list of the city's blocks would for real radio.
BLOCK_POINTS: tuple[tuple[str, int, float, float], ...] = (
    ("Hayes St", 400, FIRE.lat, FIRE.lon),
    ("Fell St", 400, 37.7757, -122.4234),
    ("Geary Blvd", 5200, 37.7805, -122.4760),
    ("Irving St", 1900, 37.7636, -122.4783),
)

# The words every invented text carries, so that it cannot be mistaken for a real
# one wherever it ends up.
INVENTED = "Invented for Beacon's mock scenario."

# How long the story runs. After it, every source answers with what is left: the
# wind advisory and the road work.
DURATION = timedelta(minutes=95)

_M_PER_DEG_LAT = 111_320.0

# The area the shelter-in-place warning was drawn for, and the smaller one of its
# update. GeoJSON positions are longitude first.
_WARNING_AREA = [
    [-122.4290, 37.7740],
    [-122.4180, 37.7740],
    [-122.4180, 37.7800],
    [-122.4290, 37.7800],
    [-122.4290, 37.7740],
]
_SMALLER_AREA = [
    [-122.4262, 37.7752],
    [-122.4212, 37.7752],
    [-122.4212, 37.7782],
    [-122.4262, 37.7782],
    [-122.4262, 37.7752],
]

_ALERT_ID = "urn:oid:2.49.0.1.840.0.beacon-mock-scenario.{name}"


@dataclass(frozen=True)
class Scenario:
    """The story, begun at `start`. Each method is what a source would answer when
    asked at the moment `at`."""

    start: datetime

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None:
            raise ValueError("The scenario's start must carry a time zone.")

    def minute(self, at: datetime) -> float:
        """Minutes since the story began. Negative before it."""
        return (at - self.start).total_seconds() / 60

    def _at(self, minute: float) -> datetime:
        return self.start + timedelta(minutes=minute)

    # National Weather Service: GET /alerts/active

    def nws_alerts(self, at: datetime) -> dict[str, Any]:
        minute = self.minute(at)
        features = [
            _alert(
                name="wind.001.1",
                event="Wind Advisory",
                sent=self._at(-180),
                ends=self._at(360),
                severity="Moderate",
                urgency="Expected",
                certainty="Likely",
                response="Execute",
                sender="NWS San Francisco CA",
                headline="Wind Advisory until this evening",
                description=(
                    f"{INVENTED} West winds 20 to 30 mph with gusts up to 45 mph across "
                    "San Francisco."
                ),
                instruction="Secure outdoor objects. Use extra caution when driving.",
            ),
            # A test message, which is not evidence of anything.
            _alert(
                name="test.001.1",
                event="Test Message",
                sent=self._at(-30),
                ends=self._at(360),
                status="Test",
                severity="Unknown",
                urgency="Unknown",
                certainty="Unknown",
                response="None",
                sender="NWS San Francisco CA",
                headline="This is a test",
                description=f"{INVENTED} A test of the alert feed.",
                instruction=None,
            ),
        ]
        warning = {
            "event": "Shelter In Place Warning",
            "ends": self._at(120),
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "response": "Shelter",
            "sender": "San Francisco Department of Emergency Management",
            "area": "Hayes Valley, San Francisco",
        }
        if 16 <= minute < 40:
            features.append(
                _alert(
                    name="shelter.001.1",
                    sent=self._at(16),
                    headline="Shelter in place because of smoke from a fire on Hayes St",
                    description=(
                        f"{INVENTED} The San Francisco Fire Department is fighting a "
                        "building fire on the 400 block of Hayes St. Smoke is heavy between "
                        "Franklin St and Laguna St, from Fell St to Grove St."
                    ),
                    instruction=(
                        "If you are in this area, go indoors, close windows and doors, "
                        "and turn off ventilation that draws in outside air."
                    ),
                    polygon=_WARNING_AREA,
                    **warning,
                )
            )
        elif 40 <= minute < 70:
            features.append(
                _alert(
                    name="shelter.001.2",
                    sent=self._at(40),
                    message_type="Update",
                    replaces=[("shelter.001.1", self._at(16))],
                    headline="Shelter in place area reduced as the Hayes St fire is contained",
                    description=(
                        f"{INVENTED} The fire on the 400 block of Hayes St is contained. "
                        "The shelter-in-place area is reduced to Hayes St and the blocks "
                        "beside it, between Gough St and Octavia St."
                    ),
                    instruction=(
                        "If you are in this area, stay indoors with windows and doors closed."
                    ),
                    polygon=_SMALLER_AREA,
                    **warning,
                )
            )
        return {
            "@context": ["https://geojson.org/geojson-ld/geojson-context.jsonld"],
            "type": "FeatureCollection",
            "features": features,
            "title": "Current watches, warnings, and advisories",
            "updated": at.isoformat(),
        }

    # OpenSky Network: GET /api/states/all

    def opensky_states(self, at: datetime) -> dict[str, Any]:
        # OpenSky answers for a time rounded down to five seconds.
        seconds = int(at.timestamp()) // 5 * 5
        minute = (seconds - self.start.timestamp()) / 60
        states = [
            # An airliner at cruise, crossing the city every ten minutes.
            _state(
                "beac03",
                "MOCK303",
                seconds,
                Point(37.75, -122.52 + (seconds % 600) / 600 * 0.16),
                altitude_m=10_500.0,
                speed_mps=230.0,
                track_deg=90.0,
                category=4,
            )
        ]
        helicopter = _helicopter(minute)
        if helicopter is not None:
            point, track = helicopter
            states.append(
                _state(
                    "beac01",
                    "MOCKH1",
                    seconds,
                    point,
                    altitude_m=320.0,
                    speed_mps=28.0,
                    track_deg=track,
                    category=8,
                )
            )
        if 25 <= minute < 29:
            # A light aircraft along the northern waterfront, which has nothing to do
            # with the fire.
            along = (minute - 25) / 4
            states.append(
                _state(
                    "beac02",
                    "MOCK202",
                    seconds,
                    Point(37.812, -122.52 + along * 0.16),
                    altitude_m=610.0,
                    speed_mps=58.0,
                    track_deg=90.0,
                    category=2,
                    # Worked out from several receivers, not broadcast by the aircraft.
                    position_source=2,
                )
            )
        return {"time": seconds, "states": states}

    # 511 SF Bay: GET /traffic/events

    def traffic_events(self, at: datetime) -> dict[str, Any]:
        minute = self.minute(at)
        events = [
            _event(
                "511.org/mock-1001",
                headline=(
                    f"{INVENTED} Caltrans : Road work on Lincoln Way between 19th Ave and "
                    "25th Ave. Right lane closed."
                ),
                event_type="CONSTRUCTION",
                severity="MINOR",
                created=self._at(-600),
                updated=self._at(-600),
                line=[[-122.4770, 37.7655], [-122.4835, 37.7652]],
                roads=[_road("Lincoln Way", "19th Ave", "25th Ave", "SOME_LANES_CLOSED")],
            ),
            # In Oakland, which Beacon does not cover.
            _event(
                "511.org/mock-1003",
                headline=(
                    f"{INVENTED} CHP : Stalled vehicle on I-880 Northbound at 23rd Ave "
                    "(Oakland). Right lane blocked."
                ),
                event_type="INCIDENT",
                severity="MINOR",
                created=self._at(-20),
                updated=self._at(-20),
                line=[[-122.2360, 37.7790]],
                roads=[_road("I-880 N", "23rd Ave", None, "SOME_LANES_CLOSED")],
                area="Alameda",
            ),
        ]
        hayes = _road("Hayes St", "Gough St", "Octavia St", "CLOSED")
        if 9 <= minute < 20:
            events.append(
                _event(
                    "511.org/mock-1002",
                    headline=(
                        f"{INVENTED} SFFD : Fire department activity on Hayes St between "
                        "Gough St and Octavia St. All lanes closed."
                    ),
                    event_type="INCIDENT",
                    severity="MAJOR",
                    created=self._at(9),
                    updated=self._at(9),
                    line=[[-122.4229, 37.7768], [-122.4244, 37.7766]],
                    roads=[hayes],
                )
            )
        elif 20 <= minute < 80:
            events.append(
                _event(
                    "511.org/mock-1002",
                    headline=(
                        f"{INVENTED} SFFD : Fire department activity on Hayes St between "
                        "Gough St and Octavia St, and on Fell St between Gough St and "
                        "Octavia St. All lanes closed. Avoid the area."
                    ),
                    event_type="INCIDENT",
                    severity="MAJOR",
                    created=self._at(9),
                    updated=self._at(20),
                    line=[[-122.4229, 37.7768], [-122.4244, 37.7766], [-122.4242, 37.7757]],
                    roads=[hayes, _road("Fell St", "Gough St", "Octavia St", "CLOSED")],
                )
            )
        return {
            "events": events,
            "pagination": {"offset": 0},
            "meta": {"url": "/traffic/events", "up_url": "/", "version": "v1"},
        }


def _alert(
    *,
    name: str,
    event: str,
    sent: datetime,
    ends: datetime,
    severity: str,
    urgency: str,
    certainty: str,
    response: str,
    sender: str,
    headline: str,
    description: str,
    instruction: str | None,
    status: str = "Actual",
    message_type: str = "Alert",
    replaces: list[tuple[str, datetime]] | None = None,
    polygon: list[list[float]] | None = None,
    area: str = "San Francisco",
) -> dict[str, Any]:
    """One feature of an `/alerts/active` answer, with the properties NWS sends."""
    alert_id = _ALERT_ID.format(name=name)
    return {
        "id": f"https://api.weather.gov/alerts/{alert_id}",
        "type": "Feature",
        "geometry": None if polygon is None else {"type": "Polygon", "coordinates": [polygon]},
        "properties": {
            "@id": f"https://api.weather.gov/alerts/{alert_id}",
            "@type": "wx:Alert",
            "id": alert_id,
            "areaDesc": area,
            "geocode": {"SAME": ["006075"], "UGC": ["CAZ006"]},
            "affectedZones": ["https://api.weather.gov/zones/forecast/CAZ006"],
            "references": [
                {
                    "@id": f"https://api.weather.gov/alerts/{_ALERT_ID.format(name=earlier)}",
                    "identifier": _ALERT_ID.format(name=earlier),
                    "sender": "w-nws.webmaster@noaa.gov",
                    "sent": earlier_sent.isoformat(),
                }
                for earlier, earlier_sent in replaces or []
            ],
            "sent": sent.isoformat(),
            "effective": sent.isoformat(),
            "onset": sent.isoformat(),
            "expires": ends.isoformat(),
            "ends": ends.isoformat(),
            "status": status,
            "messageType": message_type,
            "category": "Met" if polygon is None else "Safety",
            "severity": severity,
            "certainty": certainty,
            "urgency": urgency,
            "event": event,
            "sender": "w-nws.webmaster@noaa.gov",
            "senderName": sender,
            "headline": headline,
            "description": description,
            "instruction": instruction,
            "response": response,
            "parameters": {},
            "scope": "Public",
            "code": "IPAWSv1.0",
            "language": "en-US",
            "web": "http://www.weather.gov",
            "eventCode": {},
        },
    }


def _state(
    icao24: str,
    callsign: str,
    seconds: int,
    point: Point,
    *,
    altitude_m: float,
    speed_mps: float,
    track_deg: float,
    category: int,
    position_source: int = 0,
) -> list[Any]:
    """One state vector, in the order OpenSky documents its fields."""
    return [
        icao24,
        callsign.ljust(8),
        "United States",
        seconds,
        seconds,
        round(point.lon, 5),
        round(point.lat, 5),
        altitude_m - 15.0,  # barometric
        False,
        speed_mps,
        round(track_deg, 1),
        0.0,
        None,
        altitude_m,  # geometric
        None,
        False,
        position_source,
        category,
    ]


def _helicopter(minute: float) -> tuple[Point, float] | None:
    """Where the helicopter is and which way it points: in from the south-east,
    clockwise around the fire, and out again."""
    radius_m = 450.0
    # Where it joins and leaves the circle: due east of the fire, pointing south.
    join = _offset(FIRE, north_m=0.0, east_m=radius_m)
    away = Point(37.7560, -122.3900)
    if 5 <= minute < 7:
        return _between(away, join, (minute - 5) / 2), _bearing(away, join)
    if 7 <= minute < 48:
        # One turn every 100 seconds. The angle is measured clockwise from east.
        angle = math.tau * ((minute - 7) * 60 % 100) / 100
        point = _offset(
            FIRE, north_m=-radius_m * math.sin(angle), east_m=radius_m * math.cos(angle)
        )
        return point, (180.0 + math.degrees(angle)) % 360
    if 48 <= minute < 50:
        return _between(join, away, (minute - 48) / 2), _bearing(join, away)
    return None


def _offset(point: Point, *, north_m: float, east_m: float) -> Point:
    return Point(
        point.lat + north_m / _M_PER_DEG_LAT,
        point.lon + east_m / (_M_PER_DEG_LAT * math.cos(math.radians(point.lat))),
    )


def _between(a: Point, b: Point, share: float) -> Point:
    return Point(a.lat + (b.lat - a.lat) * share, a.lon + (b.lon - a.lon) * share)


def _bearing(a: Point, b: Point) -> float:
    """Degrees clockwise from north, from `a` toward `b`, over a short distance."""
    north = b.lat - a.lat
    east = (b.lon - a.lon) * math.cos(math.radians(a.lat))
    return math.degrees(math.atan2(east, north)) % 360


def _road(name: str, start: str, end: str | None, state: str) -> dict[str, str]:
    return {"name": name, "from": start, "to": end or "", "direction": "", "state": state}


def _event(
    event_id: str,
    *,
    headline: str,
    event_type: str,
    severity: str,
    created: datetime,
    updated: datetime,
    line: list[list[float]],
    roads: list[dict[str, str]],
    area: str = "San Francisco",
) -> dict[str, Any]:
    """One event of a `/traffic/events` answer, in the Open511 shape 511 sends."""
    geography = (
        {"type": "Point", "coordinates": line[0]}
        if len(line) == 1
        else {"type": "LineString", "coordinates": line}
    )
    return {
        "url": f"/traffic/events/{event_id}",
        "jurisdiction_url": "http://api.511.org/jurisdictions/511.org/",
        "id": event_id,
        "status": "ACTIVE",
        "headline": headline,
        "event_type": event_type,
        "event_subtypes": [],
        "severity": severity,
        "created": _zulu(created),
        "updated": _zulu(updated),
        "geography": geography,
        "roads": roads,
        "areas": [{"name": area, "id": "geonames.org/0"}],
        "schedules": [{"start_date": created.date().isoformat()}],
    }


def _zulu(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
