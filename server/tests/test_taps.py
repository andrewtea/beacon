"""Taking a tap: the route, the row, and how little of a person either keeps."""

import asyncio
import base64
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beacon import main as beacon_main
from beacon import taps as beacon_taps
from beacon.api import taps as api_taps
from beacon.api.taps import MAX_BODY_BYTES
from beacon.evidence import EvidenceStore, EvidenceStoreError, Point
from beacon.ingest import build
from beacon.main import ServerSetupError, create_app
from beacon.radio import ConversionStopped, ConversionUnavailable
from beacon.taps import (
    KEPT,
    MAX_TAPS_AT_ONCE,
    MAX_TAPS_PER_MINUTE,
    MediaLockedError,
    Swept,
    TapError,
    TapMedia,
    Taps,
    open_taps,
)
from beacon.taps import desk as tap_desk
from beacon.taps import media as tap_media
from beacon.taps.media import ORPHANED
from tests.test_taps_media import SECRETS, Same, jpeg, kept, needs_ffmpeg, real_jpeg, wav

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
# Somebody at a window on Hayes Street. The position is invented.
PHONE = {"lat": 37.776_354_9, "lon": -122.424_412_3, "accuracy_m": 11.4}
# What must not be found anywhere after a tap: the position as the phone gave it.
PRECISE = ("37.7763549", "122.4244123", "776354", "424412")


def b64(file: bytes) -> str:
    return base64.b64encode(file).decode()


@pytest.fixture(autouse=True)
def a_host_that_hands_no_core_to_a_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Photos are taken only on such a host, and where these tests run is not
    what they test. A test of a host with a collector says so itself."""
    monkeypatch.setattr(beacon_taps, "core_dump_problem", lambda: None)


@pytest.fixture
def now() -> list[datetime]:
    """The time, as the taps are told it, which a test moves."""
    return [NOON]


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence" / "mock.sqlite3", "mock")


@pytest.fixture
def photos() -> Same:
    """Stands in for the confined ffmpeg: the picture comes back as it went in."""
    return Same()


@pytest.fixture
def taps(tmp_path: Path, store: EvidenceStore, now: list[datetime], photos: Same) -> Taps:
    media = TapMedia(tmp_path / "taps", "mock")
    return Taps(store, media, photos=photos, clock=lambda: now[0])


@pytest.fixture
def client(taps: Taps) -> TestClient:
    app = create_app()
    app.state.taps = taps
    return TestClient(app)


def everything_kept(tmp_path: Path) -> bytes:
    """Every byte under the data directory, the store's side files included."""
    return b"".join(path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file())


def test_a_tap_becomes_a_row_that_says_where_to_a_block_and_when_by_the_servers_clock(
    client: TestClient, store: EvidenceStore
) -> None:
    response = client.post("/api/taps", json={"location": PHONE})

    assert response.status_code == 201
    receipt = response.json()
    assert receipt == {
        "id": receipt["id"],
        "received_at": "2026-11-27T20:00:00Z",
        "mode": "mock",
        "photo": False,
        "clip": False,
        "media_kept_until": None,
    }
    (row,) = store.search()
    assert (row.kind, row.source, row.ref) == ("tap", "app", receipt["id"])
    assert (row.occurred_at, row.confidence) == (NOON, "low")
    # A person's tap is nobody's stand-in, in whichever store it is kept.
    assert (row.origin, row.model, row.stand_in) == ("live", None, False)
    assert row.point == Point(37.776, -122.424)
    assert (row.place, row.url, row.until) == (None, None, None)
    assert row.data == {"accuracy_m": 12, "photo": None, "clip": None, "media_kept_until": None}


def test_a_tap_keeps_its_photo_and_clip_cleaned_and_for_an_hour(
    client: TestClient, taps: Taps, store: EvidenceStore, now: list[datetime], tmp_path: Path
) -> None:
    sent = {"location": PHONE, "photo": b64(jpeg()), "clip": b64(wav(2.0))}

    receipt = client.post("/api/taps", json=sent).json()

    assert (receipt["photo"], receipt["clip"]) == (True, True)
    assert receipt["media_kept_until"] == "2026-11-27T21:00:00Z"
    (row,) = store.search()
    clean = jpeg(extras=False)
    assert row.data == {
        "accuracy_m": 12,
        "photo": {"bytes": len(clean), "width": 640, "height": 480},
        "clip": {"bytes": len(wav(2.0)), "seconds": 2.0},
        "media_kept_until": "2026-11-27T21:00:00+00:00",
    }
    # What the investigator is handed is the cleaned file, and not for longer.
    assert taps.media(receipt["id"], "photo") == clean
    assert taps.media(receipt["id"], "clip") == wav(2.0)
    kept = everything_kept(tmp_path)
    for secret in SECRETS:
        assert secret not in kept
    now[0] = NOON + KEPT
    assert taps.media(receipt["id"], "photo") is None
    assert taps.media(receipt["id"], "clip") is None
    assert taps.sweep() == Swept(deleted=0)
    assert list((tmp_path / "taps" / "mock").iterdir()) == []


