"""Aircraft over San Francisco, from adsb.lol.

`GET https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}` answers with the aircraft
within `radius` nautical miles of a point (250 at most), in the format readsb writes
and ADS-B Exchange's v2 API made common: an object with `now`, the moment of the
answer in milliseconds, and `ac`, one object for each aircraft, with heights in feet,
speeds in knots, and `seen_pos`, how many seconds before `now` its position is from.
The API and its limits are at https://www.adsb.lol/docs/open-data/api, and the
interactive schema at https://api.adsb.lol/docs.

**What its terms ask, as read on Nov 27, 2026**, and what Beacon does about each:

- The API "is available to everyone", and its data is under the Open Database License
  1.0 (https://opendatacommons.org/licenses/odbl/1-0/). No permission is asked for,
  so no setting stands in for one. An older page of adsb.lol's asks people who use it
  "for production purposes" to get in touch (info@adsb.lol): do that before Beacon is
  run for the public and not for a demo.
- **A notice wherever something made from the data is shown** (ODbL 4.3): that it
  holds information from adsb.lol, available under the ODbL. That is `CREDIT`, which
  `beacon.incident.build.source_of` puts on every row of this source, as 511's credit
  is on 511's, so it goes wherever a line, a signal, a ground, a tool's count, or a
  mark on the map rests on an aircraft's position.
- **The method by which Beacon's copy differs from the database, offered to whoever
  is shown something made from it** (ODbL 4.6 b). Beacon's rows are a derivative
  database: a selection, converted to SI units. The whole method is `parse_aircraft`
  below, in a public repository: positions inside the city's box, of aircraft that are
  airborne below `MAX_ALTITUDE_M`, with an ICAO address, seen in the last
  `MAX_POSITION_AGE_SECONDS`. Beacon hands nobody a copy of its rows, so the
  share-alike terms for conveying a database (4.2, 4.4) are not met with; if it ever
  publishes its aircraft rows, they go out under the ODbL.
- **500 requests a day for each address with no key**, more with a free key, sent as
  `x-api-key` (1,000 a day for one asked for with a form, 7,200 for somebody who
  feeds the network). Over the limit the API answers `429` with `Retry-After`, which
  the runner keeps to. The poller asks no more often than the limit it is told
  allows, less a margin for restarts: every three minutes with no key.

Each position of an aircraft is one row. Only aircraft a person on the street could
notice are kept: airborne and below `MAX_ALTITUDE_M`. An airliner at cruise is not
what anyone taps about, and storing every one would bury the helicopter that is.

A row's `data`, in SI units:

    icao24           the aircraft's 24-bit address, six hex digits, lower case
    callsign         as broadcast, or null
    altitude_m       the height above the ellipsoid when the aircraft gives one,
                     else the barometric height
    speed_mps        over the ground, or null
    track_deg        degrees clockwise from north, or null
    climb_mps        or null
    squawk           or null
    position_source  how the position was had: `adsb`, `adsr`, `tisb`, `adsc`,
                     `mlat`, or `other`
    category         one of `AIRCRAFT_CATEGORIES`, such as `rotorcraft`, or null
"""

import math
from datetime import UTC, datetime
from typing import Any

from beacon.errors import raise_bare
from beacon.evidence import Confidence, EvidenceStore, NewEvidence, Origin, Point
from beacon.ingest.area import SAN_FRANCISCO, Bounds
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.runner import PollReport

SOURCE = "adsblol"
API = "https://api.adsb.lol"

# The words the Open Database License asks for wherever something made from the data
# is shown (4.3), with the license's address in them because a credit has one link,
# which is adsb.lol's.
CREDIT_TEXT = (
    "Contains information from adsb.lol, which is made available here under the "
    "Open Database License (ODbL): https://opendatacommons.org/licenses/odbl/1-0/"
)
CREDIT_URL = "https://www.adsb.lol"

# Above this an aircraft is passing over, not circling a block.
MAX_ALTITUDE_M = 3_000.0
# A position older than this when the answer was made is where the aircraft was,
# not where it is.
MAX_POSITION_AGE_SECONDS = 60.0

