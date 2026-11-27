"""Web push: the key, the addresses a push goes to, the subscriptions file, the
pass that sends, the routes, and the server around them."""

import asyncio
import base64
import json
import logging
import sqlite3
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

import beacon.api.push as api_push
import beacon.main as main_module
import beacon.push.desk as desk_module
import beacon.push.store as store_module
from beacon import taps as beacon_taps
from beacon.ember import EmberClient, Storage
from beacon.incident import Engine
from beacon.ingest.http import SourceHttp
from beacon.investigate import (
    Cited,
    InvestigationDesk,
    Outcome,
    Run,
    Runs,
    stand_in_investigator,
)
from beacon.main import _is_private, create_app
from beacon.places import REACH_M
from beacon.places.read import ELSEWHERE
from beacon.push import (
    ABOUT,
    KEPT,
    MAX_ENDPOINT,
    OFF,
    PUSH_VARIABLE,
    Notifier,
    Pass,
    Push,
    PushConfigError,
    Subscriptions,
    SubscriptionsError,
    Vapid,
    VapidError,
    audience,
    checked,
    notice_of,
    open_push,
)
from beacon.push.vapid import SUBJECT_VARIABLE, TOKEN_LIFE, key_file, open_vapid
from beacon.taps import TapMedia, Taps
from tests.incident_rows import HAYES, Rows, at, mock_store
from tests.test_incident_aircraft import offset
from tests.test_investigate import BLOCKS, NAMES, TAP, city

# Addresses as the push services make them. The first holds a colon and an
# underscore, as Chrome's do.
FCM = "https://fcm.googleapis.com/fcm/send/dXY2zQ:APA91bH_-secretSecretSecret"
MOZ = "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABsecretSecret"
APPLE = "https://web.push.apple.com/QGsecretSecretSecret"
EDGE = "https://wns2-by3p.notify.windows.com/w/?token=BQYAAACsecret%2fSecret%3d"
# Three hundred metres from the fire, and three kilometres from it.
NEAR = offset(HAYES, 300, 0)
FAR = offset(HAYES, 3000, 0)
SHOWN = {"Beacon-Mode": "mock"}


@pytest.fixture(autouse=True)
def a_host_that_hands_no_core_to_a_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beacon_taps, "core_dump_problem", lambda: None)


class Services:
    """Stands where the push services are. Answers 201, or what `status` has for
    an address, and keeps the requests it was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.status: dict[str, int] = {}
        self.unreachable: set[str] = set()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        address = str(request.url)
        if address in self.unreachable:
            raise httpx2.ConnectError(f"Could not connect to {address}")
        return httpx2.Response(self.status.get(address, 201))

    @property
    def sent_to(self) -> list[str]:
        return [str(request.url) for request in self.requests]


@dataclass
class World:
    """A fire on Hayes St and a medical response a block south of it, the file of
    what was investigated, and push over a stand-in for the push services."""

    path: Path
    rows: Rows
    now: list[datetime]
    engine: Engine
    runs: Runs
    services: Services
    push: Push
    taps: Taps

    def decide(self, minute: float = 6.5) -> None:
        """Have the stand-in investigate a tap at the fire, which turns crisis
        mode on for it at `minute`."""
        self.rows.tap(minute - 0.5, ref=TAP)
        self.now[0] = at(minute)
        desk = InvestigationDesk(
            EmberClient.fake(Storage.under(self.path), stand_in_investigator),
            self.runs,
            self.engine,
            self.taps,
            clock=lambda: self.now[0],
            wall_clock=lambda: self.now[0],
            workers=0,
        )
        assert desk.investigate(TAP) is not None
        assert self.runs.crisis(self.id_of("fire")) is not None

    def decide_for(self, incident_id: str, minute: float, *kinds: str) -> None:
        """Write a decision that turned crisis mode on for an incident, resting on
        signals of `kinds`, as a run's is kept."""
        tap_id = f"tap-{incident_id[-8:]}{int(minute * 10):04d}"
        self.runs.claim(tap_id, at(minute))
        self.runs.write(
            Run(
                tap_id=tap_id,
                started_at=at(minute),
                finished_at=at(minute),
                outcome=Outcome(
                    status="incident",
                    incident_id=incident_id,
                    grounds=("near",),
                    confidence="low",
                    crisis=True,
                    crisis_cited=tuple(Cited(kind, "medium", (2,)) for kind in kinds),
                ),
                model="fake",
                stand_in=True,
            )
        )

    def id_of(self, category: str) -> str:
        (found,) = [i.id for i in self.engine.incidents() if i.category == category]
        return found

    def notify(self, minute: float = 7) -> Pass:
        self.now[0] = at(minute)
        return self.push.notifier.notify(self.engine.snapshot(), at(minute))

    def app(self, *, push: bool = True) -> TestClient:
        """A server over this world, with push or with none."""
        app = create_app()
        app.state.incidents, app.state.runs = self.engine, self.runs
        if push:
            app.state.push = self.push
        return TestClient(app)

    def kept(self) -> list[tuple[Any, ...]]:
        """Every row of the subscriptions file, as it is on disk."""
        with sqlite3.connect(self.path / "push" / "mock.sqlite3") as db:
            return db.execute("SELECT * FROM subscription ORDER BY endpoint").fetchall()


