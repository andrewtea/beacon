"""Taking a tap: the route, the row, and how little of a person either keeps."""

import base64
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from beacon.api.taps import MAX_BODY_BYTES
from beacon.evidence import EvidenceStore, EvidenceStoreError, Point
from beacon.main import create_app
from beacon.taps import (
    KEPT,
    MAX_TAPS_AT_ONCE,
    MAX_TAPS_PER_MINUTE,
    TapError,
    TapMedia,
    Taps,
    open_taps,
)
from beacon.taps import desk as tap_desk
from beacon.taps import media as tap_media
from tests.test_taps_media import SECRETS, jpeg, wav

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
# Somebody at a window on Hayes Street. The position is invented.
PHONE = {"lat": 37.776_354_9, "lon": -122.424_412_3, "accuracy_m": 11.4}
# What must not be found anywhere after a tap: the position as the phone gave it.
PRECISE = ("37.7763549", "122.4244123", "776354", "424412")


def b64(file: bytes) -> str:
    return base64.b64encode(file).decode()


@pytest.fixture
def now() -> list[datetime]:
    """The time, as the taps are told it, which a test moves."""
    return [NOON]


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence" / "mock.sqlite3", "mock")


@pytest.fixture
def taps(tmp_path: Path, store: EvidenceStore, now: list[datetime]) -> Taps:
    return Taps(store, TapMedia(tmp_path / "taps" / "mock"), clock=lambda: now[0])


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
    assert taps.sweep() == 0
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
    assert not (tmp_path / "taps").exists()


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
    # A tap that is refused for what it is takes no place in the minute.
    assert client.post("/api/taps", json={"location": {}}).status_code == 422
    moment[0] = 1060.0
    assert client.post("/api/taps", json={"location": PHONE}).status_code == 201


def test_only_so_many_taps_are_received_at_once(client: TestClient, taps: Taps) -> None:
    held = [taps.receiving() for _ in range(MAX_TAPS_AT_ONCE)]
    for place in held:
        place.__enter__()

    busy = client.post("/api/taps", json={"location": PHONE})

    assert busy.status_code == 503
    assert busy.json() == {"detail": "Beacon is busy with other taps. Try again in a moment."}
    assert busy.headers["Retry-After"] == "1"
    # A place is given back when its tap is done, however it ended.
    held.pop().__exit__(None, None, None)
    assert client.post("/api/taps", json={"location": {}}).status_code == 422
    assert client.post("/api/taps", content=b"{").status_code == 422
    assert client.post("/api/taps", json={"location": PHONE}).status_code == 201
    for place in held:
        place.__exit__(None, None, None)


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
    # A clip that outstayed its hour while the server was down.
    old = TapMedia(tmp_path / "taps" / "mock")
    old.keep("tap-AAAAAAAAAAAA", "clip", wav(), datetime.now(UTC) - KEPT - timedelta(minutes=1))

    with TestClient(create_app()) as client:
        response = client.post("/api/taps", json={"location": PHONE, "clip": b64(wav())})
        receipt = response.json()
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
    taken = taps.take({"location": PHONE, "photo": b64(jpeg())})

    assert (taken.mode, taps.store.mode) == ("live", "live")
    assert taps.store.path == tmp_path / "evidence" / "live.sqlite3"
    assert [file.name for file in (tmp_path / "taps" / "live").iterdir()] == [
        f"{taken.id}.photo.jpg"
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
