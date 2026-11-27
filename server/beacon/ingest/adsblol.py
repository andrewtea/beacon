"""Aircraft over San Francisco, from adsb.lol.

`GET https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}` answers with the aircraft
within `radius` nautical miles of a point (250 at most), in the format readsb writes
and ADS-B Exchange's v2 API made common: an object with `now`, the moment of the
answer in milliseconds, and `ac`, one object for each aircraft, with heights in feet,
speeds in knots, and `seen_pos`, how many seconds before `now` its position is from.
The API and its limits are at https://www.adsb.lol/docs/open-data/api, and the
interactive schema at https://api.adsb.lol/docs.

**What its terms ask, as read on Nov 27, 2026**, and what Beacon does about each. The
license is the Open Database License 1.0
(https://opendatacommons.org/licenses/odbl/1-0/), and the section numbers are its own:

- The API "is available to everyone". No permission is asked for, so no setting stands
  in for one. An older page of adsb.lol's asks people who use it "for production
  purposes" to get in touch (info@adsb.lol): do that before Beacon is run for the
  public and not for a demo.
- **Beacon's aircraft rows are a Derivative Database, and what it shows from them are
  Produced Works shown to the public**: a line, a signal, a ground, a count, a mark
  on the map. Three things follow, and `docs/aircraft-data.md` is where each is said
  to whoever is shown one:
  - **A notice with each of them** (4.3): that it contains information from adsb.lol,
    made available under the ODbL, with a link to adsb.lol and a link to the license
    of their own. That is `CREDIT_PARTS`, which `beacon.incident.build.credit_of`
    puts on every row adsb.lol answered, and on nothing else: a position of the
    invented scenario contains no information from adsb.lol, and says so instead.
  - **The rows are under the ODbL themselves** (4.4 a and c): a Derivative Database
    is Publicly Used when a Produced Work made from it is, and may then be under no
    other terms. Beacon licenses its aircraft rows under the ODbL 1.0, and says so in
    `docs/aircraft-data.md` and in the notice.
  - **An offer of how the rows differ from adsb.lol's database, to whoever is shown
    something made from them** (4.6 b): the notice links `docs/aircraft-data.md`,
    which is free, is versioned with the code it describes, and gives the whole
    method: what is asked for and how often, what is kept of an answer, how it is
    converted, and how a row is stored. The method is `parse_aircraft` below; keep
    the document in step with it.
  Beacon hands nobody a copy of its rows, so the terms for conveying a database (4.2)
  are not met with. If it ever does, the copy goes out under the ODbL with the
  license's address in it.
- **500 requests a day for each address with no key**, and the same for a key had
  without an account. A key asked for with adsb.lol's form allows 1,000, one for
  somebody who feeds the network 7,200, and the daily window begins at 00:00 UTC. A
  key is sent as `x-api-key`, and what it allows is not in the key, so the poller
  takes any key to allow 500 until it is told more (`requests_per_day`). Over the
  limit the API answers `429` with `Retry-After`.
- **The allowance is kept to across restarts.** Before it asks, a live poller writes
  into the evidence store the moment before which it will not ask again
  (`not_before`): one interval on, where the interval is a day divided by the
  allowance less `_SPARE_REQUESTS`, so no twenty-four hours hold more requests of
  Beacon's than that. A `429`
  moves that moment to the end of the wait adsb.lol asked for. A process that starts
  reads it before it asks anything. It is one process's promise: two that share an
  address and not a data directory each keep their own, which is one more reason to
  run one server for each data directory.

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
                     `mlat`, or `other`. An answer says which of an aircraft's
                     fields were worked out by multilateration or relayed by a ground
                     station (`mlat`, `tisb`), and that outranks the aircraft's
                     `type`, which is its best source for anything.
    category         one of `AIRCRAFT_CATEGORIES`, such as `rotorcraft`, or null
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from beacon.errors import raise_bare
from beacon.evidence import Confidence, EvidenceStore, NewEvidence, Origin, Point
from beacon.ingest.area import SAN_FRANCISCO, Bounds
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.runner import PollerHeldError, PollReport

SOURCE = "adsblol"
API = "https://api.adsb.lol"

DATABASE_URL = "https://www.adsb.lol"
LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
# Where Beacon says that its aircraft rows are under the ODbL and how they differ from
# adsb.lol's database (ODbL 4.4 and 4.6 b): a document in Beacon's public repository.
METHOD_URL = "https://github.com/andrewtea/beacon/blob/main/docs/aircraft-data.md"
# The notice the license asks for wherever something made from the data is shown
# (4.3 a), in pieces: the words, and for the name of the database and of the license
# a link of its own, as the license's example has them.
CREDIT_PARTS: tuple[tuple[str, str | None], ...] = (
    ("Contains information from ", None),
    ("adsb.lol", DATABASE_URL),
    (", which is made available here under the ", None),
    ("Open Database License (ODbL)", LICENSE_URL),
    (". Beacon's aircraft rows are under the same license: ", None),
    ("what Beacon keeps of adsb.lol's data, and how", METHOD_URL),
    (".", None),
)
CREDIT_TEXT = "".join(words for words, _ in CREDIT_PARTS)
# The one address of a credit that has no pieces, for a reader that shows only that.
CREDIT_URL = DATABASE_URL

# Above this an aircraft is passing over, not circling a block.
MAX_ALTITUDE_M = 3_000.0
# A position older than this when the answer was made is where the aircraft was,
# not where it is.
MAX_POSITION_AGE_SECONDS = 60.0

# What adsb.lol allows an address with no key, and a key had without an account, in
# requests a day. A key that allows more is said with `requests_per_day`: 1,000 for
# one asked for with adsb.lol's form.
DEFAULT_REQUESTS_PER_DAY = 500
MAX_REQUESTS_PER_DAY = 360_000
# How many requests of a day's allowance the poller plans not to use: adsb.lol
# counts a request when it arrives and by its own clock, so one that Beacon sent
# just before midnight UTC can be counted in the day after.
_SPARE_REQUESTS = 4
# However much a key allows, the sky over a city is not asked for more often.
MIN_INTERVAL_SECONDS = 10.0
# The mock scenario has no allowance to keep to, and is asked this often.
MOCK_INTERVAL_SECONDS = 30.0
# The longest a live poller leaves adsb.lol alone because of what it wrote down or
# was told: the daily window begins again within a day, and a moment further off
# than that is a clock that was wrong.
MAX_HOLD_SECONDS = 86_400.0
_HELD = "adsb.lol is not asked again yet: Beacon keeps to its allowance across restarts."

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
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`api_key` is an adsb.lol key, sent as `x-api-key`. `requests_per_day` is
        what that key allows when it is more than the 500 of a key had without an
        account; it cannot be said without a key, because with none the limit is
        adsb.lol's 500 for the address. `interval_seconds` is for a stand-in for
        adsb.lol that is played on a clock of its own, as the mock scenario is: a
        poller given one keeps to no allowance and writes no moment down, and a
        poller that asks the network is refused one, since its interval is the
        allowance's to say. `wall_clock` is the time a wait adsb.lol asked for is
        counted from, which is when it answered."""
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
        if interval_seconds is not None:
            if http.origin == "live":
                raise ValueError("How often adsb.lol itself is asked is its allowance's to say.")
            if isinstance(interval_seconds, bool) or not 0 < interval_seconds <= MAX_HOLD_SECONDS:
                raise ValueError("An interval is a number of seconds, up to a day.")
        self._http = http
        self._api_key = api_key
        self._bounds = bounds
        self._centre, self._radius_nm = _circle_around(bounds)
        allowed = DEFAULT_REQUESTS_PER_DAY if requests_per_day is None else requests_per_day
        # Every poller keeps to the allowance but one that was given an interval.
        self._keeps_allowance = interval_seconds is None
        self.interval_seconds = (
            max(MIN_INTERVAL_SECONDS, 86_400 / max(1, allowed - _SPARE_REQUESTS))
            if interval_seconds is None
            else float(interval_seconds)
        )
        self._wall_clock = (lambda: datetime.now(UTC)) if wall_clock is None else wall_clock

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is where its client's answers
        come from and cannot be set: what a stand-in for the network answered is a
        stand-in's, whoever asks."""
        return self._http.origin

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        # The mock scenario has no allowance: it is played on a clock of its own,
        # and a step of it that failed is played again at the same moment.
        if self._keeps_allowance:
            self._reserve(store, now)
        try:
            body = fetch(
                self._http,
                "GET",
                f"{API}/v2/point/{self._centre.lat:.4f}/{self._centre.lon:.4f}/{self._radius_nm}",
                source=SOURCE,
                max_bytes=_MAX_BYTES,
                headers={} if self._api_key is None else {"x-api-key": self._api_key},
            )
        except SourceError as error:
            if self._keeps_allowance and _is_wait(error.retry_after):
                self._wait(store, now, min(float(error.retry_after), MAX_HOLD_SECONDS))
            raise
        aircraft, unreadable = parse_aircraft(read_json(body, SOURCE), self.origin, self._bounds)
        added = store.add(aircraft, now=now)
        return PollReport(added=len(added), unreadable=unreadable)

    def _reserve(self, store: EvidenceStore, now: datetime) -> None:
        """Write down, before adsb.lol is asked, the moment before which it is not
        asked again, so that a process that starts later keeps to it. When the
        moment an earlier request wrote down has not come, nothing is asked.

        Raises PollerHeldError in that case, and what the store raises when the
        moment could not be written: then nothing is asked either."""
        held = _not_before(store.state(self.name), now)
        if held is not None and now < held:
            raise_bare(PollerHeldError(_HELD, (held - now).total_seconds()))
        self._save(store, now, now + timedelta(seconds=self.interval_seconds))

    def _wait(self, store: EvidenceStore, now: datetime, seconds: float) -> None:
        """Keep the wait adsb.lol asked for, counted from when it answered, when it
        ends later than the moment already written down."""
        until = self._wall_clock() + timedelta(seconds=seconds)
        held = _not_before(store.state(self.name), now)
        if held is None or until > held:
            self._save(store, now, until)

    def _save(self, store: EvidenceStore, now: datetime, not_before: datetime) -> None:
        store.add([], state=(self.name, {"not_before": not_before.isoformat()}), now=now)