@pytest.fixture
def world(tmp_path: Path) -> Any:
    rows = Rows(mock_store(tmp_path))
    now = [at(5)]
    engine = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: now[0])
    city(rows)
    runs = Runs(tmp_path / "investigate" / "mock.sqlite3", "mock")
    services = Services()
    push = open_push(
        {"BEACON_DATA_DIR": str(tmp_path)},
        engine,
        runs,
        transport=httpx2.MockTransport(services),
    )
    assert push is not None
    taps = Taps(rows.store, TapMedia(tmp_path / "taps", "mock"), clock=lambda: now[0])
    yield World(tmp_path, rows, now, engine, runs, services, push, taps)
    push.close()
    taps.close()


# Which addresses a push goes to.


def test_an_address_of_a_browsers_push_service_is_taken_as_it_was_sent() -> None:
    for address in (
        FCM,
        MOZ,
        APPLE,
        EDGE,
        "https://fcm.googleapis.com:443/fcm/send/abc",
        # Apple asks that any host under its push name be taken.
        "https://api.push.apple.com/3/device/abc",
    ):
        assert checked(address) == address
    assert audience(FCM) == "https://fcm.googleapis.com"
    assert audience(EDGE) == "https://wns2-by3p.notify.windows.com"
    assert audience("https://fcm.googleapis.com:443/fcm/send/abc") == "https://fcm.googleapis.com"


@pytest.mark.parametrize(
    "address",
    [
        # Not a push service's host, however it is dressed.
        "https://example.org/fcm/send/abc",
        "https://fcm.googleapis.com.example.org/fcm/send/abc",
        "https://examplefcm.googleapis.com/fcm/send/abc",
        "https://evilnotify.windows.com/w/abc",
        "https://.notify.windows.com/w/abc",
        "https://a..notify.windows.com/w/abc",
        "https://-a.notify.windows.com/w/abc",
        "https://notify.windows.com/w/abc",
        "https://push.apple.com/abc",
        "https://web.push.apple.com.example.org/abc",
        # A name and a password before the host, which is then another host.
        "https://fcm.googleapis.com@example.org/fcm/send/abc",
        "https://example.org\\@fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com#@example.org/",
        # Not https, not the usual port, not this machine.
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://127.0.0.1/fcm/send/abc",
        "https://localhost/fcm/send/abc",
        # Written in a way two programs can read two ways.
        "https://FCM.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com/fcm/send/a b",
        "https://fcm.googleapis.com/fcm/send/abc\n",
        "https://fcm.googleapis.com/fcm/send/ab\u00e9",
        "https://fcm.googleapis.com",
        " https://fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com/" + "a" * MAX_ENDPOINT,
        "",
        None,
        7,
        ["https://fcm.googleapis.com/fcm/send/abc"],
    ],
)
def test_any_other_address_is_refused(address: Any) -> None:
    assert checked(address) is None


# The key.


def verified(vapid: Vapid, header: str) -> dict[str, Any]:
    """The claims of a token, once its signature is checked against the key the
    header names. Worked out here from RFC 8292 and RFC 7515, not with the code
    that signs."""
    scheme, rest = header.split(" ", 1)
    assert scheme == "vapid"
    parts = dict(part.strip().split("=", 1) for part in rest.split(","))
    assert parts["k"] == vapid.public_key
    signed, _, signature = parts["t"].rpartition(".")

    def unpacked(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    point = unpacked(parts["k"])
    assert (len(point), point[0]) == (65, 4)
    raw = unpacked(signature)
    assert len(raw) == 64
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.verify(der, signed.encode(), ec.ECDSA(hashes.SHA256()))
    header_part, claims = (json.loads(unpacked(part)) for part in signed.split("."))
    assert header_part == {"typ": "JWT", "alg": "ES256"}
    return claims


def test_a_push_is_signed_for_one_push_service_for_half_a_day(tmp_path: Path) -> None:
    vapid = open_vapid({SUBJECT_VARIABLE: "mailto:ops@example.org"}, tmp_path, "mock")

    claims = verified(vapid, vapid.authorization("https://fcm.googleapis.com", at(0)))

    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": int((at(0) + TOKEN_LIFE).timestamp()),
        "sub": "mailto:ops@example.org",
    }
    assert timedelta(0) < TOKEN_LIFE <= timedelta(hours=24)
    # With nobody named, the token names nobody.
    unnamed = Vapid.generate()
    assert "sub" not in verified(unnamed, unnamed.authorization("https://x.example", at(0)))


