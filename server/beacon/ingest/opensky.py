"""Aircraft over San Francisco, from the OpenSky Network.

`GET https://opensky-network.org/api/states/all` with a bounding box answers with one
state vector for each aircraft in it: where it is, how high, how fast, and which way.
The API is documented at https://openskynetwork.github.io/opensky-api/rest.html. It
answers without an account at 400 requests a day, and with an API client's id and
secret (OAuth2 client credentials) at 4,000.

**Being able to call it is not permission to.** OpenSky's terms
(https://opensky-network.org/about/terms-of-use) license the REST API for non-profit
research and education only, and say that using it "in any operational capacity",
which a running Beacon is, needs a written license from OpenSky whoever asks.
`beacon.ingest.setup` therefore builds this poller for the real API only when
`BEACON_OPENSKY_PERMITTED=1` says that license has been given.

Each position of an aircraft is one row. Only aircraft a person on the street could
notice are kept: airborne and below `MAX_ALTITUDE_M`. An airliner at cruise is not
what anyone taps about, and storing every one would bury the helicopter that is.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from beacon.errors import raise_bare
from beacon.evidence import EvidenceStore, NewEvidence, Origin, Point
from beacon.ingest.area import SAN_FRANCISCO, Bounds
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.runner import PollReport

SOURCE = "opensky"
STATES_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)

# Above this an aircraft is passing over, not circling a block.
MAX_ALTITUDE_M = 3_000.0

# 400 requests a day without an account is one every 216 seconds, and 4,000 with one
# is one every 21.6. A box this small costs one credit a request.
ANONYMOUS_INTERVAL_SECONDS = 240.0
SIGNED_IN_INTERVAL_SECONDS = 30.0

_MAX_BYTES = 5_000_000
# A token is asked for again this long before it runs out.
_TOKEN_MARGIN_SECONDS = 30.0

# The ADS-B emitter categories of the `category` field, for the ones worth naming.
_CATEGORIES = {
    2: "light",
    3: "small",
    4: "large",
    5: "high_vortex_large",
    6: "heavy",
    7: "high_performance",
    8: "rotorcraft",
    9: "glider",
    10: "lighter_than_air",
    12: "ultralight",
    14: "unmanned",
}
_POSITION_SOURCES = {0: "adsb", 1: "asterix", 2: "mlat", 3: "flarm"}


class OpenSkyAircraft:
    name = SOURCE

    def __init__(
        self,
        http: SourceHttp,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        bounds: Bounds = SAN_FRANCISCO,
        interval_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (client_id is None) != (client_secret is None):
            raise ValueError("An OpenSky client id and secret are given together.")
        self._http = http
        # What a stand-in for the network answered is a stand-in's, whoever asks.
        self._origin: Origin = http.origin
        self._credentials = None if client_id is None else (client_id, client_secret)
        self._bounds = bounds
        self._monotonic = monotonic
        self._token: str | None = None
        self._token_until = 0.0
        # The mock scenario has no quota to keep, and sets its own interval.
        self.interval_seconds = interval_seconds or (
            ANONYMOUS_INTERVAL_SECONDS if client_id is None else SIGNED_IN_INTERVAL_SECONDS
        )

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        try:
            body = self._states()
        except SourceError as error:
            if error.status != 401 or self._credentials is None:
                raise
            body = None
        if body is None:
            # The token ran out early. One new token, one more try.
            self._token = None
            body = self._states()
        aircraft, unreadable = parse_states(read_json(body, SOURCE), self._origin)
        added = store.add(aircraft, now=now)
        return PollReport(added=len(added), unreadable=unreadable)

    def _states(self) -> bytes:
        return fetch(
            self._http,
            "GET",
            STATES_URL,
            source=SOURCE,
            max_bytes=_MAX_BYTES,
            params={
                "lamin": str(self._bounds.south),
                "lomin": str(self._bounds.west),
                "lamax": str(self._bounds.north),
                "lomax": str(self._bounds.east),
                # Asks for the aircraft category, which tells a helicopter from a jet.
                "extended": "1",
            },
            headers=self._authorization(),
        )

    def _authorization(self) -> dict[str, str]:
        if self._credentials is None:
            return {}
        if self._token is None or self._monotonic() >= self._token_until:
            client_id, client_secret = self._credentials
            asked = self._monotonic()
            reply = read_json(
                fetch(
                    self._http,
                    "POST",
                    TOKEN_URL,
                    source=SOURCE,
                    max_bytes=100_000,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                ),
                SOURCE,
            )
            token = reply.get("access_token") if isinstance(reply, dict) else None
            if not isinstance(token, str) or not token:
                raise_bare(SourceError(SOURCE, "sent no access token"))
            lifetime = reply.get("expires_in")
            # OpenSky's tokens last 30 minutes.
            seconds = float(lifetime) if _is_number(lifetime) and lifetime > 0 else 1800.0
            self._token = token
            self._token_until = asked + max(0.0, seconds - _TOKEN_MARGIN_SECONDS)
        return {"Authorization": f"Bearer {self._token}"}


def parse_states(payload: Any, origin: Origin) -> tuple[list[NewEvidence], int]:
    """The aircraft of a `/states/all` answer that are airborne below
    `MAX_ALTITUDE_M`, and how many state vectors could not be read. `states` is null
    when the box is empty.

    Raises SourceError when the answer is not a states object. One with no `states`
    at all is not an empty sky: an error can arrive as an object too."""
    if (
        not isinstance(payload, dict)
        or "states" not in payload
        or not isinstance(payload["states"], list | None)
    ):
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    aircraft: list[NewEvidence] = []
    unreadable = 0
    for state in payload["states"] or []:
        # The fields are positional. `category` is the eighteenth, sent with `extended`.
        if not isinstance(state, list) or len(state) < 17:
            unreadable += 1
            continue
        found = _aircraft(state, origin)
        if found is not None:
            aircraft.append(found)
    return aircraft, unreadable


def _aircraft(state: list[Any], origin: Origin) -> NewEvidence | None:
    """One row, or None for an aircraft that is not kept: one with no position, on
    the ground, of unknown height, or above the ceiling."""
    icao24, callsign = state[0], state[1]
    time_position, lon, lat = state[3], state[5], state[6]
    baro_altitude, on_ground, geo_altitude = state[7], state[8], state[13]
    if not isinstance(icao24, str) or not icao24 or len(icao24) > 6 or not icao24.isalnum():
        return None
    if not _is_number(time_position) or not _is_number(lat) or not _is_number(lon):
        return None
    # The height above the ellipsoid is nearer the height above the street than the
    # barometric one, which moves with the weather.
    altitude = geo_altitude if _is_number(geo_altitude) else baro_altitude
    if on_ground is not False or not _is_number(altitude) or altitude >= MAX_ALTITUDE_M:
        return None
    try:
        point = Point(lat, lon)
        seen = datetime.fromtimestamp(time_position, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    position_source = _POSITION_SOURCES.get(state[16]) if _is_number(state[16]) else None
    category = state[17] if len(state) > 17 and _is_number(state[17]) else None
    return NewEvidence(
        kind="aircraft",
        source=SOURCE,
        ref=f"{icao24.lower()}@{int(time_position)}",
        occurred_at=seen,
        point=point,
        # A position the aircraft broadcast itself is better than one worked out
        # from when its signal reached several receivers.
        confidence="high" if position_source == "adsb" else "medium",
        origin=origin,
        data={
            "icao24": icao24.lower(),
            "callsign": callsign.strip() or None if isinstance(callsign, str) else None,
            "altitude_m": altitude,
            "speed_mps": state[9] if _is_number(state[9]) else None,
            # Degrees clockwise from north.
            "track_deg": state[10] if _is_number(state[10]) else None,
            "climb_mps": state[11] if _is_number(state[11]) else None,
            "squawk": state[14] if isinstance(state[14], str) else None,
            "position_source": position_source,
            "category": _CATEGORIES.get(category),
        },
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value == value
