"""`GET /api/scale`: a scale run's results as the app shows them, each number with
its sentence."""

import json
import os
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beacon.api import scale
from beacon.main import app, create_app
from beacon.scale import results, run
from beacon.scale.cli import main
from tests.scale_day import DAY, DataSf
from tests.test_scale_run import ROWS


def make_run(name: str = "day") -> run.Layout:
    """A stand-in run of the invented day, under the test's data directory."""
    env = {"BEACON_DATA_DIR": os.environ["BEACON_DATA_DIR"]}
    http = DataSf(ROWS).http()
    try:
        assert main(["--name", name, "--day", DAY], env, StringIO(), http, None) == 0
    finally:
        http.close()
    return run.Layout.under(env, name)


def test_a_server_that_names_no_run_shows_none_and_says_what_the_page_is() -> None:
    # No lifespan ran, and one that ran with no setting says the same.
    assert TestClient(app).get("/api/scale").json() == {
        "about_text": scale.ABOUT,
        "run": None,
        "none_text": scale.NO_RUN,
    }
    with TestClient(create_app()) as client:
        assert client.get("/api/scale").json()["none_text"] == scale.NO_RUN
    assert "apart from this server" in scale.ABOUT


def test_a_run_is_shown_as_what_it_is_then_every_number_with_its_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_run()
    monkeypatch.setenv("BEACON_SCALE_RUN", "day")

    with TestClient(create_app()) as client:
        response = client.get("/api/scale")

    assert response.status_code == 200
    shown = response.json()["run"]
    made = results.read(layout)
    assert made is not None
    assert response.json()["none_text"] is None
    # What the run is, in the run's own words, and that a stand-in had a part in it.
    assert (shown["name"], shown["day"], shown["stand_in"]) == ("day", DAY, True)
    assert shown["basis_text"] == results.GENERATED_BY_STAND_IN
    # A label for the run, and the sentence that goes with every group of its
    # numbers: neither lets a stand-in's number be read as Beacon's on real radio.
    assert shown["kind_text"] == "Stand-in, generated calls"
    assert (
        "no measurement of Beacon's accuracy, speed, or cost on real radio"
        in (shown["reminder_text"])
    )
    assert (shown["complete"], shown["state_text"]) == (True, scale.COMPLETE)
    assert shown["caveats"] == made["caveats"]
    assert shown["scored_against"] == made["scored_against"]
    # The headings in their order, and under them every number the run wrote, in
    # the order it wrote them: none is left out.
    titles = [section["title"] for section in shown["sections"]]
    assert titles == [title for title in results.SECTIONS if title in titles]
    assert titles[0] == results.RECORD
    assert {results.MATCHED, results.TAPS, results.GLOSSARY} <= set(titles)
    numbers = [number for section in shown["sections"] for number in section["numbers"]]
    assert [number["key"] for number in numbers] == [number["key"] for number in made["numbers"]]
    by_key = {number["key"]: number for number in numbers}
    # A value is shown with its unit, and a number the run does not have as "none"
    # with the sentence that says why.
    assert by_key["found"]["value_text"] == "4 calls"
    assert by_key["merged"]["value_text"] == "1 call"
    assert by_key["found_share"]["value_text"] == "66.7%"
    assert by_key["radio_cost_usd"]["value_text"] == "none"
    assert by_key["radio_cost_usd"]["text"].startswith("Not measured")
    # No number is shown bare, and none carries a raw value to show without its unit.
    assert all(number["text"] and number["label"] for number in numbers)
    assert all(set(number) == {"key", "label", "value_text", "text"} for number in numbers)


def test_a_run_that_is_not_finished_or_has_no_results_yet_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEACON_SCALE_RUN", "later")
    with TestClient(create_app()) as client:
        assert client.get("/api/scale").json() == {
            "about_text": scale.ABOUT,
            "run": None,
            "none_text": scale.NO_RESULTS,
        }
    layout = make_run("later")
    made = results.read(layout)
    assert made is not None
    results.write(layout, made | {"complete": False})

    with TestClient(create_app()) as client:
        shown = client.get("/api/scale").json()["run"]

    assert (shown["complete"], shown["state_text"]) == (False, scale.UNFINISHED)