def test_the_key_is_made_once_for_the_servers_user_only_and_read_again(tmp_path: Path) -> None:
    first = open_vapid({}, tmp_path, "mock")
    path = tmp_path / "mock.vapid.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    kept = path.read_bytes()

    again = open_vapid({}, tmp_path, "mock")

    assert again.public_key == first.public_key
    assert path.read_bytes() == kept
    # The invented scenario's server and a real one never sign with one key.
    live = open_vapid({}, tmp_path, "live")
    assert live.public_key != first.public_key
    assert (key_file("mock"), key_file("live")) == ("mock.vapid.key", "live.vapid.key")


def test_a_key_file_that_holds_no_key_is_refused_and_left_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "mock.vapid.key"
    for held in ("not a key", "", "AAAA", base64.urlsafe_b64encode(bytes(32)).decode()):
        path.write_text(held)
        with pytest.raises(VapidError) as refused:
            open_vapid({}, tmp_path, "mock")
        assert path.read_text() == held
        assert "not a key" not in str(refused.value)
        assert refused.value.__cause__ is None
        assert refused.value.__context__ is None
    with pytest.raises(VapidError, match="mailto: address or an https:// one"):
        open_vapid({SUBJECT_VARIABLE: "ops@example.org"}, tmp_path, "live")


# The subscriptions file.


def subscriptions(tmp_path: Path) -> Subscriptions:
    return Subscriptions(tmp_path / "mock.sqlite3", "mock")


def test_a_subscription_is_an_address_and_places_and_renewing_it_replaces_the_places(
    tmp_path: Path,
) -> None:
    kept = subscriptions(tmp_path)

    assert kept.put(FCM, [NEAR, FAR], at(0)) == at(0) + KEPT
    assert kept.put(FCM, [FAR], at(10)) == at(10) + KEPT

    (found,) = kept.all()
    # When it was first made is kept: a renewal is not a new subscription.
    assert (found.endpoint, found.points, found.created_at) == (FCM, (FAR,), at(0))
    with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
        columns = [column[1] for column in db.execute("PRAGMA table_info(subscription)")]
    assert columns == ["endpoint", "places", "created_at", "renewed_at"]


def test_a_subscription_is_deleted_when_asked_and_when_its_time_is_over(tmp_path: Path) -> None:
    kept = subscriptions(tmp_path)
    kept.put(FCM, [NEAR], at(0))
    kept.put(MOZ, [NEAR], at(0))
    assert kept.claim(FCM, "inc-1", at(1))
    assert kept.claim(MOZ, "inc-1", at(1))

    assert kept.remove(FCM) is True
    assert kept.remove(FCM) is False

    with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
        assert db.execute("SELECT endpoint FROM sent").fetchall() == [(MOZ,)]
    # Renewed on the twentieth day, it is kept thirty more.
    day = timedelta(days=1)
    kept.put(MOZ, [NEAR], at(0) + 20 * day)
    assert kept.sweep(at(0) + 49 * day) == 0
    assert kept.sweep(at(0) + 50 * day + timedelta(minutes=1)) == 1
    assert kept.all() == ()
    with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM sent").fetchone() == (0,)


def test_the_file_holds_only_so_many_subscriptions_and_makes_room_for_what_ran_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_SUBSCRIPTIONS", 2)
    kept = subscriptions(tmp_path)
    assert kept.put(FCM, [NEAR], at(0)) is not None
    assert kept.put(MOZ, [NEAR], at(1)) is not None

    assert kept.put(APPLE, [NEAR], at(2)) is None
    # One that is kept already is renewed all the same.
    assert kept.put(FCM, [FAR], at(3)) is not None
    assert kept.count() == 2
    # When the time of one that was not renewed is over, there is room.
    later = at(1) + KEPT + timedelta(minutes=1)
    assert kept.put(APPLE, [NEAR], later) is not None
    assert {found.endpoint for found in kept.all()} == {FCM, APPLE}