def test_asking_for_the_media_of_no_tap_answers_nothing(taps: Taps) -> None:
    assert taps.media("tap-AAAAAAAAAAAA", "photo") is None
    assert taps.media("../../evidence/mock.sqlite3", "photo") is None
    assert taps.media("", "clip") is None


def test_nothing_says_who_tapped_or_exactly_where(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    headers = {
        "User-Agent": "JanesPhone/1.0",
        "X-Forwarded-For": "203.0.113.77",
        "Cookie": "session=jane-doe",
    }

    first = client.post("/api/taps", json={"location": PHONE}, headers=headers)
    second = client.post("/api/taps", json={"location": PHONE}, headers=headers)

    # Two taps from one phone have nothing in common but the block and the minute.
    assert first.json()["id"] != second.json()["id"]
    told = first.text + second.text + caplog.text + everything_kept(tmp_path).decode("latin-1")
    for private in (*PRECISE, "JanesPhone", "203.0.113.77", "jane-doe", "testclient"):
        assert private not in told


def test_the_time_a_phone_gives_is_not_part_of_a_tap(client: TestClient) -> None:
    response = client.post("/api/taps", json={"location": PHONE, "time": "2026-11-27T19:00:00Z"})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "location",
    [
        # Oakland, the ocean, and the other side of the world.
        {"lat": 37.8044, "lon": -122.2712},
        {"lat": 37.7763, "lon": -122.6},
        {"lat": -37.7763, "lon": 122.4244},
    ],
)
def test_a_tap_from_outside_san_francisco_is_refused_and_nothing_of_it_is_kept(
    client: TestClient, store: EvidenceStore, location: dict[str, float]
) -> None:
    response = client.post("/api/taps", json={"location": location, "photo": b64(jpeg())})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Beacon covers San Francisco, and this tap is from somewhere else."
    }
    assert store.search() == []


OAKLAND = {"lat": 37.8044, "lon": -122.2712}

SHAPE = (
    "A tap is a JSON object with a location, which has lat and lon in degrees, "
    "and optionally accuracy_m, a photo, and a clip."
)
MEDIA = "A tap's photo and clip are files in base64, of a size Beacon takes."


@pytest.mark.parametrize(
    ("sent", "detail"),
    [
        ([PHONE], SHAPE),
        ("37.7763549 Hayes St", SHAPE),
        ({}, SHAPE),
        ({"location": None}, SHAPE),
        ({"location": [37.7763549, -122.4244123]}, SHAPE),
        ({"location": {"lat": 37.7763549}}, SHAPE),
        ({"location": {"lat": "37.7763549", "lon": "-122.4244123"}}, SHAPE),
        ({"location": {"lat": True, "lon": -122.4244123}}, SHAPE),
        ({"location": {"lat": 137.7763549, "lon": -122.4244123}}, SHAPE),
        ({"location": PHONE | {"street": "1458 Hayes St"}}, SHAPE),
        ({"location": PHONE, "name": "Jane Doe"}, SHAPE),
        (
            {"location": PHONE | {"accuracy_m": "11.4 m"}},
            "A tap's accuracy_m is a number of metres, when it is given.",
        ),
        (
            {"location": PHONE | {"accuracy_m": -1}},
            "A tap's accuracy_m is a number of metres, when it is given.",
        ),
        ({"location": PHONE, "photo": 7}, MEDIA),
        ({"location": PHONE, "photo": "not base64, Jane Doe!"}, MEDIA),
        ({"location": PHONE, "clip": ["UklGRg=="]}, MEDIA),
        (
            {"location": PHONE, "photo": b64(b"\x89PNG\r\n\x1a\n" + bytes(64))},
            "A tap's photo is a JPEG file of at most four megabytes.",
        ),
        (
            {"location": PHONE, "clip": b64(b"OggS" + bytes(64))},
            "A tap's clip is a WAV file of plain samples, at most fifteen seconds long.",
        ),
    ],
)
def test_what_is_not_a_tap_is_refused_in_a_fixed_sentence_that_repeats_nothing(
    client: TestClient, store: EvidenceStore, tmp_path: Path, sent: Any, detail: str
) -> None:
    response = client.post("/api/taps", json=sent)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert store.search() == []
    assert kept(tmp_path / "taps") == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"lat=37.7763549&lon=-122.4244123",
        b'{"location": {"lat": 37.7763549, "lon": -122.4244123}',
        b'{"location": {"lat": NaN, "lon": -122.4244123}}',
        b'{"location": {"lat": 37.7763549, "lon": 1' + b"0" * 5000 + b"}}",
        b"[" * 100_000,
        b"\xff\xfe\x00Jane Doe",
    ],
)
def test_a_body_that_is_not_json_or_not_a_tap_is_refused_without_being_quoted(
    client: TestClient, body: bytes
) -> None:
    response = client.post("/api/taps", content=body)

    assert response.status_code == 422
    assert response.json()["detail"] in ("A tap is a JSON object.", SHAPE)
    assert "37.7763549" not in response.text
    assert "Jane" not in response.text


