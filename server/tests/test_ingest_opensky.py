"""The OpenSky aircraft poller, against hand-written answers in the API's format."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from beacon.evidence import EvidenceStore, Point
from beacon.ingest.http import SourceError, http_client
from beacon.ingest.opensky import MAX_ALTITUDE_M, OpenSkyAircraft, parse_states

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
# 2026-11-27T20:00:00Z in Unix time.
SECONDS = 1_795_809_600


def state(icao24: str = "a1b2c3", **changes: Any) -> list[Any]:
    """A state vector in the order OpenSky documents: a helicopter over Hayes Valley."""
    fields: dict[str, Any] = {
        "icao24": icao24,
        "callsign": "N123AB  ",
        "origin_country": "United States",
        "time_position": SECONDS,
        "last_contact": SECONDS + 1,
        "longitude": -122.4236,
        "latitude": 37.7766,
        "baro_altitude": 290.0,
        "on_ground": False,
        "velocity": 28.5,
        "true_track": 135.0,
        "vertical_rate": -0.5,
        "sensors": None,
        "geo_altitude": 320.0,
        "squawk": "1200",
        "spi": False,
        "position_source": 0,
        "category": 8,
    }
    assert set(changes) <= set(fields)
    return list((fields | changes).values())


class Api:
    """Stands in for OpenSky's API and its token endpoint."""

    def __init__(self) -> None:
        self.states: list[Any] | None = []
        self.requests: list[httpx2.Request] = []
        self.tokens_issued = 0
        self.expires_in: Any = 1800
        # Bearer tokens the API no longer accepts.
        self.revoked: set[str] = set()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.host == "auth.opensky-network.org":
            form = parse_qs(request.content.decode())
            if form != {
                "grant_type": ["client_credentials"],
                "client_id": ["beacon-client"],
                "client_secret": ["s3cret"],
            }:
                return httpx2.Response(401, json={"error": "invalid_client"})
            self.tokens_issued += 1
            return httpx2.Response(
                200,
                json={"access_token": f"token-{self.tokens_issued}", "expires_in": self.expires_in},
            )
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if bearer in self.revoked:
            return httpx2.Response(401, text="expired")
        return httpx2.Response(200, json={"time": SECONDS, "states": self.states})

    @property
    def bearers(self) -> list[str | None]:
        return [
            request.headers.get("authorization")
            for request in self.requests
            if request.url.host == "opensky-network.org"
        ]


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "live")


@pytest.fixture
def api() -> Api:
    return Api()


def signed_in(api: Api, clock: list[float]) -> OpenSkyAircraft:
    return OpenSkyAircraft(
        http_client(httpx2.MockTransport(api)),
        origin="live",
        client_id="beacon-client",
        client_secret="s3cret",
        monotonic=lambda: clock[0],
    )


def test_without_an_account_the_poller_asks_for_the_city_and_polls_within_the_free_quota(
    store: EvidenceStore, api: Api
) -> None:
    poller = OpenSkyAircraft(http_client(httpx2.MockTransport(api)), origin="live")

    poller.poll(store, NOON)

    (request,) = api.requests
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "opensky-network.org",
        "/api/states/all",
    )
    assert dict(request.url.params) == {
        "lamin": "37.7",
        "lomin": "-122.53",
        "lamax": "37.84",
        "lomax": "-122.35",
        "extended": "1",
    }
    assert "authorization" not in request.headers
    # 400 requests a day is one every 216 seconds.
    assert 86_400 / poller.interval_seconds <= 400


def test_an_aircraft_becomes_a_row_named_by_its_address_and_the_time_of_its_position(
    store: EvidenceStore, api: Api
) -> None:
    api.states = [state()]
    poller = OpenSkyAircraft(http_client(httpx2.MockTransport(api)), origin="live")

    first = poller.poll(store, NOON)
    # OpenSky answers with the same position until the aircraft reports a new one.
    second = poller.poll(store, NOON)

    assert (first.added, second.added) == (1, 0)
    (aircraft,) = store.search()
    assert (aircraft.kind, aircraft.source, aircraft.ref) == (
        "aircraft",
        "opensky",
        f"a1b2c3@{SECONDS}",
    )
    assert aircraft.occurred_at == NOON
    assert aircraft.point == Point(37.7766, -122.4236)
    assert (aircraft.confidence, aircraft.origin, aircraft.url) == ("high", "live", None)
    assert aircraft.data == {
        "icao24": "a1b2c3",
        "callsign": "N123AB",
        # The geometric height, not the barometric one.
        "altitude_m": 320.0,
        "speed_mps": 28.5,
        "track_deg": 135.0,
        "climb_mps": -0.5,
        "squawk": "1200",
        "position_source": "adsb",
        "category": "rotorcraft",
    }


def test_only_aircraft_a_person_on_the_street_could_notice_are_kept() -> None:
    kept, unreadable = parse_states(
        {
            "time": SECONDS,
            "states": [
                state("aaaaa1", geo_altitude=MAX_ALTITUDE_M),
                state("aaaaa2", geo_altitude=MAX_ALTITUDE_M + 0.1),
                state("aaaaa3", on_ground=True),
                state("aaaaa4", latitude=None),
                state("aaaaa5", time_position=None),
                # With no geometric height the barometric one is used.
                state("aaaaa6", geo_altitude=None, baro_altitude=450.0),
                state("aaaaa7", geo_altitude=None, baro_altitude=None),
                # Low by the barometer and high in fact: the geometric height decides.
                state("aaaaa8", geo_altitude=9000.0, baro_altitude=100.0),
                state("AAAAA9", position_source=2, callsign=None, category=0, squawk=None),
            ],
        },
        "mock",
    )

    assert unreadable == 0
    assert [aircraft.data["icao24"] for aircraft in kept] == ["aaaaa1", "aaaaa6", "aaaaa9"]
    assert kept[1].data["altitude_m"] == 450.0
    multilaterated = kept[2]
    assert multilaterated.ref == f"aaaaa9@{SECONDS}"
    # Worked out from several receivers, which is less exact than a broadcast position.
    assert multilaterated.confidence == "medium"
    assert multilaterated.data["position_source"] == "mlat"
    assert (multilaterated.data["callsign"], multilaterated.data["category"]) == (None, None)
    assert multilaterated.origin == "mock"


