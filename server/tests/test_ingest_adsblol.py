"""The adsb.lol aircraft poller, against hand-written answers in the API's format."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.evidence import EvidenceStore, EvidenceStoreError, Point
from beacon.ingest.adsblol import (
    MAX_ALTITUDE_M,
    MAX_POSITION_AGE_SECONDS,
    AdsbLolAircraft,
    parse_aircraft,
)
from beacon.ingest.area import SAN_FRANCISCO
from beacon.ingest.http import SourceError, SourceHttp
from beacon.ingest.runner import MAX_WAIT_SECONDS, Clock, PollReport, Runner

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
# 2026-11-27T20:00:00Z in milliseconds of Unix time.
NOW_MS = 1_795_809_600_000
FEET = 0.3048
CEILING_FT = MAX_ALTITUDE_M / FEET


def craft(address: str = "a1b2c3", **changes: Any) -> dict[str, Any]:
    """An aircraft as adsb.lol lists it: a helicopter over Hayes Valley, whose
    position is from two seconds before the answer."""
    fields: dict[str, Any] = {
        "hex": address,
        "type": "adsb_icao",
        "flight": "N123AB  ",
        "r": "N123AB",
        "t": "EC35",
        "alt_baro": 950,
        "alt_geom": 1050,
        "gs": 55.4,
        "track": 135.0,
        "baro_rate": -64,
        "squawk": "1200",
        "category": "A7",
        "lat": 37.7766,
        "lon": -122.4236,
        "seen_pos": 2.0,
        "seen": 0.4,
        "messages": 1900,
        # The fields that were worked out by multilateration, and the ones a ground
        # station relayed.
        "mlat": [],
        "tisb": [],
        "rssi": -9.5,
    }
    assert set(changes) <= set(fields)
    return {name: value for name, value in (fields | changes).items() if value is not ...}


def answer(*aircraft: Any, **changes: Any) -> dict[str, Any]:
    return {
        "ac": list(aircraft),
        "msg": "No error",
        "now": NOW_MS,
        "total": len(aircraft),
        "ctime": NOW_MS,
        "ptime": 0,
    } | changes


class Api:
    """Stands in for adsb.lol's API."""

    def __init__(self) -> None:
        self.aircraft: list[Any] = []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=answer(*self.aircraft))


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "mock")


@pytest.fixture
def api() -> Api:
    return Api()


@pytest.fixture
def wall() -> list[datetime]:
    """The real time, as a poller is told it, which a test moves."""
    return [NOON]


def poller_of(transport: Any, wall: list[datetime], **settings: Any) -> AdsbLolAircraft:
    """A poller that keeps to adsb.lol's allowance, by a clock the test holds."""
    return AdsbLolAircraft(
        SourceHttp(httpx2.MockTransport(transport)), wall_clock=lambda: wall[0], **settings
    )


def test_with_no_key_the_poller_asks_for_a_circle_that_holds_the_city_within_the_free_allowance(
    store: EvidenceStore, api: Api
) -> None:
    poller = AdsbLolAircraft(SourceHttp(httpx2.MockTransport(api)))

    poller.poll(store, NOON)

    (request,) = api.requests
    assert (request.method, str(request.url)) == (
        "GET",
        "https://api.adsb.lol/v2/point/37.7700/-122.4400/7",
    )
    assert "x-api-key" not in request.headers
    # Seven nautical miles from the middle reaches every corner of the box.
    middle = Point(37.77, -122.44)
    for corner in (
        Point(SAN_FRANCISCO.south, SAN_FRANCISCO.west),
        Point(SAN_FRANCISCO.north, SAN_FRANCISCO.east),
    ):
        assert middle.distance_m(corner) < 7 * 1852
    # 500 requests a day for an address with no key, less a few: adsb.lol counts by
    # its own clock.
    assert round(86_400 / poller.interval_seconds) == 496


