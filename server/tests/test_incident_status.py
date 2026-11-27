import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from beacon.api.incidents import SourceOut, radio_of, status_text_of
from beacon.incident import open_engine
from beacon.main import create_app
from tests.incident_rows import Rows, at, mock_store


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
    # What a card shows of an incident rests on its radio calls, and the list
    # says so as the incident's own route does: each call's source, and the least
    # confidence among them.
    assert fire["radio"] == one["radio"]
    assert {source["kind"] for source in fire["radio"]["sources"]} == {"radio"}
    assert {source["stand_in"] for source in fire["radio"]["sources"]} == {True}
    # A stand-in "heard" the scenario's calls and "read" its notices. No label
    # says Ember did.
    labels = {
        (source["source"], source["model"], source["label"])
        for line in one["timeline"]
        for source in line["sources"]
        if source["model"] is not None
    }
    assert labels <= {
        ("recordings", "fake", "Fire and EMS dispatch radio"),
        ("notices", "fake", "Official notice"),
    }
    assert ("recordings", "fake", "Fire and EMS dispatch radio") in labels
    assert "Ember" not in str(one["official"]) + str(one["story"]) + str(fire["radio"])
    calls = [line for line in one["timeline"] if line["kind"] == "radio"]
    assert [source["evidence_id"] for source in fire["radio"]["sources"]] == [
        line["sources"][0]["evidence_id"] for line in calls
    ]
    assert {source["kind"] for source in medical["radio"]["sources"]} == {"radio"}
    # A medical response is its crews and its block: no status is worded for it.
    assert medical["status_text"] is None


def test_a_cancellation_that_names_no_unit_is_not_said_to_have_cleared_any(tmp_path: Path) -> None:
    rows = Rows(mock_store(tmp_path))
    rows.radio(0, "fire", "working", units=("E36", "T6"), street="Hayes St", block=400)
    # A call that cancels and names the street and no unit, while units are recent.
    rows.radio(10, "fire", "cancelled", street="Hayes St", block=400)

    (fire,) = open_engine({}, rows.store, clock=lambda: at(12)).incidents()

    assert fire.status == "clearing"
    text = status_text_of(fire)
    assert text == "A clearance or a cancellation was reported on the radio."
    assert "units" not in text


def test_what_a_card_rests_on_is_as_weak_as_its_weakest_call(tmp_path: Path) -> None:
    rows = Rows(mock_store(tmp_path))
    first = rows.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    second = rows.radio(2, "fire", "working", units=("E36",), clarity="partial")
    # A tap is no radio call: the card's facts do not rest on it.
    rows.tap(3)

    (fire,) = open_engine({}, rows.store, clock=lambda: at(5)).incidents()
    radio = radio_of(fire)

    assert radio is not None
    assert radio.confidence == "low"
    assert [source.evidence_id for source in radio.sources] == [first.id, second.id]


def test_a_source_a_stand_in_wrote_is_not_labeled_as_embers_whatever_label_was_kept() -> None:
    # A story that was kept holds the labels its sources had when it was written.
    kept = {
        "kind": "radio",
        "source": "recordings",
        "label": "Fire and EMS dispatch radio, as Ember heard it",
        "confidence": "medium",
        "at": at(0),
        "evidence_id": 1,
        "url": None,
        "stand_in": True,
        "credit": None,
    }

    by_a_stand_in = SourceOut.model_validate({**kept, "model": "fake"})
    by_ember = SourceOut.model_validate({**kept, "model": "live", "stand_in": False})

    assert by_a_stand_in.label == "Fire and EMS dispatch radio"
    # What Ember did hear is still said to be as Ember heard it.
    assert by_ember.label == "Fire and EMS dispatch radio, as Ember heard it"