def test_a_send_is_claimed_once_and_again_only_as_often_and_as_soon_as_it_may_be(
    tmp_path: Path,
) -> None:
    kept = subscriptions(tmp_path)
    kept.put(FCM, [NEAR], at(0))

    assert kept.claim(FCM, "inc-1", at(1)) is True
    assert kept.claim(FCM, "inc-1", at(1)) is False
    # Another incident is another send.
    assert kept.claim(FCM, "inc-2", at(1)) is True
    # An address that is not kept is sent nothing.
    assert kept.claim(MOZ, "inc-1", at(1)) is False

    kept.settle(FCM, "inc-1", "again")
    wait = store_module.RETRY_AFTER
    assert kept.claim(FCM, "inc-1", at(1) + wait - timedelta(seconds=1)) is False
    assert kept.claim(FCM, "inc-1", at(1) + wait) is True
    kept.settle(FCM, "inc-1", "again")
    # Twice as long after the second try.
    assert kept.claim(FCM, "inc-1", at(1) + wait * 2) is False
    assert kept.claim(FCM, "inc-1", at(1) + wait * 3) is True
    kept.settle(FCM, "inc-1", "again")
    # Three tries in all.
    assert store_module.MAX_TRIES == 3
    assert kept.claim(FCM, "inc-1", at(600)) is False
    # What was sent, and what was refused for good, is not sent again.
    for outcome in ("sent", "failed"):
        kept.settle(FCM, "inc-2", outcome)  # type: ignore[arg-type]
        assert kept.claim(FCM, "inc-2", at(600)) is False
        with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
            db.execute("UPDATE sent SET state = 'claimed' WHERE incident_id = 'inc-2'")


def test_a_subscriptions_file_is_one_modes_and_takes_nothing_once_closed(tmp_path: Path) -> None:
    kept = subscriptions(tmp_path)
    kept.put(FCM, [NEAR], at(0))
    before = (tmp_path / "mock.sqlite3").read_bytes()

    with pytest.raises(SubscriptionsError, match="begun as the mock one"):
        Subscriptions(tmp_path / "mock.sqlite3", "live")
    assert (tmp_path / "mock.sqlite3").read_bytes() == before

    kept.close()
    for write in (
        lambda: kept.put(MOZ, [NEAR], at(1)),
        lambda: kept.remove(FCM),
        lambda: kept.sweep(at(1)),
        lambda: kept.claim(FCM, "inc-1", at(1)),
        lambda: kept.settle(FCM, "inc-1", "sent"),
    ):
        with pytest.raises(SubscriptionsError):
            write()
    assert [found.endpoint for found in kept.all()] == [FCM]


# The pass that sends.


def test_a_device_is_sent_one_push_with_no_content_when_crisis_mode_is_turned_on_near_its_place(
    world: World,
) -> None:
    world.push.subscriptions.put(FCM, [FAR, NEAR], at(5))
    world.push.subscriptions.put(MOZ, [FAR], at(5))
    # Nothing was decided yet: the fire and its signals are no reason by themselves.
    assert world.notify(6) == Pass()
    assert world.services.requests == []

    world.decide(6.5)

    assert world.notify(7) == Pass(sent=1)
    (request,) = world.services.requests
    assert (request.method, str(request.url)) == ("POST", FCM)
    # No words go to the push service: there is no body to encrypt.
    assert request.content == b""
    assert request.headers.get("content-encoding") is None
    assert (request.headers["ttl"], request.headers["urgency"]) == ("3600", "high")
    claims = verified(world.push.vapid, request.headers["authorization"])
    assert claims["aud"] == "https://fcm.googleapis.com"
    # One for the incident, however often the incidents are looked over.
    assert world.notify(7.5) == Pass()
    assert world.notify(30) == Pass()
    assert len(world.services.requests) == 1


def test_how_near_is_the_saved_place_checks_reach(world: World) -> None:
    fire = BLOCKS.points[("Hayes St", 400)]
    world.push.subscriptions.put(FCM, [offset(fire, REACH_M - 20, 0)], at(5))
    world.push.subscriptions.put(MOZ, [offset(fire, REACH_M + 20, 0)], at(5))
    world.decide()

    assert world.notify() == Pass(sent=1)
    assert world.services.sent_to == [FCM]


def test_no_push_is_sent_for_a_decision_made_before_the_device_subscribed(world: World) -> None:
    world.decide(6.5)
    world.push.subscriptions.put(FCM, [NEAR], at(6.6))

    assert world.notify(7) == Pass()
    assert world.services.requests == []