def test_a_body_larger_than_a_tap_may_be_is_refused_by_its_declared_length_or_as_it_is_read(
    client: TestClient, store: EvidenceStore
) -> None:
    too_large = {"detail": "A tap is at most ten megabytes, with its photo and its clip."}
    body = b'{"location": "' + b"A" * MAX_BODY_BYTES + b'"}'

    declared = client.post("/api/taps", content=body)

    def pieces() -> Any:
        # No length is declared for a body that is sent in pieces.
        for start in range(0, len(body), 1_000_000):
            yield body[start : start + 1_000_000]

    streamed = client.post("/api/taps", content=pieces())

    assert (declared.status_code, declared.json()) == (413, too_large)
    assert (streamed.status_code, streamed.json()) == (413, too_large)
    assert store.search() == []


def test_a_photo_and_a_clip_of_the_largest_sizes_fit_in_the_largest_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The largest files that could be sent, whether or not they are media.
    sent = {
        "location": PHONE,
        "photo": b64(bytes(tap_media.MAX_PHOTO_BYTES)),
        "clip": b64(bytes(tap_media.MAX_CLIP_BYTES)),
    }
    assert len(json.dumps(sent)) <= MAX_BODY_BYTES

    response = client.post("/api/taps", json=sent)

    # Read whole, and refused for what it is and not for its size.
    assert response.status_code == 422
    assert response.json() == {"detail": "A tap's photo is a JPEG file of at most four megabytes."}


def test_taps_past_the_limit_for_a_minute_are_asked_to_come_back(
    client: TestClient, store: EvidenceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    moment = [1000.0]
    monkeypatch.setattr(tap_desk.time, "monotonic", lambda: moment[0])
    for _ in range(MAX_TAPS_PER_MINUTE):
        assert client.post("/api/taps", json={"location": PHONE}).status_code == 201
    moment[0] = 1020.5

    refused = client.post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})

    assert refused.status_code == 429
    assert refused.json() == {
        "detail": "Beacon is taking as many taps as it can just now. Try again in a minute."
    }
    # The first of the minute's taps is forty seconds from leaving it.
    assert refused.headers["Retry-After"] == "40"
    assert len(store.search()) == MAX_TAPS_PER_MINUTE
    # What is not a tap, or is from elsewhere, takes no place in the minute.
    assert client.post("/api/taps", json={"location": {}}).status_code == 422
    assert client.post("/api/taps", json={"location": OAKLAND}).status_code == 422
    moment[0] = 1060.0
    assert client.post("/api/taps", json={"location": PHONE}).status_code == 201


def test_a_tap_takes_its_place_in_the_minute_before_its_photo_is_decoded(
    client: TestClient, photos: Same, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tap_desk, "MAX_TAPS_PER_MINUTE", 2)
    not_a_photo = {"location": PHONE, "photo": b64(b"GIF89a" + bytes(64))}

    # A tap whose photo is no photo has used its place: looking at it was the work.
    assert client.post("/api/taps", json=not_a_photo).status_code == 422
    assert (
        client.post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())}).status_code == 201
    )
    assert len(photos.given) == 1

    # Past the limit nothing of a tap is decoded, however often it is sent.
    for _ in range(5):
        refused = client.post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})
        assert refused.status_code == 429
    assert len(photos.given) == 1
    # Nor is anything of a tap from elsewhere.
    assert (
        client.post("/api/taps", json={"location": OAKLAND, "photo": b64(jpeg())}).status_code
        == 422
    )
    assert len(photos.given) == 1


def test_only_so_many_taps_are_received_at_once(client: TestClient, taps: Taps) -> None:
    held = [taps.admit() for _ in range(MAX_TAPS_AT_ONCE)]

    busy = client.post("/api/taps", json={"location": PHONE})

    assert busy.status_code == 503
    assert busy.json() == {"detail": "Beacon is busy with other taps. Try again in a moment."}
    assert busy.headers["Retry-After"] == "1"
    # A place is given back when its tap is done, however it ended, and once:
    # giving one back twice does not make room for two.
    give_back = held.pop()
    give_back()
    give_back()
    assert client.post("/api/taps", json={"location": {}}).status_code == 422
    assert client.post("/api/taps", content=b"{").status_code == 422
    assert client.post("/api/taps", content=b"A" * (MAX_BODY_BYTES + 1)).status_code == 413
    assert client.post("/api/taps", json={"location": PHONE}).status_code == 201
    held.append(taps.admit())
    with pytest.raises(TapError):
        taps.admit()
    for give_back in held:
        give_back()


