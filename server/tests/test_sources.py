"""The sources route, and the pollers the server runs."""

import threading
import time as system_time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beacon.ingest import Clock, IngestConfigError, build
from beacon.main import create_app

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)


def test_sources_says_the_data_is_mock_and_how_each_pollers_last_poll_went(
    tmp_path: Path,
) -> None:
    app = create_app()
    ingest = build({"BEACON_DATA_DIR": str(tmp_path)}, Clock(lambda: NOON))
    app.state.ingest = ingest
    client = TestClient(app)

    before = client.get("/api/sources")

    assert before.status_code == 200
    assert before.json() == {
        "mode": "mock",
        "sources": [
            {"name": name, "polled_at": None, "ok": None, "rows": 0}
            for name in ("nws", "opensky", "511")
        ],
    }

    ingest.catch_up()

    assert client.get("/api/sources").json() == {
        "mode": "mock",
        "sources": [
            {"name": "nws", "polled_at": "2026-11-27T20:00:00Z", "ok": True, "rows": 2},
            {"name": "opensky", "polled_at": "2026-11-27T20:00:00Z", "ok": True, "rows": 31},
            {"name": "511", "polled_at": "2026-11-27T20:00:00Z", "ok": True, "rows": 3},
        ],
    }


def test_the_server_runs_the_pollers_on_the_mock_scenario_while_it_is_up() -> None:
    with TestClient(create_app()) as client:
        deadline = system_time.monotonic() + 20
        while True:
            answer = client.get("/api/sources").json()
            if all(source["ok"] and source["rows"] for source in answer["sources"]):
                break
            assert system_time.monotonic() < deadline, "The pollers stored nothing in 20 s."
            system_time.sleep(0.05)

        ingest = client.app.state.ingest

    assert answer["mode"] == "mock"
    assert [source["name"] for source in answer["sources"]] == ["nws", "opensky", "511"]
    # When the server has shut down, the pollers have stopped and can ask nothing more.
    assert ingest.http.is_closed
    assert "beacon-ingest" not in [thread.name for thread in threading.enumerate()]


def test_a_server_told_not_to_ingest_runs_no_pollers_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BEACON_INGEST", "0")
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))

    with TestClient(create_app()) as client:
        response = client.get("/api/sources")
        assert client.get("/api/health").status_code == 200

    assert response.status_code == 503
    assert response.json() == {"detail": "The pollers are not running in this server."}
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BEACON_INGEST", "yes", "BEACON_INGEST must be 1 or 0"),
        ("BEACON_LIVE_SOURCES", "nws,cnn", "names cnn, which is not a source"),
        ("BEACON_LIVE_SOURCES", "511", "needs BEACON_511_API_KEY"),
    ],
)
def test_a_server_with_a_setting_it_cannot_use_does_not_start(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(IngestConfigError, match=message), TestClient(create_app()):
        pass