@pytest.mark.parametrize(
    "written",
    ["not json", '{"format": 0}', '{"format": 1, "name": "day", "secret": "Jane Doe"}'],
)
def test_results_that_cannot_be_read_are_refused_in_a_fixed_sentence(
    written: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_run()
    Path(layout.results).write_text(written.replace('"format": 1', f'"format": {results.FORMAT}'))
    monkeypatch.setenv("BEACON_SCALE_RUN", "day")

    with TestClient(create_app()) as client:
        response = client.get("/api/scale")

    assert response.status_code == 503
    assert response.json() == {"detail": scale.UNREADABLE}


def test_a_setting_that_is_no_runs_name_stops_the_server_from_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEACON_SCALE_RUN", "../evidence")

    with pytest.raises(run.ScaleError, match="A run is named with"), TestClient(create_app()):
        pass


@pytest.mark.parametrize(
    ("audio", "hearer", "label", "stand_in", "basis"),
    [
        ("generated", "fake", "Stand-in, generated calls", True, results.GENERATED_BY_STAND_IN),
        ("generated", "live", "Muse Ember, generated speech", True, results.GENERATED_BY_EMBER),
        ("recorded", "live", "Muse Ember, recorded radio", False, results.RECORDED_BY_EMBER),
    ],
)
def test_only_recorded_radio_heard_by_ember_is_shown_with_no_reminder(
    audio: str,
    hearer: str,
    label: str,
    stand_in: bool,
    basis: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_run()
    made = results.read(layout)
    assert made is not None
    # A result as a run of that kind writes it: what it is, and the words that say so.
    of_kind = {"audio": audio, "hearer": hearer, "stand_in": stand_in, "basis_text": basis}
    results.write(layout, made | of_kind)
    monkeypatch.setenv("BEACON_SCALE_RUN", "day")

    with TestClient(create_app()) as client:
        shown = client.get("/api/scale").json()["run"]

    assert (shown["kind_text"], shown["stand_in"]) == (label, stand_in)
    assert (shown["reminder_text"] is None) == (not stand_in)
    assert shown["basis_text"] == basis
    if stand_in:
        assert "no measurement of Beacon" in shown["reminder_text"]
        assert "on real radio" in shown["reminder_text"]
    # A paid run can have stopped before its first request: the sentence says what
    # the run is set up to do, and not that Ember heard anything.
    assert "Numbers of Muse Ember hearing" not in (shown["reminder_text"] or "")


@pytest.mark.parametrize(
    "changed",
    [
        # A stand-in's run that does not say a stand-in had a part in it.
        {"stand_in": False},
        {"stand_in": "no"},
        # A run of no kind Beacon makes: recordings are never heard by a stand-in.
        {"audio": "recorded", "hearer": "fake"},
        {"hearer": "someone"},
        # A result that calls itself recorded radio heard by Ember while its own
        # words say no radio was heard and Ember was asked nothing, and the other
        # way about: it is not shown under either kind's label.
        {"audio": "recorded", "hearer": "live", "stand_in": False},
        {"basis_text": results.RECORDED_BY_EMBER},
        {"basis_text": results.GENERATED_BY_EMBER},
        {"basis_text": ""},
        # Whether the run is complete, as a word: the sentence about it is chosen by it.
        {"complete": "false"},
        {"complete": 0},
        {"caveats": ["  "]},
    ],
)
def test_results_that_do_not_say_what_a_stand_in_did_are_not_shown_at_all(
    changed: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_run()
    made = results.read(layout)
    assert made is not None
    results.write(layout, made | changed)
    monkeypatch.setenv("BEACON_SCALE_RUN", "day")

    with TestClient(create_app()) as client:
        response = client.get("/api/scale")

    assert (response.status_code, response.json()) == (503, {"detail": scale.UNREADABLE})


def test_a_count_of_one_is_one_thing_and_a_large_count_is_grouped() -> None:
    assert results.shown({"value": 1, "unit": "calls"}) == "1 call"
    assert results.shown({"value": 1, "unit": "entries"}) == "1 entry"
    assert results.shown({"value": 1525, "unit": "calls"}) == "1,525 calls"
    assert results.shown({"value": 0, "unit": "taps"}) == "0 taps"
    assert results.shown({"value": 1.0, "unit": "m"}) == "1.0 m"
    assert results.shown({"value": None, "unit": "USD"}) == "none"


@pytest.mark.parametrize(
    "changed",
    [
        {"value": "many"},
        {"value": True},
        {"value": float("inf")},
        {"value": float("nan")},
        # Whole numbers no float holds, for a count, a share, and an amount.
        {"value": 10**400},
        {"value": -(10**400)},
        {"unit": "percent", "value": 10**400},
        {"unit": "USD", "value": 10**400},
        {"unit": "percent", "value": "not numeric"},
        {"text": "   "},
        {"text": None},
        {"label": ""},
        {"unit": ""},
        {"section": 3},
    ],
)
def test_a_number_that_is_no_number_or_has_no_sentence_is_never_shown(
    changed: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_run()
    made = results.read(layout)
    assert made is not None
    made["numbers"][4] = made["numbers"][4] | changed
    # Written as the file would hold it, an infinity included.
    Path(layout.results).write_text(json.dumps(made))
    monkeypatch.setenv("BEACON_SCALE_RUN", "day")

    with TestClient(create_app()) as client:
        response = client.get("/api/scale")

    # The fixed sentence, and nothing of the file: no bare count, no value with a
    # unit it is not of.
    assert (response.status_code, response.json()) == (503, {"detail": scale.UNREADABLE})


def test_a_run_of_a_sample_says_so_beside_every_number_or_is_not_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"BEACON_DATA_DIR": os.environ["BEACON_DATA_DIR"]}
    http = DataSf(ROWS).http()
    try:
        assert main(["--name", "part", "--day", DAY, "--calls", "3"], env, StringIO(), http) == 0
    finally:
        http.close()
    layout = run.Layout.under(env, "part")
    monkeypatch.setenv("BEACON_SCALE_RUN", "part")

    with TestClient(create_app()) as client:
        shown = client.get("/api/scale").json()["run"]

        # The label that is beside every value, the sentence under every heading,
        # and the words before any number all say what the run is of.
        assert shown["kind_text"] == "Stand-in, generated calls, sample of 3 calls"
        assert "the first 3 of the day's 6 calls" in shown["reminder_text"]
        assert "This run is of a sample: the first 3 calls of the day" in shown["basis_text"]

        # A result that says it is of a sample its own words do not name is not
        # relabeled: it is not shown.
        made = json.loads(layout.results.read_text())
        made["sample"] = None
        layout.results.write_text(json.dumps(made))
        refused = client.get("/api/scale")
        assert refused.status_code == 503
        assert refused.json()["detail"] == scale.UNREADABLE