def _not_before(saved: dict[str, Any], now: datetime) -> datetime | None:
    """The moment a poller wrote down before which adsb.lol is not asked again, or
    None when it wrote none that can be read. Never more than `MAX_HOLD_SECONDS`
    from now: a moment further off is from a clock that has since been set back."""
    text = saved.get("not_before")
    if not isinstance(text, str) or len(text) > 40:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    return min(moment, now + timedelta(seconds=MAX_HOLD_SECONDS))


def _is_wait(seconds: Any) -> bool:
    return _is_number(seconds) and seconds > 0


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
    position_source = _position_source(entry)
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
            "callsign": _callsign(callsign),
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


def _position_source(entry: dict[str, Any]) -> str:
    """How an aircraft's position was had. An answer lists the fields that were
    worked out from when a signal reached several receivers (`mlat`) and the ones a
    ground station relayed (`tisb`), and a position on either list is that, whatever
    the aircraft's `type` says of its best source. A list that cannot be read says
    nothing either way, and the position is then `other`."""
    for listed, source in (("mlat", "mlat"), ("tisb", "tisb")):
        fields = entry.get(listed)
        if fields is None:
            continue
        if not isinstance(fields, list):
            return "other"
        if any(name in ("lat", "lon") for name in fields if isinstance(name, str)):
            return source
    kind = entry.get("type")
    return _POSITION_SOURCES.get(kind, "other") if isinstance(kind, str) else "other"


def _callsign(callsign: Any) -> str | None:
    """A callsign as broadcast, which is eight characters, or None."""
    if not isinstance(callsign, str) or len(callsign) > 16:
        return None
    return callsign.strip() or None


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
    """Whether a value of an answer is a number arithmetic can be done with. JSON
    has whole numbers too large to be one, and those are not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False