def post_slowly(
    app: Any, receive: Any, headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[int, dict[str, Any]]:
    """What the app answers a tap whose body arrives as `receive` hands it over."""
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/taps",
        "raw_path": b"/api/taps",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), *(headers or [])],
        "client": ("203.0.113.77", 50123),
        "server": ("testserver", 80),
    }

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body = b"".join(message.get("body", b"") for message in sent[1:])
    return sent[0]["status"], json.loads(body)


def test_a_tap_that_does_not_arrive_in_time_gives_its_place_back(
    taps: Taps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_taps, "RECEIVE_SECONDS", 0.05)
    app = create_app()
    app.state.taps = taps
    pieces = [{"type": "http.request", "body": b'{"location": ', "more_body": True}]

    async def stalls() -> dict[str, Any]:
        # The beginning of a tap, and then nothing, for as long as anyone waits.
        if pieces:
            return pieces.pop()
        await asyncio.sleep(3600)
        raise AssertionError("The server waited an hour for a tap.")

    for _ in range(MAX_TAPS_AT_ONCE + 1):
        pieces.append({"type": "http.request", "body": b'{"location": ', "more_body": True})
        status, answer = post_slowly(app, stalls)
        assert status == 408
        assert answer == {"detail": "A tap has to arrive within twenty seconds. Send it again."}

    # Every place was given back: a sender who sends nothing stops nobody.
    held = [taps.admit() for _ in range(MAX_TAPS_AT_ONCE)]
    assert len(held) == MAX_TAPS_AT_ONCE


@pytest.mark.parametrize(
    "failure",
    [
        EvidenceStoreError("The evidence store is closed and takes no more rows."),
        sqlite3.OperationalError("database is locked"),
        OSError(28, "No space left on device"),
    ],
)
def test_a_tap_whose_row_cannot_be_stored_keeps_no_photo_either(
    client: TestClient,
    taps: Taps,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(taps.store, "add", fail)
    sent = {"location": PHONE, "photo": b64(jpeg()), "clip": b64(wav())}

    response = client.post("/api/taps", json=sent)

    assert response.status_code == 503
    assert response.json() == {"detail": "Beacon could not keep this tap. Try again in a moment."}
    assert response.headers["Retry-After"] == "5"
    assert list((tmp_path / "taps" / "mock").iterdir()) == []


def test_a_tap_whose_photo_cannot_be_kept_stores_no_row_either(
    client: TestClient, store: EvidenceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tap_media, "MAX_STORED_BYTES", len(jpeg(extras=False)) + 10)
    sent = {"location": PHONE, "photo": b64(jpeg()), "clip": b64(wav())}

    response = client.post("/api/taps", json=sent)

    # The photo fitted and the clip did not: neither is kept, and there is no row.
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Beacon cannot keep another photo or clip just now. Send the tap without one."
    }
    assert store.search() == []
    assert list((tmp_path / "taps" / "mock").iterdir()) == []
    assert client.post("/api/taps", json={"location": PHONE}).status_code == 201


def test_a_tap_to_a_store_that_is_closed_is_answered_and_not_taken(
    client: TestClient, taps: Taps, store: EvidenceStore
) -> None:
    taps.close()

    response = client.post("/api/taps", json={"location": PHONE})

    assert response.status_code == 503
    assert store.search() == []