def test_a_key_is_sent_and_lets_the_poller_ask_as_often_as_the_key_allows(
    store: EvidenceStore, api: Api
) -> None:
    http = SourceHttp(httpx2.MockTransport(api))
    unsaid = AdsbLolAircraft(http, api_key="key-of-ours")
    asked_for = AdsbLolAircraft(http, api_key="key-of-ours", requests_per_day=1_000)
    feeder = AdsbLolAircraft(http, api_key="key-of-ours", requests_per_day=7_200)
    sponsor = AdsbLolAircraft(http, api_key="key-of-ours", requests_per_day=360_000)

    unsaid.poll(store, NOON)

    assert api.requests[0].headers["x-api-key"] == "key-of-ours"
    # What a key allows is not in the key. A key had without an account allows the
    # 500 an address has with none, so that is what a key is taken to allow until
    # the poller is told more: 1,000 for one asked for with adsb.lol's form.
    assert round(86_400 / unsaid.interval_seconds) == 496
    assert round(86_400 / asked_for.interval_seconds) == 996
    assert round(86_400 / feeder.interval_seconds) == 7_196
    # However much a key allows, the city's sky is asked for every ten seconds at most.
    assert sponsor.interval_seconds == 10.0


@pytest.mark.parametrize(
    "settings",
    [
        # With no key the limit is adsb.lol's for the address, and nobody else's to say.
        {"requests_per_day": 7_200},
        {"api_key": "key-of-ours", "requests_per_day": 0},
        {"api_key": "key-of-ours", "requests_per_day": 360_001},
        {"api_key": "key-of-ours", "requests_per_day": True},
        {"api_key": ""},
        # A key goes into a header: nothing that could begin another one.
        {"api_key": "key\r\nx-other: 1"},
        {"api_key": "k" * 201},
    ],
)
def test_a_key_or_an_allowance_that_is_not_one_is_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"adsb\.lol|requests a day"):
        AdsbLolAircraft(SourceHttp(httpx2.MockTransport(Api())), **settings)


def test_how_often_adsb_lol_itself_is_asked_cannot_be_set() -> None:
    # Asks nothing: a poller that would ask the network is refused an interval.
    with SourceHttp() as network, pytest.raises(ValueError, match="its allowance's to say"):
        AdsbLolAircraft(network, interval_seconds=2.0)
    stand_in = SourceHttp(httpx2.MockTransport(Api()))
    # A stand-in played on a clock of its own has no allowance, and is given one.
    assert AdsbLolAircraft(stand_in, interval_seconds=30.0).interval_seconds == 30.0
    for interval in (0, -1.0, True, 86_401.0):
        with pytest.raises(ValueError, match="An interval"):
            AdsbLolAircraft(stand_in, interval_seconds=interval)


