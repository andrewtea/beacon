"""Web push: the key, the addresses a push goes to, the subscriptions file, the
pass that sends, the routes, and the server around them."""

import asyncio
import base64
import json
import logging
import sqlite3
import stat
import threading
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
import beacon.push.vapid as vapid_module
from beacon import taps as beacon_taps
from beacon.ember import EmberClient, Storage
from beacon.incident import Engine, Snapshot
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
    NO_CONTACT,
    OFF,
    PUSH_VARIABLE,
    TURNED_OFF,
    Notifier,
    Pass,
    Place,
    Push,
    PushConfigError,
    Subscription,
    Subscriptions,
    SubscriptionsError,
    Vapid,
    VapidError,
    audience,
    checked,
    notice_of,
    open_push,
    subject_of,
)
from beacon.push.store import PERMIT_LIFE, PERMIT_SHAPE, SENT_KEPT, SPENT
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
# Whom a push service may write to about the server under test.
CONTACT = "mailto:ops@example.org"


def near(minute: float = 5) -> Place:
    """The place three hundred metres from the fire, as one added at `minute`."""
    return Place(NEAR, at(minute))


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
        # Addresses whose push is taken (201) with a body that then fails to come.
        self.body_fails: set[str] = set()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        address = str(request.url)
        if address in self.unreachable:
            raise httpx2.ConnectError(f"Could not connect to {address}")
        if address in self.body_fails:
            return httpx2.Response(201, stream=_BrokenBody())
        return httpx2.Response(self.status.get(address, 201))

    @property
    def sent_to(self) -> list[str]:
        return [str(request.url) for request in self.requests]


class _BrokenBody(httpx2.AsyncByteStream):
    """A reply's body that breaks off as it is read."""

    async def __aiter__(self) -> Any:
        raise httpx2.ReadError("The connection was lost.")
        yield b""  # pragma: no cover


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

    @property
    def notifier(self) -> Notifier:
        assert self.push.notifier is not None
        return self.push.notifier

    @property
    def vapid(self) -> Vapid:
        assert self.push.vapid is not None
        return self.push.vapid

    def notify(self, minute: float = 7) -> Pass:
        self.now[0] = at(minute)
        return self.push.work(self.engine.snapshot, at(minute))

    def places_added(self, minute: float) -> None:
        """Make every place in the file one that was added at `minute`: the
        routes add a place at the time it really is, which is after the story's."""
        with sqlite3.connect(self.path / "push" / "mock.sqlite3") as db:
            for endpoint, places in db.execute("SELECT endpoint, places FROM subscription"):
                moved = [
                    [lat, lon, store_module._stamp(at(minute))]
                    for lat, lon, _ in json.loads(places)
                ]
                db.execute(
                    "UPDATE subscription SET places = ? WHERE endpoint = ?",
                    (json.dumps(moved), endpoint),
                )

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
        {"BEACON_DATA_DIR": str(tmp_path), SUBJECT_VARIABLE: CONTACT},
        engine,
        runs,
        transport=httpx2.MockTransport(services),
    )
    assert push is not None
    assert push.sends
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


def test_a_push_is_signed_for_one_push_service_for_half_a_day_and_names_a_contact(
    tmp_path: Path,
) -> None:
    vapid = open_vapid(tmp_path, "mock", CONTACT)

    claims = verified(vapid, vapid.authorization("https://fcm.googleapis.com", at(0)))

    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": int((at(0) + TOKEN_LIFE).timestamp()),
        "sub": CONTACT,
    }
    assert timedelta(0) < TOKEN_LIFE <= timedelta(hours=24)


def test_the_contact_is_a_mail_address_or_a_page_and_none_is_none() -> None:
    assert subject_of({}) is None
    assert subject_of({SUBJECT_VARIABLE: "  "}) is None
    for contact in (CONTACT, "https://example.org/contact"):
        assert subject_of({SUBJECT_VARIABLE: f" {contact} "}) == contact
    for contact in (
        "ops@example.org",
        "mailto:ops",
        "http://example.org/contact",
        "https://example.org/a b",
        "mailto:ops@example.org, secret@example.org",
        "https://" + "a" * 300 + ".org",
    ):
        with pytest.raises(VapidError, match="mailto: address or an https:// one") as refused:
            subject_of({SUBJECT_VARIABLE: contact})
        assert "secret" not in str(refused.value)


def test_the_key_is_made_once_for_the_servers_user_only_and_read_again(tmp_path: Path) -> None:
    first = open_vapid(tmp_path, "mock", CONTACT)
    path = tmp_path / "mock.vapid.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    kept = path.read_bytes()

    again = open_vapid(tmp_path, "mock", CONTACT)

    assert again.public_key == first.public_key
    assert path.read_bytes() == kept
    # Nothing is left of how it was written.
    assert [found.name for found in tmp_path.iterdir()] == ["mock.vapid.key"]
    # The invented scenario's server and a real one never sign with one key.
    live = open_vapid(tmp_path, "live", CONTACT)
    assert live.public_key != first.public_key
    assert (key_file("mock"), key_file("live")) == ("mock.vapid.key", "live.vapid.key")


def test_two_servers_that_start_at_once_sign_with_one_key(tmp_path: Path) -> None:
    together = threading.Barrier(8)
    found: list[str] = []

    def start() -> None:
        together.wait()
        found.append(open_vapid(tmp_path, "mock", CONTACT).public_key)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(found) == 8
    assert len(set(found)) == 1
    assert [path.name for path in tmp_path.iterdir()] == ["mock.vapid.key"]


def test_a_key_that_could_not_be_kept_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(vapid_module.os, "fsync", full)

    with pytest.raises(VapidError, match="could not be read or made") as refused:
        open_vapid(tmp_path, "mock", CONTACT)

    assert refused.value.__context__ is None
    # No key file, whole or in part, is left under any name.
    assert list(tmp_path.iterdir()) == []


def test_a_key_file_that_holds_no_key_is_refused_and_left_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "mock.vapid.key"
    for held in ("not a key", "", "AAAA", base64.urlsafe_b64encode(bytes(32)).decode()):
        path.write_text(held)
        with pytest.raises(VapidError) as refused:
            open_vapid(tmp_path, "mock", CONTACT)
        assert path.read_text() == held
        assert "not a key" not in str(refused.value)
        assert refused.value.__cause__ is None
        assert refused.value.__context__ is None


# The subscriptions file.


def subscriptions(tmp_path: Path) -> Subscriptions:
    return Subscriptions(tmp_path / "mock.sqlite3", "mock")