def test_a_failure_that_is_not_one_of_keeping_a_tap_is_not_hidden_and_keeps_no_media(
    taps: Taps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("a bug")

    monkeypatch.setattr(taps.store, "add", fail)

    with pytest.raises(KeyError):
        taps.take({"location": PHONE, "photo": b64(jpeg())})

    assert list((tmp_path / "taps" / "mock").iterdir()) == []


def test_a_tap_error_carries_nothing_of_the_tap_under_it(taps: Taps) -> None:
    with pytest.raises(TapError) as refused:
        taps.take({"location": PHONE, "photo": "Jane Doe, 1458 Hayes St"})

    assert refused.value.status == 422
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


def test_taps_taken_at_once_are_all_stored(taps: Taps, store: EvidenceStore) -> None:
    taken: list[str] = []

    def tap() -> None:
        taken.append(taps.take({"location": PHONE, "clip": b64(wav(0.5))}).id)

    threads = [threading.Thread(target=tap) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert len(set(taken)) == 8
    assert sorted(row.ref for row in store.search()) == sorted(taken)
    assert all(taps.media(tap_id, "clip") == wav(0.5) for tap_id in taken)


def test_a_server_takes_taps_into_the_store_its_environment_picks_and_sweeps_old_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    # A clip that outstayed its hour while the server was down, kept when Beacon
    # ran on the other store.
    old = TapMedia(tmp_path / "taps", "live")
    old.keep("tap-AAAAAAAAAAAA", "clip", wav(), datetime.now(UTC) - KEPT - timedelta(minutes=1))
    old.close()

    with TestClient(create_app()) as client:
        response = client.post("/api/taps", json={"location": PHONE, "clip": b64(wav())})
        receipt = response.json()
    # The sweep that began with the server was let finish when the server stopped.
    assert list((tmp_path / "taps" / "live").iterdir()) == []
    files = [file.name for file in (tmp_path / "taps" / "mock").iterdir()]

    assert (response.status_code, receipt["mode"]) == (201, "mock")
    assert files == [f"{receipt['id']}.clip.wav"]
    store = EvidenceStore(tmp_path / "evidence" / "mock.sqlite3", "mock")
    assert [row.ref for row in store.search(kinds=["tap"])] == [receipt["id"]]


def test_a_server_that_has_not_started_takes_no_taps() -> None:
    response = TestClient(create_app()).post("/api/taps", json={"location": PHONE})

    assert response.status_code == 503
    assert response.json() == {"detail": "This server is not taking taps."}


def test_taps_for_real_sources_go_to_the_live_store_and_their_media_beside_it(
    tmp_path: Path,
) -> None:
    env = {"BEACON_DATA_DIR": str(tmp_path), "BEACON_LIVE_SOURCES": "nws"}

    taps = open_taps(env)
    taken = taps.take({"location": PHONE, "clip": b64(wav())})

    assert (taken.mode, taps.store.mode) == ("live", "live")
    assert taps.store.path == tmp_path / "evidence" / "live.sqlite3"
    assert [file.name for file in (tmp_path / "taps" / "live").iterdir()] == [
        f"{taken.id}.clip.wav"
    ]


def test_the_servers_access_log_leaves_out_the_tap_route_and_the_address_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    create_app()
    access = logging.getLogger("uvicorn.access")
    line = '%s - "%s %s HTTP/%s" %d'

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(line, "203.0.113.77:50123", "POST", "/api/taps", "1.1", 201)
        access.info(line, "203.0.113.77:50123", "POST", "/api/taps?x=1", "1.1", 422)
        access.info(line, "203.0.113.77:50124", "GET", "/api/health", "1.1", 200)

    assert [record.getMessage() for record in caplog.records] == [
        '203.0.113.77:50124 - "GET /api/health HTTP/1.1" 200'
    ]


def put(path: Path, content: bytes, when: datetime) -> None:
    """A file as it lies on the disk, written at `when`."""
    path.write_bytes(content)
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_a_file_with_no_row_is_handed_to_nobody_and_is_swept(
    taps: Taps, now: list[datetime], tmp_path: Path
) -> None:
    taken = taps.take({"location": PHONE, "clip": b64(wav())})
    directory = tmp_path / "taps" / "mock"
    # Beacon died after a tap's photo was written and before its row was stored.
    orphan = "tap-AAAAAAAAAAAA"
    put(directory / f"{orphan}.photo.jpg", jpeg(extras=False), NOON)

    assert taps.media(orphan, "photo") is None
    # Nor is a tap handed a kind of media its row does not say it sent.
    put(directory / f"{taken.id}.photo.jpg", jpeg(extras=False), NOON)
    assert taps.media(taken.id, "photo") is None
    assert taps.media(taken.id, "clip") == wav()

    now[0] = NOON + ORPHANED
    assert taps.sweep() == Swept(deleted=1)

    assert kept(directory) == [f"{taken.id}.clip.wav", f"{taken.id}.photo.jpg"]


def test_a_file_is_not_handed_out_after_the_moment_its_tap_was_told(
    taps: Taps, now: list[datetime], tmp_path: Path
) -> None:
    taken = taps.take({"location": PHONE, "clip": b64(wav())})
    # The directory was restored from a copy, and the file is new again.
    later = NOON + KEPT
    put(tmp_path / "taps" / "mock" / f"{taken.id}.clip.wav", wav(), later)

    now[0] = later

    assert taken.media_kept_until == later
    assert taps.media(taken.id, "clip") is None
    assert kept(tmp_path / "taps") == []


def test_media_that_cannot_be_deleted_after_a_failure_does_not_change_the_answer(
    client: TestClient,
    taps: Taps,
    store: EvidenceStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def closed(*_args: Any, **_kwargs: Any) -> Any:
        raise EvidenceStoreError("The evidence store is closed and takes no more rows.")

    def stuck(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(taps.store, "add", closed)
    monkeypatch.setattr(Path, "unlink", stuck)

    response = client.post("/api/taps", json={"location": PHONE, "clip": b64(wav())})
    monkeypatch.undo()

    assert response.status_code == 503
    assert response.json() == {"detail": "Beacon could not keep this tap. Try again in a moment."}
    # The operator is told, in words that name no tap, and the file is nobody's.
    assert "A file of a tap that was not taken could not be deleted." in caplog.text
    assert "tap-" not in caplog.text
    assert ".clip.wav" not in caplog.text
    assert store.search() == []


@pytest.mark.parametrize(
    "location",
    [
        f'{{"lat": 1{"0" * 400}, "lon": -122.4244123}}',
        f'{{"lat": 37.7763549, "lon": -1{"0" * 400}}}',
        f'{{"lat": 37.7763549, "lon": -122.4244123, "accuracy_m": 1{"0" * 400}}}',
        '{"lat": 1e400, "lon": -122.4244123}',
    ],
)
def test_a_number_too_large_to_be_a_place_is_refused_as_any_other_that_is_no_place(
    client: TestClient, location: str
) -> None:
    response = client.post("/api/taps", content=f'{{"location": {location}}}'.encode())

    assert response.status_code == 422
    assert response.json()["detail"].startswith("A tap")


@pytest.mark.parametrize("length", ["9" * 5000, str(MAX_BODY_BYTES + 1), "0" * 30 + "99999999"])
def test_a_declared_length_past_the_limit_is_refused_however_long_the_number(
    taps: Taps, length: str
) -> None:
    app = create_app()
    app.state.taps = taps

    async def nothing() -> dict[str, Any]:
        raise AssertionError("A body that was declared too long was read.")

    status, answer = post_slowly(app, nothing, headers=[(b"content-length", length.encode())])

    assert status == 413
    assert answer == {"detail": "A tap is at most ten megabytes, with its photo and its clip."}


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 5_000_000,
        b"[" + b"{}," * 1_000_000 + b"{}]",
        b'{"location": ' + b'{"a":' * 50 + b"1" + b"}" * 51,
        b'{"location": {"lat": 37.7763549, "lon": -122.4244123}, "photo": [' + b"0," * 99 + b"0]}",
    ],
)
def test_a_body_with_more_structure_than_a_tap_has_is_refused_before_it_is_parsed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    def parsed(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("The body was parsed.")

    # Only where the taps parse a body, and not where the test reads the answer.
    monkeypatch.setattr(tap_desk, "json", SimpleNamespace(loads=parsed))

    response = client.post("/api/taps", content=body)

    assert response.status_code == 422
    assert response.json() == {"detail": SHAPE}


def test_a_server_that_cannot_draw_a_photo_again_takes_taps_without_one(
    store: EvidenceStore, tmp_path: Path
) -> None:
    app = create_app()
    app.state.taps = Taps(store, TapMedia(tmp_path / "taps", "mock"), photos=None)
    client = TestClient(app)

    refused = client.post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})

    # A photo is not kept as it was sent, so it is not kept at all.
    assert refused.status_code == 503
    assert refused.json() == {
        "detail": "This server cannot take a photo just now. Send the tap without one."
    }
    assert store.search() == []
    assert kept(tmp_path / "taps") == []
    assert client.post("/api/taps", json={"location": PHONE, "clip": b64(wav())}).status_code == 201


def test_a_tap_whose_photo_is_being_drawn_when_the_server_stops_is_not_taken(
    store: EvidenceStore, tmp_path: Path
) -> None:
    class Closed:
        def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
            raise ConversionStopped("The converter is closed.")

    app = create_app()
    app.state.taps = Taps(store, TapMedia(tmp_path / "taps", "mock"), photos=Closed())

    response = TestClient(app).post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})

    assert response.status_code == 503
    assert response.json() == {"detail": "This server is not taking taps."}
    assert store.search() == []