def test_a_process_that_starts_again_keeps_to_the_allowance(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    first = poller_of(api, wall)
    first.poll(store, NOON)

    # Another process, begun a minute later, with nothing in memory of the first.
    wall[0] = NOON + timedelta(seconds=60)
    again = poller_of(api, wall)
    held = again.poll(store, wall[0])

    # It asks nothing, stores nothing, and says how long is left of the interval:
    # that is no failure of adsb.lol's, and is not raised as one.
    assert len(api.requests) == 1
    assert (held.added, held.unreadable, held.partial) == (0, 0, False)
    assert held.again_in == pytest.approx(first.interval_seconds - 60)
    wall[0] = NOON + timedelta(seconds=first.interval_seconds)
    assert again.poll(store, wall[0]).again_in is None
    assert len(api.requests) == 2


def test_the_allowance_is_kept_by_the_time_of_the_request_and_not_of_the_runners_step(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    poller = poller_of(api, wall)
    # The step began half a minute ago: the pollers before this one took that long.
    step = NOON - timedelta(seconds=30)

    poller.poll(store, step)

    # The next request is an interval after this one was sent, not after the step began.
    until = NOON + timedelta(seconds=poller.interval_seconds)
    assert store.state("adsblol") == {"not_before": until.isoformat()}
    # A process that starts then, whose own step began long after, is still held.
    wall[0] = until - timedelta(seconds=1)
    assert poller_of(api, wall).poll(store, until + timedelta(hours=1)).again_in == 1.0
    assert len(api.requests) == 1


def test_the_runner_asks_a_held_poller_again_when_its_time_comes_and_not_an_interval_later(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    first = poller_of(api, wall, api_key="key-of-ours", requests_per_day=360_000)
    assert first.interval_seconds == 10.0
    first.poll(store, NOON)
    # The server starts again nine seconds after its last request.
    wall[0] = NOON + timedelta(seconds=9)
    again = poller_of(api, wall, api_key="key-of-ours", requests_per_day=360_000)
    runner = Runner([again], store, Clock(lambda: wall[0]))

    (held,) = runner.step().values()

    assert isinstance(held, PollReport)
    assert held.again_in == 1.0
    assert len(api.requests) == 1
    # Nothing was asked, so nothing is known of the source from this process yet.
    (status,) = runner.status()
    assert (status.polled_at, status.ok) == (None, None)
    wall[0] = NOON + timedelta(seconds=9.9)
    runner.step()
    assert len(api.requests) == 1
    # A second after it looked, and not ten: the interval is not begun again.
    wall[0] = NOON + timedelta(seconds=10)
    runner.step()
    assert len(api.requests) == 2
    (status,) = runner.status()
    assert (status.polled_at, status.ok) == (wall[0], True)


def test_the_moment_is_written_down_before_adsb_lol_is_asked(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    poller = poller_of(api, wall)
    seen: list[Any] = []

    def looked(request: httpx2.Request) -> httpx2.Response:
        seen.append(store.state("adsblol"))
        return api(request)

    poller_of(looked, wall).poll(store, NOON)

    until = NOON + timedelta(seconds=poller.interval_seconds)
    assert seen == [{"not_before": until.isoformat()}]

    # A request that fails has still been made, and counts.
    def down(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    wall[0] = until
    with pytest.raises(SourceError):
        poller_of(down, wall).poll(store, until)
    wall[0] = until + timedelta(seconds=1)
    assert poller.poll(store, wall[0]).again_in is not None
    # Only the first of the three reached the stand-in that answers.
    assert len(api.requests) == 1


def test_when_the_moment_cannot_be_written_down_nothing_is_asked(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    poller = poller_of(api, wall)
    store.close()

    with pytest.raises(EvidenceStoreError):
        poller.poll(store, NOON)

    assert api.requests == []


def test_the_wait_adsb_lol_asks_for_is_kept_across_a_restart(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    # It answered three seconds after it was asked, and the wait is from then.
    answered = NOON + timedelta(seconds=3)

    def spent(request: httpx2.Request) -> httpx2.Response:
        wall[0] = answered
        return httpx2.Response(429, headers={"Retry-After": "5400"})

    with pytest.raises(SourceError):
        poller_of(spent, wall).poll(store, NOON)

    wall[0] = NOON + timedelta(hours=1)
    again = poller_of(api, wall)
    assert again.poll(store, wall[0]).again_in == pytest.approx(1803.0)
    assert api.requests == []
    wall[0] = answered + timedelta(seconds=5400)
    again.poll(store, wall[0])
    assert len(api.requests) == 1


def test_a_wait_is_never_made_shorter_and_is_kept_for_as_long_as_the_runner_keeps_one(
    store: EvidenceStore, wall: list[datetime]
) -> None:
    # Any answer that is no success: a server that is down can ask for a wait as well.
    answers = iter([(429, "1"), (503, "172800"), (429, "99999999")])

    def waiting(request: httpx2.Request) -> httpx2.Response:
        status, wait = next(answers)
        return httpx2.Response(status, headers={"Retry-After": wait})

    poller = poller_of(waiting, wall)
    with pytest.raises(SourceError):
        poller.poll(store, NOON)
    # A wait of a second ends before the interval that was written down does.
    until = NOON + timedelta(seconds=poller.interval_seconds)
    assert store.state("adsblol") == {"not_before": until.isoformat()}

    wall[0] = until
    with pytest.raises(SourceError) as failure:
        poller.poll(store, until)
    # Two days, as asked, and not one: what the runner keeps within a process is
    # what a process that starts again keeps.
    two_days = until + timedelta(days=2)
    assert failure.value.retry_after == 172_800.0
    assert store.state("adsblol") == {"not_before": two_days.isoformat()}
    assert poller_of(waiting, wall).poll(store, until).again_in == 172_800.0

    wall[0] = two_days
    with pytest.raises(SourceError):
        poller.poll(store, two_days)
    # A week at the most, which is the longest the runner waits for any source.
    week = two_days + timedelta(seconds=MAX_WAIT_SECONDS)
    assert store.state("adsblol") == {"not_before": week.isoformat()}


@pytest.mark.parametrize("saved", ["soon", 7, None, "2026-11-27T20:00:00", "9" * 41])
def test_a_moment_that_cannot_be_read_holds_nothing_and_one_too_far_off_holds_a_week(
    store: EvidenceStore, api: Api, wall: list[datetime], saved: Any
) -> None:
    poller = poller_of(api, wall)
    store.add([], state=("adsblol", {"not_before": saved}), now=NOON)

    poller.poll(store, NOON)

    assert len(api.requests) == 1
    # A moment a year off is from a clock that has since been set back.
    far = NOON + timedelta(days=365)
    store.add([], state=("adsblol", {"not_before": far.isoformat()}), now=NOON)
    assert poller.poll(store, NOON).again_in == MAX_WAIT_SECONDS
    assert len(api.requests) == 1
    # The week is counted from that look, and not again from each one after it.
    wall[0] = NOON + timedelta(days=6)
    assert poller.poll(store, wall[0]).again_in == 86_400.0
    wall[0] = NOON + timedelta(seconds=MAX_WAIT_SECONDS)
    assert poller.poll(store, wall[0]).again_in is None
    assert len(api.requests) == 2


def test_a_wait_that_could_not_be_written_down_is_still_kept_and_written_before_the_next_request(
    store: EvidenceStore, api: Api, wall: list[datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits = [httpx2.Response(503, headers={"Retry-After": "172800"})]

    def answered(request: httpx2.Request) -> httpx2.Response:
        return waits.pop() if waits else api(request)

    poller = poller_of(answered, wall)
    postpone = store.postpone
    failing = [True]

    def postponed(*args: Any, **options: Any) -> None:
        if failing[0]:
            raise sqlite3.OperationalError("database is locked")
        postpone(*args, **options)

    monkeypatch.setattr(store, "postpone", postponed)

    # The error is adsb.lol's, with the wait it asked for, which the runner keeps:
    # not the store's, which would be tried again after an interval.
    with pytest.raises(SourceError) as failure:
        poller.poll(store, NOON)
    assert failure.value.retry_after == 172_800.0
    assert failure.value.__context__ is None
    interval = NOON + timedelta(seconds=poller.interval_seconds)
    assert store.state("adsblol") == {"not_before": interval.isoformat()}

    # While it cannot be written, nothing is asked, though the interval is over.
    wall[0] = interval
    with pytest.raises(sqlite3.OperationalError):
        poller.poll(store, interval)
    assert api.requests == []

    # Once the store takes it, it is written before anything is asked, and holds.
    failing[0] = False
    held = poller.poll(store, interval)
    two_days = NOON + timedelta(days=2)
    assert store.state("adsblol") == {"not_before": two_days.isoformat()}
    assert held.again_in == pytest.approx(172_800.0 - poller.interval_seconds)
    assert api.requests == []


def test_the_mock_scenarios_poller_keeps_no_allowance(store: EvidenceStore, api: Api) -> None:
    poller = AdsbLolAircraft(SourceHttp(httpx2.MockTransport(api)), interval_seconds=30.0)

    # A step of the scenario that failed is played again at the same moment.
    poller.poll(store, NOON)
    poller.poll(store, NOON)

    assert len(api.requests) == 2
    assert store.state("adsblol") == {}


def test_an_aircraft_becomes_a_row_named_by_its_address_and_the_time_of_its_position(
    store: EvidenceStore, api: Api, wall: list[datetime]
) -> None:
    api.aircraft = [craft()]
    poller = poller_of(api, wall)

    first = poller.poll(store, NOON)
    # An aircraft that was not heard from again is listed with the same position.
    api.aircraft = [craft(seen_pos=2.0)]
    wall[0] = NOON + timedelta(minutes=3)
    second = poller.poll(store, wall[0])

    assert (first.added, second.added) == (1, 0)
    (aircraft,) = store.search()
    # The position is from two seconds before the answer was made.
    assert (aircraft.kind, aircraft.source, aircraft.ref) == (
        "aircraft",
        "adsblol",
        f"a1b2c3@{NOW_MS // 1000 - 2}",
    )
    assert aircraft.occurred_at == datetime(2026, 11, 27, 19, 59, 58, tzinfo=UTC)
    assert aircraft.point == Point(37.7766, -122.4236)
    # The poller was given a stand-in for the network, so the row is a stand-in's.
    assert (aircraft.confidence, aircraft.origin, aircraft.url) == ("high", "mock", None)
    assert aircraft.data == {
        "icao24": "a1b2c3",
        "callsign": "N123AB",
        # The geometric height, not the barometric one, and in metres: 1,050 ft.
        "altitude_m": 320.0,
        # 55.4 knots.
        "speed_mps": 28.5,
        "track_deg": 135.0,
        # 64 feet a minute down.
        "climb_mps": -0.33,
        "squawk": "1200",
        "position_source": "adsb",
        "category": "rotorcraft",
    }


def test_only_aircraft_a_person_on_the_street_could_notice_are_kept() -> None:
    kept, unreadable = parse_aircraft(
        answer(
            craft("aaaaa1", alt_geom=CEILING_FT - 1),
            # Below the ceiling is kept, and at it is not.
            craft("aaaaa2", alt_geom=CEILING_FT + 0.01),
            # An aircraft on the ground says so where its height would be.
            craft("aaaaa3", alt_baro="ground", alt_geom=...),
            craft("aaaaa4", lat=...),
            craft("aaaaa5", seen_pos=...),
            # With no geometric height the barometric one is used.
            craft("aaaaa6", alt_geom=..., alt_baro=1476),
            craft("aaaaa7", alt_geom=..., alt_baro=...),
            # Low by the barometer and high in fact: the geometric height decides.
            craft("aaaaa8", alt_geom=29_500, alt_baro=300),
            craft("AAAAA9", type="mlat", flight=..., category="A0", squawk=...),
            # Where the aircraft was a minute and more ago is not where it is.
            craft("aaaab1", seen_pos=MAX_POSITION_AGE_SECONDS + 0.1),
            # Asked for a circle, the API answers with what is over the bay and the
            # ocean too: the city's box is cut from it.
            craft("aaaab2", lat=37.62, lon=-122.38),
            craft("aaaab3", lon=-122.60),
            # An address that is one receiver's name for a track, not the aircraft's.
            craft("~a993cb"),
        ),
        "mock",
    )

    assert unreadable == 0
    assert [aircraft.data["icao24"] for aircraft in kept] == ["aaaaa1", "aaaaa6", "aaaaa9"]
    assert kept[1].data["altitude_m"] == 449.9
    multilaterated = kept[2]
    assert multilaterated.ref == f"aaaaa9@{NOW_MS // 1000 - 2}"
    # Worked out from several receivers, which is less exact than a broadcast position.
    assert multilaterated.confidence == "medium"
    assert multilaterated.data["position_source"] == "mlat"
    assert (multilaterated.data["callsign"], multilaterated.data["category"]) == (None, None)
    assert multilaterated.origin == "mock"


@pytest.mark.parametrize(
    ("kind", "source", "confidence"),
    [
        ("adsb_icao", "adsb", "high"),
        ("adsb_icao_nt", "adsb", "high"),
        # Relayed by a ground station, not heard from the aircraft.
        ("adsr_icao", "adsr", "medium"),
        ("tisb_icao", "tisb", "medium"),
        ("adsc", "adsc", "medium"),
        ("mlat", "mlat", "medium"),
        ("mode_s", "other", "medium"),
        ("something new", "other", "medium"),
        (None, "other", "medium"),
    ],
)
def test_only_a_position_the_aircraft_broadcast_itself_is_trusted_highly(
    kind: Any, source: str, confidence: str
) -> None:
    (aircraft,), _ = parse_aircraft(answer(craft(type=kind)), "live")

    assert (aircraft.data["position_source"], aircraft.confidence) == (source, confidence)


@pytest.mark.parametrize(
    ("flags", "source", "confidence"),
    [
        # The aircraft broadcasts, and this position was still worked out from when
        # its signal reached several receivers, or relayed by a ground station.
        ({"mlat": ["lat", "lon", "nic", "rc"]}, "mlat", "medium"),
        ({"mlat": ["lon"]}, "mlat", "medium"),
        ({"tisb": ["lat", "lon"]}, "tisb", "medium"),
        ({"mlat": ["lat"], "tisb": ["lat"]}, "mlat", "medium"),
        # Other fields were, and the position is the aircraft's own.
        ({"mlat": ["gs", "track", "baro_rate"]}, "adsb", "high"),
        ({"mlat": ..., "tisb": ...}, "adsb", "high"),
        # A list that cannot be read says nothing either way, and neither does one
        # with a member that is no field's name: it is not taken for a list that
        # names no position, which would make the position the aircraft's own.
        ({"mlat": "lat"}, "other", "medium"),
        ({"tisb": {"lat": True}}, "other", "medium"),
        ({"mlat": [["lat"], 7, None]}, "other", "medium"),
        ({"mlat": ["gs", 7]}, "other", "medium"),
        ({"mlat": [], "tisb": [None]}, "other", "medium"),
    ],
)
def test_how_a_position_was_had_outranks_the_aircrafts_best_source(
    flags: dict[str, Any], source: str, confidence: str
) -> None:
    (aircraft,), _ = parse_aircraft(answer(craft(type="adsb_icao", **flags)), "live")

    assert (aircraft.data["position_source"], aircraft.confidence) == (source, confidence)


HUGE = 10**400


def test_one_entry_that_is_not_as_expected_does_not_cost_the_others() -> None:
    kept, unreadable = parse_aircraft(
        answer(
            craft("ccccc1", type=["adsb_icao"]),
            craft("ccccc2", type={"best": "mlat"}),
            # Whole numbers too large for arithmetic, which JSON allows.
            craft("ccccc3", gs=HUGE, track=HUGE, baro_rate=-HUGE),
            craft("ccccc4", alt_geom=HUGE),
            craft("ccccc5", alt_geom=..., alt_baro=HUGE),
            craft("ccccc6", seen_pos=HUGE),
            craft("ccccc7", lat=HUGE),
            craft("ccccc8", flight="N" * 500, squawk=["1200"], category=["A7"]),
            craft("ccccc9"),
        ),
        "live",
    )

    assert unreadable == 0
    assert [(row.data["icao24"], row.data["position_source"]) for row in kept] == [
        ("ccccc1", "other"),
        ("ccccc2", "other"),
        ("ccccc3", "adsb"),
        ("ccccc4", "adsb"),
        ("ccccc8", "adsb"),
        ("ccccc9", "adsb"),
    ]
    by_address = {row.data["icao24"]: row.data for row in kept}
    assert by_address["ccccc3"] | {"icao24": ""} == by_address["ccccc9"] | {
        "icao24": "",
        "speed_mps": None,
        "track_deg": None,
        "climb_mps": None,
    }
    # With no height it can use, the barometric one is the height.
    assert by_address["ccccc4"]["altitude_m"] == round(950 * FEET, 1)
    assert (by_address["ccccc8"]["callsign"], by_address["ccccc8"]["squawk"]) == (None, None)
    assert by_address["ccccc8"]["category"] is None


def test_an_entry_beacon_cannot_read_is_counted_or_passed_over() -> None:
    kept, unreadable = parse_aircraft(
        answer(
            "not an aircraft",
            craft(hex=...),
            craft(hex=12),
            craft("not an address"),
            craft("bbbbb3", lat=91.0),
            craft("bbbbb4", lon="west"),
            craft("bbbbb5", alt_geom=float("nan"), alt_baro=float("nan")),
            craft("bbbbb6", seen_pos=-1),
            craft("bbbbb1", gs="fast", track=None, baro_rate=..., squawk=7700, category=7),
        ),
        "live",
    )

    assert unreadable == 3
    (aircraft,) = kept
    assert aircraft.data == {
        "icao24": "bbbbb1",
        "callsign": "N123AB",
        "altitude_m": 320.0,
        "speed_mps": None,
        "track_deg": None,
        "climb_mps": None,
        "squawk": None,
        "position_source": "adsb",
        "category": None,
    }


def test_an_empty_sky_is_not_an_error(store: EvidenceStore, api: Api) -> None:
    poller = AdsbLolAircraft(SourceHttp(httpx2.MockTransport(api)))

    assert poller.poll(store, NOON).added == 0


@pytest.mark.parametrize(
    "payload",
    # Objects without the aircraft or the moment, as an error can be: not an empty sky.
    [
        [],
        None,
        {},
        {"detail": "Not Found"},
        {"ac": None, "now": NOW_MS},
        {"ac": {}, "now": NOW_MS},
        {"ac": [], "msg": "No error"},
        {"ac": [], "now": "today"},
        {"ac": [], "now": 0},
        {"ac": [], "now": True},
        {"ac": [], "now": HUGE},
    ],
)
def test_an_answer_that_is_not_a_list_of_aircraft_made_at_a_moment_is_an_error(
    payload: Any,
) -> None:
    with pytest.raises(SourceError, match=r"^adsblol sent a reply Beacon cannot read\.$"):
        parse_aircraft(payload, "live")


def test_a_spent_allowance_is_reported_with_the_wait_adsb_lol_asks_for(
    store: EvidenceStore,
) -> None:
    def spent(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "5400"}, json={"detail": "quota"})

    poller = AdsbLolAircraft(SourceHttp(httpx2.MockTransport(spent)), api_key="key-of-ours")

    with pytest.raises(SourceError) as failure:
        poller.poll(store, NOON)

    assert (failure.value.status, failure.value.retry_after) == (429, 5400.0)
    # The key was in a header of the request, and is in nothing the failure says.
    assert str(failure.value) == "adsblol answered HTTP 429."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)