# What adsb.lol allows an address with no key, and a key that was asked for with
# its form, in requests a day. A key that allows more is said with `requests_per_day`.
ANONYMOUS_REQUESTS_PER_DAY = 500
KEYED_REQUESTS_PER_DAY = 1_000
MAX_REQUESTS_PER_DAY = 360_000
# The part of a day's allowance the poller plans to use. The rest is for the polls a
# restart makes at once.
_SHARE_OF_ALLOWANCE = 0.96
# However much a key allows, the sky over a city is not asked for more often.
MIN_INTERVAL_SECONDS = 10.0
# The mock scenario has no allowance to keep to, and is asked this often.
MOCK_INTERVAL_SECONDS = 30.0

_MAX_BYTES = 5_000_000
_MAX_RADIUS_NM = 250
_M_PER_NM = 1852.0
_M_PER_FT = 0.3048
_MPS_PER_KNOT = 1852.0 / 3600.0
_MPS_PER_FT_PER_MIN = _M_PER_FT / 60.0
_M_PER_DEG_LAT = 111_320.0

# The ADS-B emitter categories of the `category` field, for the ones worth naming.
_CATEGORIES = {
    "A1": "light",
    "A2": "small",
    "A3": "large",
    "A4": "high_vortex_large",
    "A5": "heavy",
    "A6": "high_performance",
    "A7": "rotorcraft",
    "B1": "glider",
    "B2": "lighter_than_air",
    "B4": "ultralight",
    "B6": "unmanned",
}
# The names a row's `category` can hold, for a reader that hands it on as a tag.
AIRCRAFT_CATEGORIES: tuple[str, ...] = tuple(_CATEGORIES.values())
# How a position was had, from the `type` of the aircraft's best source.
_POSITION_SOURCES = {
    "adsb_icao": "adsb",
    "adsb_icao_nt": "adsb",
    "adsr_icao": "adsr",
    "tisb_icao": "tisb",
    "adsc": "adsc",
    "mlat": "mlat",
}
_KEY_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


class AdsbLolAircraft:
    name = SOURCE

    def __init__(
        self,
        http: SourceHttp,
        *,
        api_key: str | None = None,
        requests_per_day: int | None = None,
        bounds: Bounds = SAN_FRANCISCO,
        interval_seconds: float | None = None,
    ) -> None:
        """`api_key` is an adsb.lol key, sent as `x-api-key`. `requests_per_day` is
        what that key allows when it is not the 1,000 of a key asked for with the
        form; it cannot be said without a key, because with none the limit is
        adsb.lol's 500 for the address. `interval_seconds` is for the mock
        scenario, which has no allowance to keep to and sets its own."""
        if api_key is not None and not (
            0 < len(api_key) <= 200 and set(api_key) <= _KEY_CHARACTERS
        ):
            raise ValueError("An adsb.lol key is letters, digits, hyphens, dots, and underscores.")
        if requests_per_day is not None:
            if api_key is None:
                raise ValueError("Without a key adsb.lol allows an address 500 requests a day.")
            if isinstance(requests_per_day, bool) or not (
                1 <= requests_per_day <= MAX_REQUESTS_PER_DAY
            ):
                raise ValueError("A key's requests a day are a number from 1 to 360,000.")
        self._http = http
        self._api_key = api_key
        self._bounds = bounds
        self._centre, self._radius_nm = _circle_around(bounds)
        allowed = requests_per_day or (
            ANONYMOUS_REQUESTS_PER_DAY if api_key is None else KEYED_REQUESTS_PER_DAY
        )
        self.interval_seconds = interval_seconds or max(
            MIN_INTERVAL_SECONDS, 86_400 / (allowed * _SHARE_OF_ALLOWANCE)
        )

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
            f"{API}/v2/point/{self._centre.lat:.4f}/{self._centre.lon:.4f}/{self._radius_nm}",
            source=SOURCE,
            max_bytes=_MAX_BYTES,
            headers={} if self._api_key is None else {"x-api-key": self._api_key},
        )
        aircraft, unreadable = parse_aircraft(read_json(body, SOURCE), self.origin, self._bounds)
        added = store.add(aircraft, now=now)
        return PollReport(added=len(added), unreadable=unreadable)


