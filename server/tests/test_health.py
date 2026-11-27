from importlib.metadata import version

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from beacon.main import FRONT_UNVERIFIED, ServerSetupError, app, create_app


def test_health_reports_ok_and_the_package_version() -> None:
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    # No lifespan ran, so this server reads no evidence and has no mode to name.
    # Nor does it tell a story again: only `beacon.serve` can, and says so here.
    # And nothing says its front end is unchecked.
    assert response.json() == {
        "status": "ok",
        "version": version("beacon-server"),
        "mode": None,
        "repeats_text": None,
        "front_text": None,
    }


def test_health_says_when_the_server_runs_on_the_invented_scenario() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["mode"] == "mock"


class Tap(BaseModel):
    latitude: float


def test_a_server_behind_a_front_end_nobody_checked_says_so_to_every_visitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the incidents are invented. What a visitor sends is theirs, and a host
    # in front of the server can write down the address a tap's answer is read at.
    assert TestClient(create_app()).get("/api/health").json()["front_text"] is None
    monkeypatch.setenv("BEACON_FRONT_END", "verified")
    assert TestClient(create_app()).get("/api/health").json()["front_text"] is None

    monkeypatch.setenv("BEACON_FRONT_END", "unverified")
    said = TestClient(create_app()).get("/api/health").json()["front_text"]

    assert said == FRONT_UNVERIFIED
    assert "What you send here is real" in said
    assert "nobody has checked what it writes down" in said
    assert "holds the tap's id" in said
    # It does not say what the host keeps, which nobody knows.
    assert "may keep" in said

    monkeypatch.setenv("BEACON_FRONT_END", "probably fine")
    with pytest.raises(ServerSetupError, match="is verified or unverified"):
        create_app()


def test_a_request_that_fails_validation_is_not_echoed_back() -> None:
    app = create_app()

    # A route of the test's own, read by FastAPI as the real tap route is not.
    @app.post("/api/places")
    def tap(tap: Tap) -> Tap:
        return tap

    response = TestClient(app).post("/api/places", json={"latitude": "37.7749 Mission St"})

    assert response.status_code == 422
    assert response.json() == {"detail": "The request cannot be used (body: float_parsing)."}
    assert "37.7749" not in response.text