def test_a_subscription_is_an_address_and_places_and_a_place_keeps_when_it_was_added(
    tmp_path: Path,
) -> None:
    kept = subscriptions(tmp_path)

    assert kept.put(FCM, [NEAR, FAR], at(0)) == at(0) + KEPT
    # Renewed without the first place, and then with it again.
    assert kept.put(FCM, [FAR], at(10)) == at(10) + KEPT
    assert kept.put(FCM, [NEAR, FAR, FAR], at(20)) == at(20) + KEPT

    (found,) = kept.all()
    # A place that was named all along keeps the time it was added. One that was
    # removed and named again was added when it was named again.
    assert found == Subscription(FCM, (Place(NEAR, at(20)), Place(FAR, at(0))))
    with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
        columns = [column[1] for column in db.execute("PRAGMA table_info(subscription)")]
        sent = [column[1] for column in db.execute("PRAGMA table_info(sent)")]
    # Every column is one the app's person is told of before they turn
    # notifications on (`ABOUT`).
    assert columns == ["endpoint", "places", "renewed_at"]
    assert sent == ["endpoint", "incident_id", "claimed_at", "tries", "state"]


def test_a_subscription_is_deleted_when_asked_and_when_its_time_is_over(tmp_path: Path) -> None:
    kept = subscriptions(tmp_path)
    kept.put(FCM, [NEAR], at(0))
    kept.put(MOZ, [NEAR], at(0))
    assert kept.claim(FCM, "inc-1", near(0), at(1))
    assert kept.claim(MOZ, "inc-1", near(0), at(1))

    assert kept.remove(FCM) is True
    assert kept.remove(FCM) is False

    with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
        assert db.execute("SELECT endpoint FROM sent").fetchall() == [(MOZ,)]
    # Renewed on the twentieth day, it is kept thirty more.
    day = timedelta(days=1)
    kept.put(MOZ, [NEAR], at(0) + 20 * day)
    # What was sent is kept two days and no longer, whatever the subscription.
    assert kept.claim(MOZ, "inc-2", near(0), at(0) + 20 * day)
    kept.sweep(at(0) + 20 * day + SENT_KEPT - timedelta(minutes=1))
    assert set(kept.tries()) == {(MOZ, "inc-2")}
    kept.sweep(at(0) + 20 * day + SENT_KEPT + timedelta(minutes=1))
    assert kept.tries() == {}
    assert kept.claim(MOZ, "inc-3", near(0), at(0) + 48 * day)
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
    place = near(0)

    assert kept.claim(FCM, "inc-1", place, at(1)) is True
    assert kept.claim(FCM, "inc-1", place, at(1)) is False
    # Another incident is another send.
    assert kept.claim(FCM, "inc-2", place, at(1)) is True
    # An address that is not kept is sent nothing.
    assert kept.claim(MOZ, "inc-1", place, at(1)) is False

    kept.settle(FCM, "inc-1", "again")
    wait = store_module.RETRY_AFTER
    assert kept.claim(FCM, "inc-1", place, at(1) + wait - timedelta(seconds=1)) is False
    assert kept.claim(FCM, "inc-1", place, at(1) + wait) is True
    kept.settle(FCM, "inc-1", "again")
    # Twice as long after the second try.
    assert kept.claim(FCM, "inc-1", place, at(1) + wait * 2) is False
    assert kept.claim(FCM, "inc-1", place, at(1) + wait * 3) is True
    kept.settle(FCM, "inc-1", "again")
    # Three tries in all.
    assert store_module.MAX_TRIES == 3
    assert kept.claim(FCM, "inc-1", place, at(600)) is False
    # What was sent, and what was refused for good, is not sent again.
    for outcome in ("sent", "failed"):
        kept.settle(FCM, "inc-2", outcome)  # type: ignore[arg-type]
        assert kept.claim(FCM, "inc-2", place, at(600)) is False
        with sqlite3.connect(tmp_path / "mock.sqlite3") as db:
            db.execute("UPDATE sent SET state = 'claimed' WHERE incident_id = 'inc-2'")


def test_a_claim_whose_server_was_stopped_before_it_heard_is_tried_again_later(
    tmp_path: Path,
) -> None:
    kept = subscriptions(tmp_path)
    kept.put(FCM, [NEAR], at(0))
    assert kept.claim(FCM, "inc-1", near(0), at(1)) is True
    # The server is stopped here: the claim is never settled. Another is started.
    again = Subscriptions(tmp_path / "mock.sqlite3", "mock")
    stale = store_module.CLAIM_STALE

    # Not while the first server's pass could still be under way.
    assert again.claim(FCM, "inc-1", near(0), at(1) + stale - timedelta(seconds=1)) is False
    assert again.claim(FCM, "inc-1", near(0), at(1) + stale) is True
    # It counts as a try: three in all, however they ended.
    assert again.tries()[(FCM, "inc-1")].tries == 2
    assert again.claim(FCM, "inc-1", near(0), at(1) + stale * 2) is True
    assert again.claim(FCM, "inc-1", near(0), at(1) + stale * 9) is False
    assert stale > timedelta(seconds=desk_module.PASS_SECONDS) * 4


def test_a_send_is_claimed_only_for_a_place_the_subscription_still_names(tmp_path: Path) -> None:
    kept = subscriptions(tmp_path)
    kept.put(FCM, [NEAR, FAR], at(0))
    # A pass reads the subscription, and the person then removes the place.
    kept.put(FCM, [FAR], at(1))

    assert kept.claim(FCM, "inc-1", near(0), at(2)) is False
    # Named again, it is a place added then, and not the one the pass read.
    kept.put(FCM, [FAR, NEAR], at(3))
    assert kept.claim(FCM, "inc-1", near(0), at(4)) is False
    assert kept.claim(FCM, "inc-1", near(3), at(4)) is True
    assert kept.tries()[(FCM, "inc-1")].tries == 1


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
        lambda: kept.claim(FCM, "inc-1", near(0), at(1)),
        lambda: kept.settle(FCM, "inc-1", "sent"),
    ):
        with pytest.raises(SubscriptionsError):
            write()
    assert [found.endpoint for found in kept.all()] == [FCM]


_PAIRS = (
    "subscription (endpoint TEXT PRIMARY KEY, places TEXT NOT NULL, "
    "created_at TEXT NOT NULL, renewed_at TEXT NOT NULL)",
    (FCM, "[[37.776, -122.424]]", "2026-11-01T00:00:00.000000", "2026-11-20T00:00:00.000000"),
)
_TIMED = (
    "subscription (endpoint TEXT PRIMARY KEY, places TEXT NOT NULL, renewed_at TEXT NOT NULL)",
    (FCM, '[[37.776, -122.424, "2026-11-01T00:00:00.000000"]]', "2026-11-20T00:00:00.000000"),
)