def parse_aircraft(
    payload: Any, origin: Origin, bounds: Bounds = SAN_FRANCISCO
) -> tuple[list[NewEvidence], int]:
    """The aircraft of a `/v2/point` answer that are inside `bounds` and airborne
    below `MAX_ALTITUDE_M`, and how many of its entries could not be read. An
    aircraft with no position, on the ground, of unknown height, above the ceiling,
    outside the box, with a position that is old, or under an address that is not an
    ICAO one is passed over, and is not counted: it was read.

    Raises SourceError when the answer is not an object with the moment it was made
    and a list of aircraft. An error can arrive as an object too, and is not an
    empty sky."""
    listed = payload.get("ac") if isinstance(payload, dict) else None
    made_ms = payload.get("now") if isinstance(payload, dict) else None
    if not isinstance(listed, list) or not _is_number(made_ms) or made_ms <= 0:
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    aircraft: list[NewEvidence] = []
    unreadable = 0
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("hex"), str):
            unreadable += 1
            continue
        found = _aircraft(entry, made_ms / 1000, origin, bounds)
        if found is not None:
            aircraft.append(found)
    return aircraft, unreadable


def _aircraft(
    entry: dict[str, Any], made: float, origin: Origin, bounds: Bounds
) -> NewEvidence | None:
    """One row, or None for an aircraft that is not kept."""
    address = entry["hex"].lower()
    # An address that begins with `~` is not the aircraft's own: one receiver's
    # name for a track, which the next answer may give another.
    if len(address) != 6 or any(digit not in "0123456789abcdef" for digit in address):
        return None
    lat, lon, age = entry.get("lat"), entry.get("lon"), entry.get("seen_pos")
    if not _is_number(lat) or not _is_number(lon) or not _is_number(age):
        return None
    if not 0 <= age <= MAX_POSITION_AGE_SECONDS:
        return None
    # `alt_baro` is the word "ground" for an aircraft that says it is on it. The
    # height above the ellipsoid is nearer the height above the street than the
    # barometric one, which moves with the weather.
    barometric, geometric = entry.get("alt_baro"), entry.get("alt_geom")
    feet = geometric if _is_number(geometric) else barometric
    if barometric == "ground" or not _is_number(feet):
        return None
    altitude_m = feet * _M_PER_FT
    if altitude_m >= MAX_ALTITUDE_M:
        return None
    try:
        point = Point(lat, lon)
        seen = datetime.fromtimestamp(made - age, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    if not bounds.holds(point):
        return None
    position_source = _POSITION_SOURCES.get(entry.get("type"), "other")
    # A position the aircraft broadcast itself is better than one worked out from
    # when its signal reached several receivers, or relayed by a ground station.
    confidence: Confidence = "high" if position_source == "adsb" else "medium"
    callsign, squawk, category = entry.get("flight"), entry.get("squawk"), entry.get("category")
    return NewEvidence(
        kind="aircraft",
        source=SOURCE,
        ref=f"{address}@{int(seen.timestamp())}",
        occurred_at=seen,
        point=point,
        confidence=confidence,
        origin=origin,
        data={
            "icao24": address,
            "callsign": callsign.strip() or None if isinstance(callsign, str) else None,
            "altitude_m": round(altitude_m, 1),
            "speed_mps": _scaled(entry.get("gs"), _MPS_PER_KNOT),
            # Degrees clockwise from north.
            "track_deg": entry["track"] if _is_number(entry.get("track")) else None,
            "climb_mps": _scaled(
                entry["geom_rate"]
                if _is_number(entry.get("geom_rate"))
                else entry.get("baro_rate"),
                _MPS_PER_FT_PER_MIN,
            ),
            "squawk": squawk if isinstance(squawk, str) and len(squawk) <= 4 else None,
            "position_source": position_source,
            "category": _CATEGORIES.get(category) if isinstance(category, str) else None,
        },
    )


def _circle_around(bounds: Bounds) -> tuple[Point, int]:
    """The middle of a box, and the whole nautical miles from it that reach every
    corner: adsb.lol is asked for a circle, and the box is cut from it."""
    centre = Point((bounds.south + bounds.north) / 2, (bounds.west + bounds.east) / 2)
    north_m = (bounds.north - bounds.south) / 2 * _M_PER_DEG_LAT
    # A box is widest at its edge nearer the equator.
    widest = min(abs(bounds.south), abs(bounds.north))
    east_m = (bounds.east - bounds.west) / 2 * _M_PER_DEG_LAT * math.cos(math.radians(widest))
    radius_nm = math.ceil(math.hypot(north_m, east_m) / _M_PER_NM)
    if radius_nm > _MAX_RADIUS_NM:
        raise ValueError("adsb.lol answers for at most 250 nautical miles around a point.")
    return centre, radius_nm


def _scaled(value: Any, factor: float) -> float | None:
    return round(value * factor, 2) if _is_number(value) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