def test_no_push_is_sent_for_a_medical_response_a_closed_incident_or_signals_that_are_gone(
    world: World,
) -> None:
    medical = world.id_of("medical response")
    (response,) = [i for i in world.engine.incidents() if i.id == medical]
    assert response.point is not None
    world.push.subscriptions.put(FCM, [response.point, NEAR], at(5))
    # A decision about a medical response is never made. Were one kept, nothing
    # would be sent for it.
    fire = world.id_of("fire")
    (burning,) = [i for i in world.engine.incidents() if i.id == fire]
    signal = burning.signals[0]
    world.decide_for(medical, 6, signal.kind)
    # A decision that rested on a signal the fire does not have now.
    assert "aircraft_circling" not in {signal.kind for signal in burning.signals}
    world.decide_for(fire, 6, "aircraft_circling")

    assert world.notify(7) == Pass()
    assert world.services.requests == []

    # The engine gives a medical response no signals, which is one reason nothing
    # was sent. Were it ever to have the signal a decision cited, there would
    # still be no push: it is a medical response.
    snapshot = world.engine.snapshot()
    doctored = replace(
        snapshot,
        incidents=tuple(
            replace(incident, signals=(signal,)) if incident.id == medical else incident
            for incident in snapshot.incidents
        ),
    )
    assert world.push.notifier.notify(doctored, at(7)) == Pass()
    assert world.services.requests == []


def test_no_push_is_sent_for_an_incident_whose_record_is_closed(world: World) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.decide(6.5)
    world.rows.radio(8, "fire", "clear", units=("E36", "T6", "B2"))
    world.now[0] = at(120)
    (fire,) = [i for i in world.engine.incidents() if i.category == "fire"]
    assert fire.open is False
    assert world.runs.crisis(fire.id) is not None

    assert world.notify(120) == Pass()
    assert world.services.requests == []


def test_an_address_the_push_service_says_is_gone_is_deleted(world: World) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.push.subscriptions.put(MOZ, [NEAR], at(5))
    world.push.subscriptions.put(APPLE, [NEAR], at(5))
    world.services.status = {FCM: 410, MOZ: 404}
    world.decide()

    assert world.notify() == Pass(sent=1, gone=2)

    assert [found.endpoint for found in world.push.subscriptions.all()] == [APPLE]
    assert world.notify(30) == Pass()


def test_a_push_the_service_did_not_take_is_tried_again_three_times_in_all(world: World) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.push.subscriptions.put(MOZ, [NEAR], at(5))
    world.push.subscriptions.put(APPLE, [NEAR], at(5))
    world.services.status = {FCM: 503, MOZ: 429}
    world.services.unreachable = {APPLE}
    world.decide()

    assert world.notify(7) == Pass(again=3)
    # Not at once.
    assert world.notify(7.5) == Pass()
    assert world.notify(8.1) == Pass(again=3)
    # The third try is taken by two of them.
    world.services.status = {FCM: 503}
    world.services.unreachable = set()
    assert world.notify(10.2) == Pass(sent=2, again=1)
    assert world.notify(60) == Pass()
    assert world.services.sent_to.count(FCM) == 3
    assert len(world.services.requests) == 9
    # They are all still subscribed.
    assert len(world.push.subscriptions.all()) == 3


def test_one_token_is_signed_for_a_push_service_and_used_for_an_hour(world: World) -> None:
    other = FCM + "-2"
    for address in (FCM, other, MOZ):
        world.push.subscriptions.put(address, [NEAR], at(5))
    world.decide()

    assert world.notify(7) == Pass(sent=3)

    tokens = {str(r.url): r.headers["authorization"] for r in world.services.requests}
    # One for each push service, whoever of its devices is sent to.
    assert tokens[FCM] == tokens[other]
    assert tokens[FCM] != tokens[MOZ]
    assert verified(world.push.vapid, tokens[MOZ])["aud"] == audience(MOZ)
    # Within the hour the same token is used, and after it another is signed.
    send = world.push.notifier._send
    assert send(FCM, at(7) + desk_module.TOKEN_REUSED - timedelta(seconds=1)) == "sent"
    assert world.services.requests[-1].headers["authorization"] == tokens[FCM]
    assert send(FCM, at(7) + desk_module.TOKEN_REUSED) == "sent"
    later = world.services.requests[-1].headers["authorization"]
    assert later != tokens[FCM]
    assert verified(world.push.vapid, later)["exp"] > verified(world.push.vapid, tokens[FCM])["exp"]


def test_a_push_the_service_refuses_is_not_tried_again_and_the_subscription_stays(
    world: World,
) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.services.status = {FCM: 403}
    world.decide()

    assert world.notify(7) == Pass(failed=1)
    assert world.notify(30) == Pass()
    assert len(world.services.requests) == 1
    assert len(world.push.subscriptions.all()) == 1