@pytest.mark.parametrize(
    ("shape", "table", "row"),
    [
        # The first shape that was written: a place as a pair, and a time made.
        (1, *_PAIRS),
        # The same number, with a place's time and without the time made.
        (1, *_TIMED),
        # The second: as now, and an address taken without a permit.
        (2, *_TIMED),
        # The third: as now, from when a device was handed words for the invented
        # scenario that said who decided.
        (3, *_TIMED),
    ],
)
def test_a_file_of_the_earlier_shape_has_its_subscriptions_deleted_and_stays_its_modes(
    tmp_path: Path, shape: int, table: str, row: tuple[str, ...]
) -> None:
    path = tmp_path / "mock.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {table}")
        db.execute(f"INSERT INTO subscription VALUES ({', '.join('?' * len(row))})", row)
        db.execute(
            "CREATE TABLE sent (endpoint TEXT NOT NULL, incident_id TEXT NOT NULL, "
            "claimed_at TEXT NOT NULL, tries INTEGER NOT NULL, state TEXT NOT NULL, "
            "PRIMARY KEY (endpoint, incident_id))"
        )
        db.execute(
            "INSERT INTO sent VALUES (?, 'inc-1', '2026-11-20T00:00:00.000000', 1, 'sent')", (FCM,)
        )
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('mode', 'mock')")
        db.execute(f"PRAGMA user_version = {shape}")

    # It is still the mock store's, and is refused as the live one's as it was.
    before = path.read_bytes()
    with pytest.raises(SubscriptionsError, match="begun as the mock one"):
        Subscriptions(path, "live")
    assert path.read_bytes() == before

    kept = Subscriptions(path, "mock")

    assert (kept.all(), kept.tries()) == ((), {})
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)
    # It is a file of this shape from then on, and keeps what it is given.
    kept.put(FCM, [NEAR], at(0))
    assert kept.claim(FCM, "inc-1", near(0), at(1))
    kept.close()
    again = Subscriptions(path, "mock")
    assert [found.endpoint for found in again.all()] == [FCM]
    assert len(again.tries()) == 1
    again.close()


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
    claims = verified(world.vapid, request.headers["authorization"])
    assert (claims["aud"], claims["sub"]) == ("https://fcm.googleapis.com", CONTACT)
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


def test_a_push_is_for_crisis_mode_turned_on_near_a_place_the_device_had_named_by_then(
    world: World,
) -> None:
    # One device names the place before the decision, one after it, and one had
    # a far place before and adds the near one after.
    world.push.subscriptions.put(FCM, [NEAR], at(6))
    world.push.subscriptions.put(APPLE, [FAR], at(6))
    world.decide(6.5)
    world.push.subscriptions.put(MOZ, [NEAR], at(6.6))
    world.push.subscriptions.put(APPLE, [FAR, NEAR], at(6.6))

    assert world.notify(7) == Pass(sent=1)
    assert world.services.sent_to == [FCM]
    # A renewal changes nothing of that: the place keeps when it was added.
    world.push.subscriptions.put(MOZ, [NEAR], at(7.1))
    assert world.notify(7.5) == Pass()

    # A place named at the very moment of the decision was not named before it.
    decided = world.runs.crisis(world.id_of("fire"))
    assert decided is not None
    world.push.subscriptions.put(EDGE, [NEAR], decided.decided_at)
    assert world.notify(8) == Pass()
    world.push.subscriptions.remove(EDGE)
    world.push.subscriptions.put(EDGE, [NEAR], decided.decided_at - timedelta(microseconds=1))
    assert world.notify(8.5) == Pass(sent=1)