@needs_ffmpeg
def test_a_server_keeps_a_real_photo_as_a_picture_it_drew_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path / "data"))
    real = real_jpeg(tmp_path, "3000x2000")
    photo = real[:2] + b"\xff\xe1\x00\x1bExif\x00\x00" + SECRETS[0] + real[2:] + SECRETS[3]

    with TestClient(create_app()) as client:
        response = client.post("/api/taps", json={"location": PHONE, "photo": b64(photo)})
        receipt = response.json()
        kept = client.app.state.taps.media(receipt["id"], "photo")  # type: ignore[attr-defined]
        not_one = client.post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})

    assert response.status_code == 201
    (row,) = EvidenceStore(tmp_path / "data" / "evidence" / "mock.sqlite3", "mock").search()
    assert row.data["photo"] == {"bytes": len(kept), "width": 2048, "height": 1365}
    assert kept[:2] == b"\xff\xd8"
    for secret in SECRETS:
        assert secret not in everything_kept(tmp_path / "data")
    # The layout of a JPEG with no picture in it is not a photo.
    assert not_one.status_code == 422


def test_a_tap_is_left_out_of_fastapis_telemetry_and_other_routes_are_not(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentelemetry import trace

    spans: list[tuple[str, dict[str, Any]]] = []

    class Tracer(trace.NoOpTracer):
        def start_span(self, name: str, *args: Any, **kwargs: Any) -> trace.Span:
            spans.append((name, dict(kwargs.get("attributes") or {})))
            return super().start_span(name, *args, **kwargs)

    class Provider(trace.TracerProvider):
        """A provider somebody set up, as an operator's tracing would be."""

        def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
            return Tracer()

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: Provider())

    assert client.get("/api/health?probe=1").status_code == 200
    traced = len(spans)
    assert client.post("/api/taps?lat=37.7763549", json={"location": PHONE}).status_code == 201
    assert client.post("/api/taps", json={"location": {}}).status_code == 422

    # The provider is in use, and it is told nothing about a tap.
    assert traced > 0
    assert len(spans) == traced
    assert "37.7763549" not in str(spans)