def test_a_pass_sends_only_so_many_and_only_so_long_and_the_next_sends_the_rest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for address in (FCM, MOZ, APPLE):
        world.push.subscriptions.put(address, [NEAR], at(5))
    world.decide()
    monkeypatch.setattr(desk_module, "MAX_SENDS", 2)

    assert world.notify(7) == Pass(sent=2, more=True)
    assert world.notify(7.2) == Pass(sent=1)
    assert sorted(world.services.sent_to) == sorted([FCM, MOZ, APPLE])

    # And by the clock: a pass whose time is up claims nothing more.
    world.push.subscriptions.put(EDGE, [NEAR], at(5))
    ticks = iter([0.0, 0.0, desk_module.PASS_SECONDS, desk_module.PASS_SECONDS])
    timed = Notifier(
        world.push.subscriptions,
        world.runs,
        world.push.vapid,
        SourceHttp(httpx2.MockTransport(world.services)),
        monotonic=lambda: next(ticks),
    )
    with sqlite3.connect(world.path / "push" / "mock.sqlite3") as db:
        db.execute("DELETE FROM sent")
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    assert timed.notify(world.engine.snapshot(), at(8)) == Pass(sent=1, more=True)
    timed.close()


def test_nothing_of_an_address_a_place_or_an_incident_is_logged_or_raised(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for address in (FCM, MOZ, APPLE, EDGE):
        world.push.subscriptions.put(address, [NEAR], at(5))
    world.services.status = {FCM: 410, MOZ: 503, EDGE: 403}
    world.services.unreachable = {APPLE}
    world.decide()
    stop = asyncio.Event()

    async def run() -> None:
        # The server's own loop, stopped once its first pass has asked.
        task = asyncio.create_task(main_module._send_pushes(world.push, world.engine, stop))
        while len(world.services.requests) < 4:
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(run())

    assert len(world.services.requests) == 4
    logged = caplog.text
    assert "Pushes: 0 sent, 1 addresses gone and deleted, 2 to try again, 1 refused." in logged
    # A refusal is a warning: it is printed where a server's logging is as it comes.
    assert [record.levelno for record in caplog.records if "Pushes:" in record.message] == [
        logging.WARNING
    ]
    for private in ("secret", "googleapis", "mozilla", "apple", "windows", "inc-"):
        assert private not in logged
    assert f"{NEAR.lat:.3f}" not in logged


def test_a_closed_notifier_sends_nothing_and_claims_nothing(world: World) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.decide()
    world.push.notifier.close()

    assert world.notify(7) == Pass()
    assert world.services.requests == []
    # The send was not claimed, so a server that is started again makes it.
    again = Notifier(
        world.push.subscriptions,
        world.runs,
        world.push.vapid,
        SourceHttp(httpx2.MockTransport(world.services)),
    )
    assert again.notify(world.engine.snapshot(), at(7.1)) == Pass(sent=1)
    again.close()


# The words.


def test_the_words_of_a_notification_say_whose_decision_it_is_and_tell_nobody_what_to_do() -> None:
    live, mock = notice_of("live"), notice_of("mock")

    assert live.title == "Beacon: crisis mode near a saved place"
    assert "the investigator's reading and no authority's" in live.text
    # On the invented scenario it says so first, and that a stand-in decided.
    assert mock.title == "Beacon: invented scenario"
    assert mock.text.startswith("Invented scenario, not a real event. A stand-in for")
    for text in (live.title, live.text, mock.title, mock.text, ABOUT):
        said = text.lower()
        for word in ("stay", "leave", "evacuate", " go ", "shelter", "safe", "danger"):
            assert word not in said
    # A model is not called a stand-in, nor a stand-in a model.
    assert "stand-in" not in live.text
    assert "model" not in mock.text
    assert "30 days" in ABOUT
    assert timedelta(days=30) == KEPT


# The routes.


def body_of(endpoint: Any = FCM, places: Any = None) -> dict[str, Any]:
    listed = [{"lat": NEAR.lat, "lon": NEAR.lon}] if places is None else places
    return {"endpoint": endpoint, "places": listed}


def test_the_app_is_told_the_key_and_what_is_kept_before_anyone_subscribes(world: World) -> None:
    answer = world.app().get("/api/push").json()

    assert answer == {
        "mode": "mock",
        "enabled": True,
        "text": None,
        "public_key": world.push.vapid.public_key,
        "about_text": ABOUT,
        # The invented scenario's words, for a device that subscribes to it.
        "notice": {"title": notice_of("mock").title, "text": notice_of("mock").text},
        "reach_m": 1000,
        "max_places": 5,
        "kept_days": 30,
    }


def test_subscribing_keeps_the_address_and_the_places_rounded_to_a_block_and_nothing_else(
    world: World,
) -> None:
    client = world.app()
    places = [{"lat": 37.77664321, "lon": -122.42361234}, {"lat": 37.7801, "lon": -122.4199}]

    response = client.post(
        "/api/push/subscribe",
        json=body_of(places=places),
        headers={**SHOWN, "User-Agent": "A browser 1.0", "X-Forwarded-For": "203.0.113.9"},
    )

    assert response.status_code == 200
    answer = response.json()
    assert set(answer) == {"mode", "kept_until"}
    assert answer["mode"] == "mock"
    ((endpoint, kept_places, created_at, renewed_at),) = world.kept()
    assert endpoint == FCM
    assert json.loads(kept_places) == [[37.777, -122.424], [37.78, -122.42]]
    assert created_at == renewed_at
    until = datetime.fromisoformat(answer["kept_until"])
    assert KEPT - timedelta(minutes=1) < until - datetime.now(until.tzinfo) <= KEPT
    # Nothing of who asked is anywhere in the file.
    held = (world.path / "push" / "mock.sqlite3").read_bytes()
    for private in (b"browser", b"203.0.113", b"37.7766"):
        assert private not in held

    # Asked again with other places, it is the same subscription with those.
    assert client.post("/api/push/subscribe", json=body_of(), headers=SHOWN).status_code == 200
    ((_, kept_places, created_again, _),) = world.kept()
    assert json.loads(kept_places) == [[round(NEAR.lat, 3), round(NEAR.lon, 3)]]
    assert created_again == created_at


def test_a_subscription_the_routes_made_is_sent_its_push(world: World) -> None:
    client = world.app()
    assert client.post("/api/push/subscribe", json=body_of(), headers=SHOWN).status_code == 200
    world.decide(6.5)
    # The route stamps a subscription with the time it is now, which is after the
    # story's. The decision is moved to after that.
    with sqlite3.connect(world.path / "push" / "mock.sqlite3") as db:
        db.execute("UPDATE subscription SET created_at = '2026-01-01T00:00:00.000000'")

    assert world.notify(7) == Pass(sent=1)
    assert world.services.sent_to == [FCM]

    # Turned off, the address and what was sent to it are deleted.
    gone = client.post("/api/push/unsubscribe", json={"endpoint": FCM})
    assert (gone.status_code, gone.content) == (204, b"")
    assert world.kept() == []
    with sqlite3.connect(world.path / "push" / "mock.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM sent").fetchone() == (0,)
    # The answer is the same when there was none, and for an address Beacon
    # would never have kept.
    for endpoint in (FCM, "https://example.org/x"):
        again = client.post("/api/push/unsubscribe", json={"endpoint": endpoint})
        assert (again.status_code, again.content) == (204, b"")


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (body_of(endpoint="https://example.org/push/secret-1"), 422, api_push.NOT_A_PUSH_SERVICE),
        (body_of(endpoint="http://127.0.0.1:8000/secret-1"), 422, api_push.NOT_A_PUSH_SERVICE),
        (body_of(endpoint=7), 422, api_push.SUBSCRIBE_SHAPE),
        (body_of(places=[]), 422, api_push.SUBSCRIBE_SHAPE),
        (body_of(places=[{"lat": 37.7766, "lon": -122.4236}] * 6), 422, api_push.SUBSCRIBE_SHAPE),
        (
            body_of(places=[{"lat": 37.7766, "lon": -122.4236, "name": "secret-1"}]),
            422,
            api_push.SUBSCRIBE_SHAPE,
        ),
        (body_of(places=[{"lat": 40.7128, "lon": -74.006}]), 422, ELSEWHERE),
        ({**body_of(), "keys": {"p256dh": "secret-1", "auth": "secret-1"}}, 422, None),
        ({"endpoint": FCM}, 422, api_push.SUBSCRIBE_SHAPE),
        (["secret-1"], 422, api_push.SUBSCRIBE_SHAPE),
        ("[" * 9 + "secret-1", 422, api_push.SUBSCRIBE_SHAPE),
    ],
)
def test_a_request_to_be_notified_that_cannot_be_used_is_refused_in_a_fixed_sentence(
    world: World, body: Any, status: int, detail: str | None
) -> None:
    client = world.app()
    sent = body if isinstance(body, str) else json.dumps(body)

    response = client.post("/api/push/subscribe", content=sent, headers=SHOWN)

    assert response.status_code == status
    assert response.json() == {"detail": detail or api_push.SUBSCRIBE_SHAPE}
    for private in ("secret", "example.org", "127.0.0.1", "40.71", "37.77"):
        assert private not in response.text
    assert world.kept() == []