def test_a_place_removed_after_a_pass_read_it_brings_no_push(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.push.subscriptions.put(FCM, [NEAR, FAR], at(5))
    world.decide()
    read = world.push.subscriptions.all

    def read_and_then_the_place_is_removed() -> Any:
        found = read()
        world.push.subscriptions.put(FCM, [FAR], at(6.9))
        return found

    monkeypatch.setattr(world.push.subscriptions, "all", read_and_then_the_place_is_removed)

    assert world.notify(7) == Pass()
    assert world.services.requests == []
    assert world.push.subscriptions.tries() == {}


def test_no_push_is_sent_from_a_point_beacon_took_from_a_511_event(world: World) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.decide()
    snapshot = world.engine.snapshot()
    (fire,) = [incident for incident in snapshot.incidents if incident.category == "fire"]
    assert fire.point_from == "blocks"
    # The same incident, had Beacon no point for its block and only a closure's:
    # a notification says it is near a place, and could not carry 511's credit.
    from_511 = replace(
        snapshot,
        incidents=tuple(
            replace(incident, point_from="closure") if incident.id == fire.id else incident
            for incident in snapshot.incidents
        ),
    )

    assert world.notifier.notify(from_511, at(7)) == Pass()
    assert world.services.requests == []
    assert world.notifier.notify(snapshot, at(7)) == Pass(sent=1)


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
    assert world.notifier.notify(doctored, at(7)) == Pass()
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


def test_a_push_the_service_took_is_sent_whatever_becomes_of_the_reply_after_its_status(
    world: World,
) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.services.body_fails = {FCM}
    world.decide()

    # The status said the push was taken. Asking again would notify twice.
    assert world.notify(7) == Pass(sent=1)
    assert world.notify(30) == Pass()
    assert len(world.services.requests) == 1


def test_a_send_whose_server_was_stopped_before_it_heard_is_made_again(world: World) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.decide()
    # A pass claims the send, and the server is stopped before the push service
    # answers: the claim is never settled.
    fire = world.id_of("fire")
    assert world.push.subscriptions.claim(FCM, fire, near(5), at(7))

    assert world.notify(8) == Pass()
    stale = store_module.CLAIM_STALE.total_seconds() / 60
    # Best effort: whether the push service took the first is not known, so the
    # device may be notified twice.
    assert world.notify(7 + stale) == Pass(sent=1)
    assert world.notify(8 + stale) == Pass()
    assert world.services.sent_to == [FCM]


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
    assert verified(world.vapid, tokens[MOZ])["aud"] == audience(MOZ)
    # Within the hour the same token is used, and after it another is signed.
    send = world.notifier._send
    assert send(FCM, at(7) + desk_module.TOKEN_REUSED - timedelta(seconds=1), 5.0) == "sent"
    assert world.services.requests[-1].headers["authorization"] == tokens[FCM]
    assert send(FCM, at(7) + desk_module.TOKEN_REUSED, 5.0) == "sent"
    later = world.services.requests[-1].headers["authorization"]
    assert later != tokens[FCM]
    assert verified(world.vapid, later)["exp"] > verified(world.vapid, tokens[FCM])["exp"]


def test_only_so_many_tokens_are_kept_and_none_past_its_hour(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desk_module, "MAX_TOKENS", 2)
    notifier = world.notifier
    hosts = [f"https://wns{number}.notify.windows.com/w/abc" for number in range(4)]

    for minute, host in enumerate(hosts):
        assert notifier._send(host, at(minute), 5.0) == "sent"

    # The two signed last are kept, however many push services there were.
    assert set(notifier._tokens) == {audience(hosts[2]), audience(hosts[3])}
    # A pass an hour on keeps none of them: no push went to their services since.
    world.notify(3 + desk_module.TOKEN_REUSED.total_seconds() / 60)
    assert notifier._tokens == {}


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


def test_a_pass_sends_only_so_many_and_the_next_sends_the_rest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for address in (FCM, MOZ, APPLE):
        world.push.subscriptions.put(address, [NEAR], at(5))
    world.decide()
    monkeypatch.setattr(desk_module, "MAX_SENDS", 2)

    assert world.notify(7) == Pass(sent=2, more=True)
    assert world.notify(7.2) == Pass(sent=1)
    assert sorted(world.services.sent_to) == sorted([FCM, MOZ, APPLE])


def test_what_was_sent_before_takes_none_of_a_pass_so_the_last_devices_are_reached(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    addresses = [f"{FCM}-{number}" for number in range(7)]
    for address in addresses:
        world.push.subscriptions.put(address, [NEAR], at(5))
    world.decide()
    monkeypatch.setattr(desk_module, "MAX_SENDS", 3)
    writes: list[str] = []
    claim = world.push.subscriptions.claim

    def counted(endpoint: str, *rest: Any) -> bool:
        writes.append(endpoint)
        return claim(endpoint, *rest)

    monkeypatch.setattr(world.push.subscriptions, "claim", counted)

    assert world.notify(7) == Pass(sent=3, more=True)
    assert world.notify(7.2) == Pass(sent=3, more=True)
    assert world.notify(7.4) == Pass(sent=1)
    # Every device once, in the order they subscribed, and a pass asked the file
    # about no send it had already made.
    assert world.services.sent_to == addresses
    assert writes == addresses


class _Clock:
    """A clock a test moves by hand, in seconds."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


def timed_push(world: World, clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> tuple[Push, Any]:
    """The world's push with a clock a test moves, over push services that take
    all the time they are given, and the list of how long each was given."""
    waited: list[float] = []
    fetch = desk_module.fetch

    def slow(*args: Any, **named: Any) -> bytes:
        waited.append(named["max_seconds"])
        clock.seconds += named["max_seconds"]
        return fetch(*args, **named)

    monkeypatch.setattr(desk_module, "fetch", slow)
    timed = Notifier(
        world.push.subscriptions,
        world.runs,
        world.vapid,
        SourceHttp(httpx2.MockTransport(world.services)),
        monotonic=clock,
    )
    return replace(world.push, notifier=timed), waited


def test_a_pass_starts_no_send_past_its_time_which_runs_from_before_it_reads_anything(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for address in (FCM, MOZ, APPLE):
        world.push.subscriptions.put(address, [NEAR], at(5))
    world.decide()
    clock = _Clock()
    push, waited = timed_push(world, clock, monkeypatch)

    def slow_snapshot() -> Snapshot:
        # Reading the incidents takes half of the pass's time, which counts.
        clock.seconds += desk_module.PASS_SECONDS - 16.0
        return world.engine.snapshot()

    # A claim is a write, which can wait for the file.
    claim = world.push.subscriptions.claim

    def slow_claim(*args: Any) -> bool:
        clock.seconds += 1.5
        return claim(*args)

    monkeypatch.setattr(world.push.subscriptions, "claim", slow_claim)

    assert push.work(slow_snapshot, at(7)) == Pass(sent=2, more=True)

    # No push service is waited for longer than a send may take, or than the pass
    # has left once the send is claimed, and the third device is left for later.
    assert waited == [desk_module.SEND_SECONDS, 3.0]
    assert clock.seconds == desk_module.PASS_SECONDS
    assert desk_module.SEND_SECONDS < desk_module.PASS_SECONDS
    assert set(world.push.subscriptions.tries()) == {
        (FCM, world.id_of("fire")),
        (MOZ, world.id_of("fire")),
    }
    push.notifier.close()  # type: ignore[union-attr]


def test_a_pass_whose_time_is_over_looks_at_no_subscription_and_says_it_stopped(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two devices, with nothing due for either.
    world.push.subscriptions.put(FCM, [FAR], at(5))
    world.push.subscriptions.put(MOZ, [FAR], at(5))
    world.decide()
    clock = _Clock()
    push, waited = timed_push(world, clock, monkeypatch)

    def slow_snapshot() -> Snapshot:
        clock.seconds += desk_module.PASS_SECONDS
        return world.engine.snapshot()

    # It does not say that nothing is due: it did not look.
    assert push.work(slow_snapshot, at(7)) == Pass(more=True)
    assert push.work(world.engine.snapshot, at(7.5)) == Pass()
    assert (waited, world.push.subscriptions.tries()) == ([], {})
    push.notifier.close()  # type: ignore[union-attr]


def test_a_pass_reads_only_what_was_sent_about_the_incidents_it_has_a_push_due_for(
    world: World,
) -> None:
    kept = world.push.subscriptions
    kept.put(FCM, [NEAR], at(5))
    assert kept.claim(FCM, "inc-old", near(), at(5))
    assert kept.claim(FCM, "inc-older", near(), at(5))

    assert set(kept.tries(["inc-old", "inc-old"])) == {(FCM, "inc-old")}
    assert kept.tries([]) == {}
    assert len(kept.tries()) == 2


def test_a_push_service_that_does_not_answer_in_time_is_asked_again_later(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.decide()

    class Silent(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(30)
            return httpx2.Response(201)

    monkeypatch.setattr(desk_module, "SEND_SECONDS", 0.05)
    waiting = Notifier(world.push.subscriptions, world.runs, world.vapid, SourceHttp(Silent()))

    assert waiting.notify(world.engine.snapshot(), at(7)) == Pass(again=1)
    waiting.close()


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
    world.notifier.close()

    assert world.notify(7) == Pass()
    assert world.services.requests == []
    # The send was not claimed, so a server that is started again makes it.
    again = Notifier(
        world.push.subscriptions,
        world.runs,
        world.vapid,
        SourceHttp(httpx2.MockTransport(world.services)),
    )
    assert again.notify(world.engine.snapshot(), at(7.1)) == Pass(sent=1)
    again.close()


# The words.


def test_the_words_of_a_notification_say_whose_decision_it_is_and_tell_nobody_what_to_do() -> None:
    live, mock = notice_of("live"), notice_of("mock")

    assert live.title == "Beacon: crisis mode near a saved place"
    assert "the investigator's reading and no authority's" in live.text
    # On the invented scenario it says so first. It does not say who decided: the
    # words on a device can be older than the telling, whose investigator is a
    # stand-in or a model, and the app says which.
    assert mock.title == "Beacon: invented scenario"
    assert mock.text.startswith("Invented scenario, not a real event. Beacon's investigator,")
    assert "which the app names as a model or a stand-in for one" in mock.text
    assert "no authority's" in mock.text
    for text in (live.title, live.text, mock.title, mock.text, ABOUT):
        said = text.lower()
        for word in ("stay", "leave", "evacuate", " go ", "shelter", "safe", "danger"):
            assert word not in said
    # On real sources nothing is a stand-in's.
    assert "stand-in" not in live.text
    assert "30 days" in ABOUT
    assert timedelta(days=30) == KEPT
    assert "2 days at most" in ABOUT
    assert timedelta(days=2) == SENT_KEPT
    # It names everything the file holds beside an address, and does not say the
    # push service learns nothing.
    for kept in (
        "a random number it handed this device, which is tied to no address and no place",
        "for 2 minutes at most",
        "when this server was first told of it",
        "when this device last renewed all that",
        "the incident's id",
        "how many times it tried",
    ):
        assert kept in ABOUT
    # It says no more than the server knows: not when a place was saved, not when
    # the app was opened, and a deletion only once the request for it has come.
    for more in ("when you added it", "last opened", "when you turn notifications off;"):
        assert more not in ABOUT
    assert "deletes all of this when it receives the request your device sends" in ABOUT
    assert "if this server had been told of the place by then" in ABOUT
    assert "it does learn that Beacon sent this device a message, and when" in ABOUT
    assert "nothing else" not in ABOUT
    # Sending is best effort, and the words do not promise more.
    assert "can fail to arrive, and one can arrive twice" in ABOUT


# The routes.


# A permit no server gave: it has the shape of one.
NO_PERMIT = "p" * 32


def body_of(endpoint: Any = FCM, places: Any = None, permit: Any = NO_PERMIT) -> dict[str, Any]:
    listed = [{"lat": NEAR.lat, "lon": NEAR.lon}] if places is None else places
    return {"endpoint": endpoint, "places": listed, "permit": permit}


def permit_from(client: TestClient, revoke: Any = ()) -> str:
    """A permit the server gives, as the app asks for one before each request to
    be notified."""
    answer = client.post("/api/push/permit", json={"revoke": list(revoke)}, headers=SHOWN)
    assert answer.status_code == 200
    return answer.json()["permit"]


def subscribe(client: TestClient, headers: dict[str, str] | None = None, **body: Any) -> Any:
    """Ask to be notified as the app does: with a permit asked for just before."""
    return client.post(
        "/api/push/subscribe",
        json=body_of(**body, permit=permit_from(client)),
        headers={**SHOWN, **(headers or {})},
    )


def test_the_app_is_told_the_key_and_what_is_kept_before_anyone_subscribes(world: World) -> None:
    answer = world.app().get("/api/push").json()

    assert answer == {
        "mode": "mock",
        "enabled": True,
        "text": None,
        "public_key": world.vapid.public_key,
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

    response = subscribe(
        client,
        headers={"User-Agent": "A browser 1.0", "X-Forwarded-For": "203.0.113.9"},
        places=places,
    )

    assert response.status_code == 200
    answer = response.json()
    assert set(answer) == {"mode", "kept_until"}
    assert answer["mode"] == "mock"
    ((endpoint, kept_places, renewed_at),) = world.kept()
    assert endpoint == FCM
    # Each place rounded, with when it was added, which is when it was first sent.
    assert json.loads(kept_places) == [
        [37.777, -122.424, renewed_at],
        [37.78, -122.42, renewed_at],
    ]
    until = datetime.fromisoformat(answer["kept_until"])
    assert KEPT - timedelta(minutes=1) < until - datetime.now(until.tzinfo) <= KEPT
    # Nothing of who asked is anywhere in the file.
    held = (world.path / "push" / "mock.sqlite3").read_bytes()
    for private in (b"browser", b"203.0.113", b"37.7766"):
        assert private not in held

    # The permit was used up with it, and the file holds none.
    assert world.push.subscriptions.permits() == 0

    # Asked again with other places, it is the same subscription with those.
    assert subscribe(client).status_code == 200
    ((_, kept_places, renewed_again),) = world.kept()
    assert json.loads(kept_places) == [[round(NEAR.lat, 3), round(NEAR.lon, 3), renewed_again]]
    assert renewed_again > renewed_at


def test_a_subscription_the_routes_made_is_sent_its_push(world: World) -> None:
    client = world.app()
    assert subscribe(client).status_code == 200
    world.places_added(5)
    world.decide(6.5)

    assert world.notify(7) == Pass(sent=1)
    assert world.services.sent_to == [FCM]

    # Turned off, the address and what was sent to it are deleted.
    gone = client.post("/api/push/unsubscribe", json={"endpoint": FCM}, headers=SHOWN)
    assert (gone.status_code, gone.content) == (204, b"")
    assert world.kept() == []
    with sqlite3.connect(world.path / "push" / "mock.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM sent").fetchone() == (0,)
    # The answer is the same when there was none, and for an address Beacon
    # would never have kept.
    for endpoint in (FCM, "https://example.org/x"):
        again = client.post("/api/push/unsubscribe", json={"endpoint": endpoint}, headers=SHOWN)
        assert (again.status_code, again.content) == (204, b"")


def test_an_address_is_said_to_be_deleted_only_by_the_server_whose_file_it_is_in(
    world: World,
) -> None:
    client = world.app()
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    gone = {"endpoint": FCM}

    # 204 says this mode's file does not hold the address. A request that names
    # no mode, or the other mode's, is refused unread, and nothing is deleted.
    unnamed = client.post("/api/push/unsubscribe", json=gone)
    other = client.post("/api/push/unsubscribe", json=gone, headers={"Beacon-Mode": "live"})
    assert (unnamed.status_code, other.status_code) == (400, 409)
    assert "nothing was kept or deleted" in other.json()["detail"]
    assert len(world.kept()) == 1
    # Nor by a server that is too busy, or stopping.
    admission = client.app.state.push_requests  # type: ignore[attr-defined]
    held = [admission.admit() for _ in range(8)]
    busy = client.post("/api/push/unsubscribe", json=gone, headers=SHOWN)
    for give_back in held:
        give_back()
    assert busy.status_code == 503
    assert len(world.kept()) == 1
    assert client.post("/api/push/unsubscribe", json=gone, headers=SHOWN).status_code == 204
    assert world.kept() == []
    world.push.subscriptions.close()
    stopping = client.post("/api/push/unsubscribe", json=gone, headers=SHOWN)
    assert stopping.status_code == 503


# A deletion is final: a request that was already on its way puts nothing back.


def test_a_permit_is_good_once_for_two_minutes_and_only_so_many_are_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = subscriptions(tmp_path)

    first = kept.permit(at(0))
    assert first is not None
    assert PERMIT_SHAPE.fullmatch(first)
    assert kept.put(FCM, [NEAR], at(1), permit=first) == at(1) + KEPT
    # Used up: the same request, come again, keeps nothing and changes nothing.
    assert kept.put(FCM, [FAR], at(1.5), permit=first) == SPENT
    assert kept.put(MOZ, [NEAR], at(1.5), permit=NO_PERMIT) == SPENT
    assert [(found.endpoint, found.places) for found in kept.all()] == [(FCM, (near(1),))]

    # Its time is over two minutes after it was given, to the second.
    late, in_time = kept.permit(at(2)), kept.permit(at(2))
    assert late is not None
    assert in_time is not None
    assert kept.put(MOZ, [NEAR], at(4) - timedelta(seconds=1), permit=in_time) != SPENT
    assert kept.put(APPLE, [NEAR], at(4), permit=late) == SPENT
    assert kept.permits() == 0
    # One nobody used is deleted when its time is over.
    assert kept.permit(at(5)) is not None
    kept.sweep(at(7) - timedelta(seconds=1))
    assert kept.permits() == 1
    kept.sweep(at(7))
    assert kept.permits() == 0

    # Only so many are out at once, and those whose time is over make room.
    monkeypatch.setattr(store_module, "MAX_PERMITS", 2)
    assert kept.permit(at(10)) is not None
    assert kept.permit(at(10)) is not None
    assert kept.permit(at(11)) is None
    assert kept.permit(at(12)) is not None
    kept.close()
    with pytest.raises(SubscriptionsError):
        kept.permit(at(13))


@pytest.mark.parametrize("first", ["the deletion", "the request to be notified"])
def test_whichever_comes_first_a_deletion_leaves_no_address_behind(
    world: World, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    client = world.app()
    kept = world.push.subscriptions
    # A device asks to be notified, and the server has the request and is about
    # to write it when the device, which gave up on it and turned notifications
    # off, asks for the address to be deleted, naming the request's permit.
    permit = permit_from(client)
    late = body_of(permit=permit)
    gone = {"endpoint": FCM, "permits": [permit]}
    put = kept.put
    deleted: list[int] = []

    def held(*args: Any, **named: Any) -> Any:
        # The deletion is asked for, and answered, while the write waits.
        if first == "the deletion":
            answer = client.post("/api/push/unsubscribe", json=gone, headers=SHOWN)
            deleted.append(answer.status_code)
        return put(*args, **named)

    monkeypatch.setattr(kept, "put", held)

    answer = client.post("/api/push/subscribe", json=late, headers=SHOWN)

    if first == "the deletion":
        # The device was told that the address is deleted, and it stays so: the
        # request that comes after keeps nothing, and says why.
        assert deleted == [204]
        assert (answer.status_code, answer.json()) == (410, {"detail": api_push.PERMIT_SPENT})
    else:
        assert answer.status_code == 200
        assert len(world.kept()) == 1
        assert client.post("/api/push/unsubscribe", json=gone, headers=SHOWN).status_code == 204
    assert world.kept() == []
    assert kept.permits() == 0
    # And the same request, sent once more by anything on the way, keeps nothing.
    again = client.post("/api/push/subscribe", json=late, headers=SHOWN)
    assert again.status_code == 410
    assert world.kept() == []


def test_an_older_request_to_be_notified_cannot_follow_a_newer_one(world: World) -> None:
    client = world.app()
    both = [{"lat": NEAR.lat, "lon": NEAR.lon}, {"lat": FAR.lat, "lon": FAR.lon}]
    # A request with two places is out, and the device gave up on it. A place is
    # removed, and the next request's permit takes the earlier one's away.
    earlier = permit_from(client)
    later = permit_from(client, revoke=[earlier])

    assert (
        client.post("/api/push/subscribe", json=body_of(permit=later), headers=SHOWN).status_code
        == 200
    )
    late = client.post(
        "/api/push/subscribe", json=body_of(places=both, permit=earlier), headers=SHOWN
    )

    assert late.status_code == 410
    ((_, places, _),) = world.kept()
    assert [place[:2] for place in json.loads(places)] == [[round(NEAR.lat, 3), round(NEAR.lon, 3)]]


@pytest.mark.parametrize(
    ("path", "body", "shape"),
    [
        ("/api/push/permit", {}, api_push.PERMIT_REQUEST_SHAPE),
        ("/api/push/permit", {"revoke": "secret-1"}, api_push.PERMIT_REQUEST_SHAPE),
        ("/api/push/permit", {"revoke": ["secret-1"]}, api_push.PERMIT_REQUEST_SHAPE),
        ("/api/push/permit", {"revoke": ["p" * 32] * 17}, api_push.PERMIT_REQUEST_SHAPE),
        ("/api/push/permit", {"revoke": [], "endpoint": FCM}, api_push.PERMIT_REQUEST_SHAPE),
        ("/api/push/unsubscribe", {"endpoint": FCM, "permits": ["secret-1"]}, None),
        ("/api/push/unsubscribe", {"endpoint": FCM, "permits": "p" * 32}, None),
        ("/api/push/unsubscribe", {"endpoint": FCM, "more": 1}, None),
    ],
)
def test_a_request_about_a_permit_that_cannot_be_used_is_refused_and_deletes_nothing(
    world: World, path: str, body: Any, shape: str | None
) -> None:
    client = world.app()
    world.push.subscriptions.put(FCM, [NEAR], at(5))

    response = client.post(path, json=body, headers=SHOWN)

    assert response.status_code == 422
    assert response.json() == {"detail": shape or api_push.UNSUBSCRIBE_SHAPE}
    assert "secret" not in response.text
    assert len(world.kept()) == 1
    assert world.push.subscriptions.permits() == 0


def test_a_permit_is_given_only_by_a_server_that_sends_in_the_mode_that_is_named(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = world.app()

    given = client.post("/api/push/permit", json={"revoke": []}, headers=SHOWN)
    assert given.status_code == 200
    answer = given.json()
    assert set(answer) == {"mode", "permit", "seconds"}
    assert (answer["mode"], answer["seconds"]) == ("mock", 120)
    assert timedelta(seconds=120) == PERMIT_LIFE

    unnamed = client.post("/api/push/permit", json={"revoke": []})
    other = client.post("/api/push/permit", json={"revoke": []}, headers={"Beacon-Mode": "live"})
    assert (unnamed.status_code, other.status_code) == (400, 409)
    monkeypatch.setattr(store_module, "MAX_PERMITS", 1)
    full = client.post("/api/push/permit", json={"revoke": []}, headers=SHOWN)
    assert (full.status_code, full.json()) == (503, {"detail": api_push.NO_PERMITS})
    # A permit that is withdrawn makes room for the next.
    again = client.post("/api/push/permit", json={"revoke": [answer["permit"]]}, headers=SHOWN)
    assert again.status_code == 200
    assert world.push.subscriptions.permits() == 1

    none = world.app(push=False)
    off = none.post("/api/push/permit", json={"revoke": []}, headers=SHOWN)
    assert (off.status_code, off.json()) == (503, {"detail": OFF})


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
        ({"endpoint": FCM, "places": [{"lat": 37.7766, "lon": -122.4236}]}, 422, None),
        (body_of(permit="secret-1"), 422, api_push.SUBSCRIBE_SHAPE),
        (body_of(permit=["p" * 32]), 422, api_push.SUBSCRIBE_SHAPE),
        # A permit of the right shape that no server gave, or that was used.
        (body_of(), 410, api_push.PERMIT_SPENT),
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
    for path in ("/api/push/permit", "/api/push/subscribe", "/api/push/unsubscribe"):
        busy = client.post(path, content="not read", headers=SHOWN)
        assert (busy.status_code, busy.headers["retry-after"]) == (503, "1")
    for give_back in held:
        give_back()
    assert subscribe(client).status_code == 200
    assert admission.held == 0

    # A file that is full takes no more, and says so.
    monkeypatch.setattr(store_module, "MAX_SUBSCRIPTIONS", 1)
    full = subscribe(client, endpoint=MOZ)
    assert (full.status_code, full.json()) == (503, {"detail": api_push.FULL})
    assert admission.held == 0


def test_a_server_with_notifications_off_says_so_keeps_nothing_and_looks_before_it_says_deleted(
    world: World, tmp_path: Path
) -> None:
    world.push.close()
    fresh = tmp_path / "fresh"
    quiet = open_push({"BEACON_DATA_DIR": str(fresh), PUSH_VARIABLE: "0"}, world.engine, world.runs)
    world.push = quiet
    client = world.app()

    answer = client.get("/api/push").json()
    assert (answer["enabled"], answer["text"]) == (False, OFF)
    assert (answer["public_key"], answer["about_text"], answer["notice"]) == (None, None, None)
    refused = client.post("/api/push/subscribe", json=body_of(), headers=SHOWN)
    assert (refused.status_code, refused.json()) == (503, {"detail": OFF})
    # It has this mode's file open, with nothing in it and no key beside it, so
    # its 204 is the answer of a server that looked.
    assert quiet.subscriptions.all() == ()
    assert (fresh / "push" / "mock.sqlite3").exists()
    assert not (fresh / "push" / "mock.vapid.key").exists()
    gone = client.post("/api/push/unsubscribe", json={"endpoint": FCM}, headers=SHOWN)
    assert gone.status_code == 204

    # A server that has no file open says nothing was deleted, never 204.
    none = world.app(push=False)
    for body in ({"endpoint": FCM}, {"endpoint": FCM, "permits": [NO_PERMIT]}):
        unseen = none.post("/api/push/unsubscribe", json=body, headers=SHOWN)
        assert unseen.status_code == 503
        assert "deleted nothing" in unseen.json()["detail"]


def test_a_server_that_sends_none_and_started_first_deletes_what_another_kept_since(
    world: World, tmp_path: Path
) -> None:
    world.push.close()
    shared = {"BEACON_DATA_DIR": str(tmp_path / "shared")}
    # A server told to send none starts on a data directory with no file.
    quiet = open_push({**shared, PUSH_VARIABLE: "0"}, world.engine, world.runs)
    # Another one, which sends, is started on the same directory after it, and
    # keeps a device's subscription and gives it a permit.
    sender = open_push(
        {**shared, SUBJECT_VARIABLE: CONTACT},
        world.engine,
        world.runs,
        transport=httpx2.MockTransport(world.services),
    )
    sender.subscriptions.put(FCM, [NEAR], at(5))
    permit = sender.subscriptions.permit(at(5))
    assert permit is not None

    # The device's request to delete reaches the first.
    world.push = quiet
    gone = world.app().post(
        "/api/push/unsubscribe", json={"endpoint": FCM, "permits": [permit]}, headers=SHOWN
    )

    # Its 204 is true: the address is gone from the file, and the permit with it.
    assert gone.status_code == 204
    assert sender.subscriptions.all() == ()
    assert sender.subscriptions.put(FCM, [NEAR], at(5.5), permit=permit) == SPENT
    sender.close()


@pytest.mark.parametrize(
    ("env", "why"),
    [({PUSH_VARIABLE: "0", SUBJECT_VARIABLE: CONTACT}, TURNED_OFF), ({}, NO_CONTACT)],
)
def test_a_server_that_sends_none_still_deletes_what_an_earlier_one_kept(
    world: World, env: dict[str, str], why: str
) -> None:
    # A server that sent notifications kept two subscriptions, and is stopped.
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.push.subscriptions.put(MOZ, [NEAR], at(5) + timedelta(days=20))
    world.decide()
    world.push.close()
    key = world.path / "push" / "mock.vapid.key"
    key.unlink()

    # The next one is told to send none, or is given nobody to name to a push service.
    quiet = open_push(
        {"BEACON_DATA_DIR": str(world.path), **env},
        world.engine,
        world.runs,
        transport=httpx2.MockTransport(world.services),
    )
    assert quiet is not None
    assert (quiet.sends, quiet.off, quiet.vapid, quiet.notifier) == (False, why, None, None)
    # It makes no key, takes no subscription, and sends nothing, though one is due.
    world.push = quiet
    client = world.app()
    assert client.get("/api/push").json()["enabled"] is False
    refused = client.post("/api/push/subscribe", json=body_of(endpoint=APPLE), headers=SHOWN)
    assert (refused.status_code, refused.json()) == (503, {"detail": OFF})
    assert world.notify(7) == Pass()
    assert world.services.requests == []
    assert not key.exists()
    assert len(world.kept()) == 2

    # A device that turns notifications off is deleted, and one whose time is
    # over is deleted by the pass, which the server goes on making.
    gone = client.post("/api/push/unsubscribe", json={"endpoint": MOZ}, headers=SHOWN)
    assert gone.status_code == 204
    assert [row[0] for row in world.kept()] == [FCM]
    day = 24 * 60
    assert world.notify(5 + 29 * day) == Pass()
    assert len(world.kept()) == 1
    assert world.notify(5 + 30 * day + 1) == Pass()
    assert world.kept() == []
    quiet.close()


def test_the_other_modes_subscriptions_are_deleted_when_their_time_is_over(
    world: World, tmp_path: Path
) -> None:
    # A live server kept a subscription here before this one, which is the
    # invented scenario's, was started on the same data directory.
    live = Subscriptions(world.path / "push" / "live.sqlite3", "live")
    live.put(FCM, [NEAR], at(5))
    live.put(MOZ, [NEAR], at(5) + timedelta(days=10))
    live.close()
    world.push.close()
    both = open_push(
        {"BEACON_DATA_DIR": str(world.path), SUBJECT_VARIABLE: CONTACT},
        world.engine,
        world.runs,
        transport=httpx2.MockTransport(world.services),
    )
    assert both is not None
    world.push = both
    (other,) = both.others
    assert (other.mode, both.unswept) == ("live", False)
    world.decide()

    day = 24 * 60
    world.notify(5 + 30 * day + 1)

    # Only time empties the other mode's file: nothing in it is sent to, by a
    # server that is not its mode's.
    assert [found.endpoint for found in other.all()] == [MOZ]
    assert world.services.requests == []
    world.notify(5 + 41 * day)
    assert other.all() == ()
    both.close()
    with pytest.raises(SubscriptionsError):
        other.sweep(at(0))


def test_another_modes_file_this_beacon_cannot_open_is_left_and_said(world: World) -> None:
    world.push.close()
    path = world.path / "push" / "live.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
        db.execute("CREATE TABLE newer (x)")
    before = path.read_bytes()

    opened = open_push(
        {"BEACON_DATA_DIR": str(world.path), SUBJECT_VARIABLE: CONTACT},
        world.engine,
        world.runs,
        transport=httpx2.MockTransport(world.services),
    )

    assert opened is not None
    assert (opened.others, opened.unswept) == ((), True)
    assert path.read_bytes() == before
    opened.close()


def with_both_files(world: World) -> Push:
    """The world's push again, on a data directory that also has a live file with
    one subscription whose time is over at day 31, as this mode's file has."""
    live = Subscriptions(world.path / "push" / "live.sqlite3", "live")
    live.put(MOZ, [NEAR], at(5))
    live.close()
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.push.close()
    both = open_push(
        {"BEACON_DATA_DIR": str(world.path), SUBJECT_VARIABLE: CONTACT},
        world.engine,
        world.runs,
        transport=httpx2.MockTransport(world.services),
    )
    assert both is not None
    world.push = both
    return both


def test_what_ran_out_is_deleted_from_both_files_though_the_incidents_cannot_be_read(
    world: World,
) -> None:
    both = with_both_files(world)
    (other,) = both.others

    def broken() -> Snapshot:
        raise sqlite3.OperationalError("the evidence store is busy")

    with pytest.raises(sqlite3.OperationalError):
        both.work(broken, at(5 + 31 * 24 * 60))

    assert (both.subscriptions.all(), other.all()) == ((), ())
    both.close()


@pytest.mark.parametrize("failing", ["this", "other"])
def test_a_file_that_cannot_be_swept_does_not_keep_the_other_from_being_swept(
    world: World, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    both = with_both_files(world)
    (other,) = both.others
    stuck, swept = (both.subscriptions, other) if failing == "this" else (other, both.subscriptions)

    def locked(now: datetime) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(stuck, "sweep", locked)

    assert both.work(world.engine.snapshot, at(5 + 31 * 24 * 60)) == Pass(unswept=1)

    assert swept.all() == ()
    assert len(stuck.all()) == 1
    both.close()


def test_a_subscription_whose_time_is_over_is_sent_nothing_though_it_could_not_be_swept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = 24 * 60
    kept = world.push.subscriptions
    # Two devices near the fire. One's time is over when crisis mode is turned
    # on, and the file cannot be swept just then.
    kept.put(FCM, [NEAR], at(5) - KEPT - timedelta(minutes=1))
    kept.put(MOZ, [NEAR], at(5) - timedelta(days=1))
    world.decide()
    assert near(5 - 30 * day - 1) in kept.all()[0].places

    def locked(now: datetime) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kept, "sweep", locked)

    assert world.notify(7) == Pass(sent=1, unswept=1)

    assert [request.url.host for request in world.services.requests] == [
        "updates.push.services.mozilla.com"
    ]
    assert set(kept.tries()) == {(MOZ, world.id_of("fire"))}
    # At the moment its time is over, to the microsecond, and not before.
    edge = at(7) - KEPT
    kept.put(APPLE, [NEAR], edge)
    assert kept.claim(APPLE, "inc-1", near(7 - 30 * day), at(7))
    assert not kept.claim(APPLE, "inc-2", near(7 - 30 * day), at(7) + timedelta(microseconds=1))


def test_a_server_told_to_send_none_deletes_what_ran_out_whatever_its_contact_is(
    world: World,
) -> None:
    world.push.subscriptions.put(FCM, [NEAR], at(5))
    world.push.close()
    env = {"BEACON_DATA_DIR": str(world.path), PUSH_VARIABLE: "0", SUBJECT_VARIABLE: "nobody"}

    # The contact is one no server could send with, and this one never would.
    with pytest.raises(VapidError):
        open_push({**env, PUSH_VARIABLE: "1"}, world.engine, world.runs)
    quiet = open_push(env, world.engine, world.runs)

    assert quiet is not None
    assert (quiet.sends, quiet.off) == (False, TURNED_OFF)
    world.push = quiet
    assert world.notify(5 + 31 * 24 * 60) == Pass()
    assert world.kept() == []
    quiet.close()


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
    monkeypatch.setenv(SUBJECT_VARIABLE, CONTACT)

    with TestClient(create_app()) as client:
        first = client.get("/api/push").json()
        push: Push = client.app.state.push  # type: ignore[attr-defined]
        assert subscribe(client).status_code == 200
    assert (first["enabled"], first["mode"]) == (True, "mock")
    # Closed with the server: nothing more is kept, and nothing more is sent.
    with pytest.raises(SubscriptionsError):
        push.subscriptions.put(MOZ, [NEAR], at(0))
    assert push.notifier is not None
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
        push: Push = client.app.state.push  # type: ignore[attr-defined]
        assert (push.sends, push.off, push.vapid) == (False, TURNED_OFF, None)
    # The file is there for it to delete from, and no key is made.
    assert (tmp_path / "off" / "push" / "mock.sqlite3").exists()
    assert not (tmp_path / "off" / "push" / "mock.vapid.key").exists()

    with pytest.raises(PushConfigError, match="must be 1 or 0"):
        open_push({PUSH_VARIABLE: "yes"}, world.engine, world.runs)


def test_a_server_given_no_contact_starts_sends_none_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEACON_INGEST", "0")
    caplog.set_level(logging.WARNING)

    with TestClient(create_app()) as client:
        answer = client.get("/api/push").json()
        push: Push = client.app.state.push  # type: ignore[attr-defined]

    assert (answer["enabled"], answer["text"], answer["public_key"]) == (False, OFF, None)
    assert (push.sends, push.off) == (False, NO_CONTACT)
    assert f"This server sends no notifications ({NO_CONTACT})" in caplog.text
    # No key is made for a server that signs nothing.
    assert not (tmp_path / "push" / "mock.vapid.key").exists()

    monkeypatch.setenv(SUBJECT_VARIABLE, "ops@example.org")
    with pytest.raises(VapidError), TestClient(create_app()):
        pass