def test_a_state_vector_beacon_cannot_read_is_counted_or_passed_over() -> None:
    without_category = state("bbbbb1")[:17]

    kept, unreadable = parse_states(
        {
            "time": SECONDS,
            "states": [
                state("bbbbb2")[:16],
                "not a vector",
                without_category,
                state("not an address"),
                state("bbbbb3", latitude=91.0),
                state("bbbbb4", longitude="west"),
                state("bbbbb5", geo_altitude=float("nan"), baro_altitude=float("nan")),
            ],
        },
        "live",
    )

    assert unreadable == 2
    (aircraft,) = kept
    assert (aircraft.data["icao24"], aircraft.data["category"]) == ("bbbbb1", None)


def test_an_empty_sky_is_not_an_error(store: EvidenceStore, api: Api) -> None:
    api.states = None
    poller = OpenSkyAircraft(http_client(httpx2.MockTransport(api)), origin="live")

    assert poller.poll(store, NOON).added == 0


@pytest.mark.parametrize("payload", [[], {"states": {}}, {"states": "none"}, None])
def test_an_answer_that_is_not_a_states_object_is_an_error(payload: Any) -> None:
    with pytest.raises(SourceError, match=r"^opensky sent a reply Beacon cannot read\.$"):
        parse_states(payload, "live")


def test_with_an_account_one_token_is_used_until_shortly_before_it_runs_out(
    store: EvidenceStore, api: Api
) -> None:
    clock = [1000.0]
    poller = signed_in(api, clock)
    # 4,000 requests a day is one every 21.6 seconds.
    assert 86_400 / poller.interval_seconds <= 4000

    poller.poll(store, NOON)
    clock[0] += 1769.0
    poller.poll(store, NOON)
    # 30 seconds before the 1,800 the token lasts, a new one is asked for.
    clock[0] += 1.0
    poller.poll(store, NOON)

    assert api.bearers == ["Bearer token-1", "Bearer token-1", "Bearer token-2"]
    token_request = api.requests[0]
    assert (token_request.method, str(token_request.url)) == (
        "POST",
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/"
        "openid-connect/token",
    )
    assert token_request.headers["content-type"] == "application/x-www-form-urlencoded"


@pytest.mark.parametrize("expires_in", [None, "soon", -5, True])
def test_a_token_with_no_usable_lifetime_is_taken_to_last_thirty_minutes(
    store: EvidenceStore, api: Api, expires_in: Any
) -> None:
    api.expires_in = expires_in
    clock = [0.0]
    poller = signed_in(api, clock)

    poller.poll(store, NOON)
    clock[0] = 1769.0
    poller.poll(store, NOON)
    clock[0] = 1770.0
    poller.poll(store, NOON)

    assert api.tokens_issued == 2


def test_a_token_refused_early_is_replaced_once_and_the_poll_tried_again(
    store: EvidenceStore, api: Api
) -> None:
    api.states = [state()]
    poller = signed_in(api, [0.0])
    poller.poll(store, NOON)
    api.revoked = {"token-1"}

    report = poller.poll(store, NOON)

    assert api.bearers == ["Bearer token-1", "Bearer token-1", "Bearer token-2"]
    assert report.added == 0

    # A new token that is refused as well is the poll's failure, not a loop.
    api.revoked = {"token-2", "token-3"}
    with pytest.raises(SourceError, match=r"^opensky answered HTTP 401\.$") as failure:
        poller.poll(store, NOON)
    assert api.tokens_issued == 3
    assert "s3cret" not in str(failure.value)


def test_credentials_the_token_endpoint_refuses_fail_the_poll_without_being_repeated(
    store: EvidenceStore, api: Api
) -> None:
    poller = OpenSkyAircraft(
        http_client(httpx2.MockTransport(api)),
        origin="live",
        client_id="beacon-client",
        client_secret="wrong-s3cret",
    )

    with pytest.raises(SourceError) as failure:
        poller.poll(store, NOON)

    assert str(failure.value) == "opensky answered HTTP 401."
    assert failure.value.__cause__ is None
    # No request for aircraft is made without a token.
    assert api.bearers == []


def test_a_token_reply_without_a_token_fails_the_poll(store: EvidenceStore) -> None:
    def no_token(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"expires_in": 1800})

    poller = OpenSkyAircraft(
        http_client(httpx2.MockTransport(no_token)),
        origin="live",
        client_id="beacon-client",
        client_secret="s3cret",
    )

    with pytest.raises(SourceError, match=r"^opensky sent no access token\.$"):
        poller.poll(store, NOON)


def test_a_spent_quota_is_reported_with_the_wait_opensky_asks_for(store: EvidenceStore) -> None:
    def spent(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "5400"})

    poller = OpenSkyAircraft(http_client(httpx2.MockTransport(spent)), origin="live")

    with pytest.raises(SourceError) as failure:
        poller.poll(store, NOON)

    assert (failure.value.status, failure.value.retry_after) == (429, 5400.0)


def test_an_id_without_a_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="given together"):
        OpenSkyAircraft(http_client(), origin="live", client_id="beacon-client")
