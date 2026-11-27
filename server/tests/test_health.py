from importlib.metadata import version

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from beacon import front
from beacon.main import ServerSetupError, app, create_app


def test_health_reports_ok_and_the_package_version() -> None:
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    # No lifespan ran, so this server reads no evidence and has no mode to name.
    # Nor does it tell a story again: only `beacon.serve` can, and says so here.
    # And nothing says its front end is unchecked: it takes a visitor's own.
    assert response.json() == {
        "status": "ok",
        "version": version("beacon-server"),
        "mode": None,
        "teller": None,
        "teller_text": None,
        "media_text": None,
        "repeats_text": None,
        "input": "own",
        "front_text": None,
        "front_more_text": None,
    }


def test_health_says_when_the_server_runs_on_the_invented_scenario() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["mode"] == "mock"


class Tap(BaseModel):
    latitude: float


def test_a_server_behind_a_front_end_nobody_checked_says_what_it_takes_and_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = TestClient(create_app()).get("/api/health").json()
    assert (plain["input"], plain["front_text"], plain["front_more_text"]) == ("own", None, None)
    monkeypatch.setenv("BEACON_FRONT_END", "verified")
    checked = TestClient(create_app()).get("/api/health").json()
    assert (checked["input"], checked["front_text"]) == ("own", None)

    monkeypatch.setenv("BEACON_FRONT_END", "unverified")
    health = TestClient(create_app()).get("/api/health").json()

    assert health["input"] == "invented"
    assert (health["front_text"], health["front_more_text"]) == (
        front.INVENTED,
        front.INVENTED_MORE,
    )

    # Neither word, the empty one included, stops the server: a deployment's
    # default is not emptied into a server that takes everything and says nothing.
    for wrong in ("probably fine", ""):
        monkeypatch.setenv("BEACON_FRONT_END", wrong)
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
