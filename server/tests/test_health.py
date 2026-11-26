from importlib.metadata import version

from fastapi.testclient import TestClient
from pydantic import BaseModel

from beacon.main import app, create_app


def test_health_reports_ok_and_the_package_version() -> None:
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": version("beacon-server")}


class Tap(BaseModel):
    latitude: float


def test_a_request_that_fails_validation_is_not_echoed_back() -> None:
    app = create_app()

    @app.post("/api/taps")
    def tap(tap: Tap) -> Tap:
        return tap

    response = TestClient(app).post("/api/taps", json={"latitude": "37.7749 Mission St"})

    assert response.status_code == 422
    assert response.json() == {"detail": "The request cannot be used (body: float_parsing)."}
    assert "37.7749" not in response.text