def test_a_request_to_be_notified_names_the_mode_its_sender_was_shown(world: World) -> None:
    client = world.app()

    unnamed = client.post("/api/push/subscribe", json=body_of())
    other = client.post("/api/push/subscribe", json=body_of(), headers={"Beacon-Mode": "live"})

    assert (unnamed.status_code, other.status_code) == (400, 409)
    assert "nothing of it was read" in unnamed.json()["detail"]
    assert "Nothing of the request was read" in other.json()["detail"]
    assert world.kept() == []


def test_a_request_about_notifications_is_bounded_before_it_is_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = world.app()
    large = json.dumps(body_of(endpoint=FCM + "a" * api_push.MAX_BODY_BYTES))

    too_large = client.post("/api/push/subscribe", content=large, headers=SHOWN)
    assert (too_large.status_code, too_large.json()["detail"]) == (
        413,
        "A request about notifications is at most four kilobytes.",
    )

    # Only so many are worked on at once: one more is refused with its body unread.
    admission = client.app.state.push_requests  # type: ignore[attr-defined]
    held = [admission.admit() for _ in range(8)]
    assert all(held)
    assert admission.admit() is None
    for path in ("/api/push/subscribe", "/api/push/unsubscribe"):
        busy = client.post(path, content="not read", headers=SHOWN)
        assert (busy.status_code, busy.headers["retry-after"]) == (503, "1")
    for give_back in held:
        give_back()
    assert client.post("/api/push/subscribe", json=body_of(), headers=SHOWN).status_code == 200
    assert admission.held == 0

    # A file that is full takes no more, and says so.
    monkeypatch.setattr(store_module, "MAX_SUBSCRIPTIONS", 1)
    full = client.post("/api/push/subscribe", json=body_of(endpoint=MOZ), headers=SHOWN)
    assert (full.status_code, full.json()) == (503, {"detail": api_push.FULL})
    assert admission.held == 0