def test_what_uvicorn_logs_at_its_trace_level_names_no_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    create_app()
    asgi = logging.getLogger("uvicorn.asgi")
    server = logging.getLogger("uvicorn.error")

    with caplog.at_level(1, logger="uvicorn"):
        # As uvicorn writes them at its trace level: the address, and the scope,
        # and each connection as it is made and lost.
        asgi.log(5, "%s - ASGI [%d] Started scope=%s", "203.0.113.77:50123", 1, {"path": "/x"})
        asgi.log(5, "%s - ASGI [%d] Receive %s", "203.0.113.77:50123", 1, {"type": "http.request"})
        server.log(5, "%s - HTTP connection made", "203.0.113.77:50123")
        server.log(5, "%s - HTTP connection lost", "203.0.113.77:50123")
        # What it says at the levels an operator runs it at is still said.
        server.info("Application startup complete.")

    assert [record.getMessage() for record in caplog.records] == ["Application startup complete."]


class FakeIngest:
    """Stands in for the pollers in a test of how the server starts and stops."""

    def __init__(self, *, starts: bool = True) -> None:
        self._starts = starts
        self.stopped = 0

    def start(self) -> None:
        if not self._starts:
            raise RuntimeError("The pollers' thread could not be started.")

    def stop(self) -> bool:
        self.stopped += 1
        return True


def test_a_server_whose_pollers_do_not_start_ends_what_it_had_begun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    ingest = FakeIngest(starts=False)
    monkeypatch.setattr(beacon_main, "build", lambda _env: ingest)
    app = create_app()

    with pytest.raises(RuntimeError, match="could not be started"), TestClient(app):
        pass

    assert ingest.stopped == 1
    assert app.state.taps.store.closed
    assert [task for task in threading.enumerate() if task.name.startswith("asyncio_")] == []


def test_a_server_that_fails_to_close_its_taps_still_stops_its_pollers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    ingest = FakeIngest()
    monkeypatch.setattr(beacon_main, "build", lambda _env: ingest)

    def fail(self: Taps) -> None:
        raise OSError("The taps could not be closed.")

    monkeypatch.setattr(Taps, "close", fail)

    with pytest.raises(OSError, match="could not be closed"), TestClient(create_app()):
        pass

    assert ingest.stopped == 1


def test_a_server_whose_taps_cannot_be_opened_stops_the_pollers_it_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    ingest = FakeIngest()
    monkeypatch.setattr(beacon_main, "build", lambda _env: ingest)

    def fail(_env: Any) -> Taps:
        raise EvidenceStoreError("The store was begun as the other one.")

    monkeypatch.setattr(beacon_main, "open_taps", fail)

    with pytest.raises(EvidenceStoreError), TestClient(create_app()):
        pass

    assert ingest.stopped == 1


def test_a_host_that_could_not_run_the_converter_is_not_told_its_photo_is_bad(
    store: EvidenceStore, tmp_path: Path
) -> None:
    class Unavailable:
        def to_jpeg(self, photo: bytes, *, max_side: int, max_bytes: int) -> bytes | None:
            raise ConversionUnavailable("The converter could not be run on this host just now.")

    app = create_app()
    app.state.taps = Taps(store, TapMedia(tmp_path / "taps", "mock"), photos=Unavailable())

    response = TestClient(app).post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "This server cannot take a photo just now. Send the tap without one."
    }
    assert response.headers["Retry-After"] == "30"
    assert store.search() == []


