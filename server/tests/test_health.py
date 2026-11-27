from importlib.metadata import version

from fastapi.testclient import TestClient
from pydantic import BaseModel

from beacon.main import app, create_app


def test_health_reports_ok_and_the_package_version() -> None:
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    # No lifespan ran, so this server reads no evidence and has no mode to name.
    # Nor does it tell a story again: only `beacon.serve` can, and says so here.
    assert response.json() == {
        "status": "ok",
        "version": version("beacon-server"),
        "mode": None,
        "repeats_text": None,
    }


def test_health_says_when_the_server_runs_on_the_invented_scenario() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["mode"] == "mock"


class Tap(BaseModel):
    latitude: float


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