def test_a_server_that_sends_no_notifications_says_so_and_keeps_nothing(world: World) -> None:
    client = world.app(push=False)

    answer = client.get("/api/push").json()
    assert (answer["enabled"], answer["text"]) == (False, OFF)
    assert (answer["public_key"], answer["about_text"], answer["notice"]) == (None, None, None)
    for path, body in (
        ("/api/push/subscribe", body_of()),
        ("/api/push/unsubscribe", {"endpoint": FCM}),
    ):
        refused = client.post(path, json=body, headers=SHOWN)
        assert (refused.status_code, refused.json()) == (503, {"detail": OFF})
    assert world.kept() == []


def test_a_server_that_is_stopping_refuses_in_a_sentence(world: World) -> None:
    client = world.app()
    world.push.subscriptions.close()

    refused = client.post("/api/push/subscribe", json=body_of(), headers=SHOWN)

    assert refused.status_code == 503
    assert refused.json()["detail"].startswith("This server is stopping")


def test_no_log_line_is_written_about_a_request_about_notifications() -> None:
    for path in ("/api/push", "/api/push/subscribe", "/api/push/unsubscribe", "/x/api/push"):
        assert _is_private(path)
    assert "/api/push" in main_module.PRIVATE_PATHS


# The server.


def test_the_server_opens_push_with_a_key_that_lasts_and_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEACON_INGEST", "0")

    with TestClient(create_app()) as client:
        first = client.get("/api/push").json()
        push: Push = client.app.state.push  # type: ignore[attr-defined]
        assert client.post("/api/push/subscribe", json=body_of(), headers=SHOWN).status_code == 200
    assert (first["enabled"], first["mode"]) == (True, "mock")
    # Closed with the server: nothing more is kept, and nothing more is sent.
    with pytest.raises(SubscriptionsError):
        push.subscriptions.put(MOZ, [NEAR], at(0))
    assert push.notifier._http.closed
    # For the server's user only.
    directory = tmp_path / "push"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert sorted(path.name for path in directory.iterdir() if path.suffix != ".sqlite3-wal") >= [
        "mock.sqlite3",
        "mock.vapid.key",
    ]

    # Started again, it signs with the same key and has the subscription still.
    with TestClient(create_app()) as client:
        again = client.get("/api/push").json()
        kept = client.app.state.push.subscriptions.all()  # type: ignore[attr-defined]
    assert again["public_key"] == first["public_key"]
    assert [found.endpoint for found in kept] == [FCM]


def test_push_is_turned_off_by_a_setting_and_a_wrong_setting_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path / "off"))
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv(PUSH_VARIABLE, "0")

    with TestClient(create_app()) as client:
        assert client.get("/api/push").json()["enabled"] is False
        assert not hasattr(client.app.state, "push")
    assert not (tmp_path / "off" / "push").exists()

    with pytest.raises(PushConfigError, match="must be 1 or 0"):
        open_push({PUSH_VARIABLE: "yes"}, world.engine, world.runs)