def test_a_server_on_a_host_that_collects_core_dumps_takes_no_photos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    # ffmpeg holds the photo while it draws it, and nothing keeps a collector on
    # the host from being handed ffmpeg's memory when it crashes.
    monkeypatch.setattr(beacon_taps, "core_dump_problem", lambda: "This host hands core dumps on.")

    with TestClient(create_app()) as client:
        photo = client.post("/api/taps", json={"location": PHONE, "photo": b64(jpeg())})
        without = client.post("/api/taps", json={"location": PHONE, "clip": b64(wav())})

    assert (photo.status_code, without.status_code) == (503, 201)
    assert "Taps are taken without photos." in caplog.text


def test_a_server_that_could_leave_a_core_with_a_collector_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    # It could not be marked, and its core size limit is nothing to a collector.
    # The HTTP server holds a request before any route can refuse it, so the
    # server is not there to be sent one.
    monkeypatch.setattr(beacon_main, "forbid_core_dumps", lambda: False)
    monkeypatch.setattr(beacon_main, "core_dump_problem", lambda: "It hands cores on.")

    with (
        pytest.raises(ServerSetupError, match=r"python -m beacon\.taps") as failure,
        TestClient(create_app()),
    ):
        pass

    assert "kernel.core_pattern" in str(failure.value)
    assert list(tmp_path.iterdir()) == []


def test_a_server_that_cannot_be_marked_runs_where_no_collector_is_handed_a_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(beacon_main, "forbid_core_dumps", lambda: False)
    monkeypatch.setattr(beacon_main, "core_dump_problem", lambda: None)

    with TestClient(create_app()) as client:
        response = client.post("/api/taps", json={"location": PHONE})

    # Its core size limit of nothing is enough where a core would be a file.
    assert response.status_code == 201
    assert "could not mark itself" in caplog.text


@pytest.mark.skipif(sys.platform != "linux", reason="The mark is Linux's.")
def test_a_server_that_takes_taps_is_a_process_the_kernel_makes_no_core_of(
    tmp_path: Path,
) -> None:
    # In a process of its own, because the mark stays on the process that sets it.
    script = (
        "import ctypes, os, resource\n"
        "from fastapi.testclient import TestClient\n"
        "from beacon.main import create_app\n"
        "libc = ctypes.CDLL(None)\n"
        "before = libc.prctl(3, 0, 0, 0, 0)\n"
        "with TestClient(create_app()) as client:\n"
        "    place = {'location': {'lat': 37.7763, 'lon': -122.4244}}\n"
        "    status = client.post('/api/taps', json=place).status_code\n"
        "    core = resource.getrlimit(resource.RLIMIT_CORE)[0]\n"
        "    print(before, libc.prctl(3, 0, 0, 0, 0), core, status)\n"
    )
    env = os.environ | {"BEACON_INGEST": "0", "BEACON_DATA_DIR": str(tmp_path)}

    ran = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)

    # Dumpable before, not dumpable while it takes taps, with no core file either.
    assert ran.stdout.split() == ["1", "0", "0", "201"], ran.stderr


def test_a_tap_is_left_out_of_telemetry_wherever_the_app_is_served_from(
    taps: Taps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentelemetry import trace

    spans: list[tuple[str, dict[str, Any]]] = []

    class Tracer(trace.NoOpTracer):
        def start_span(self, name: str, *args: Any, **kwargs: Any) -> trace.Span:
            spans.append((name, dict(kwargs.get("attributes") or {})))
            return super().start_span(name, *args, **kwargs)

    class Provider(trace.TracerProvider):
        def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
            return Tracer()

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: Provider())
    app = create_app()
    app.state.taps = taps
    # Behind a proxy that serves the app under /beacon, as uvicorn's --root-path does.
    client = TestClient(app, root_path="/beacon")

    assert client.get("/beacon/api/health").status_code == 200
    traced = len(spans)
    tapped = client.post("/beacon/api/taps?lat=37.7763549", json={"location": PHONE})

    assert tapped.status_code == 201
    assert traced > 0
    assert len(spans) == traced
    assert "37.7763549" not in str(spans)


def test_pollers_whose_thread_could_not_be_started_can_still_be_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ingest = build({"BEACON_DATA_DIR": str(tmp_path)})

    def fail(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patched:
        patched.setattr(threading.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            ingest.start()

    # Stopping joins no thread that never ran, and closes what was open.
    assert ingest.stop() is True
    assert ingest.store.closed


def test_a_second_server_in_the_same_data_directory_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))

    with TestClient(create_app()) as first:
        # The megabytes kept and the sweep are one process's to count.
        with (
            pytest.raises(MediaLockedError, match="Another Beacon process is taking taps"),
            TestClient(create_app()),
        ):
            pass
        assert first.post("/api/taps", json={"location": PHONE}).status_code == 201

    # The first has stopped and given the directory up.
    with TestClient(create_app()) as second:
        assert second.post("/api/taps", json={"location": PHONE}).status_code == 201
