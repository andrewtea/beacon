import time
from typing import Any

from fastapi.testclient import TestClient

from beacon.main import create_app


def _of(listed: list[dict[str, Any]], category: str) -> dict[str, Any] | None:
    return next((incident for incident in listed if incident["category"] == category), None)


def test_an_incidents_status_is_said_as_what_the_radio_said_and_a_medical_one_has_none() -> None:
    # The server's own start: the mock scenario, twenty minutes into its story,
    # which the pollers store on a thread of their own.
    with TestClient(create_app()) as client:
        fire = medical = None
        for _ in range(200):
            listed = client.get("/api/incidents").json()["incidents"]
            fire, medical = _of(listed, "fire"), _of(listed, "medical response")
            if fire is not None and medical is not None and fire["status"] == "working":
                break
            time.sleep(0.05)
        assert fire is not None
        assert medical is not None
        one = client.get(f"/api/incidents/{fire['id']}").json()["incident"]

    assert fire["status"] == "working"
    assert fire["status_text"] == "The radio said: crews at work."
    assert one["status_text"] == fire["status_text"]
    # A medical response is its crews and its block: no status is worded for it.
    assert medical["status_text"] is None
