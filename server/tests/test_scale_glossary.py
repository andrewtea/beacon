"""The scale run's glossary: learned once from the run's incidents, and compared with
DataSF's unit types, which the learner is never shown."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from beacon.ember import EmberClient, SpendScope, Storage
from beacon.glossary import Pattern
from beacon.investigate import Runs
from beacon.scale import generate, glossary, results, run, score
from tests.scale_day import DataSf
from tests.test_scale_run import PAID, ROWS, Meta, _clip, command, numbers_of, scored, texts_of


@pytest.fixture
def site() -> DataSf:
    return DataSf(ROWS)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"BEACON_DATA_DIR": str(tmp_path / "data")}


@pytest.fixture
def speech(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    monkeypatch.setattr(generate, "can_speak", lambda: True)
    monkeypatch.setattr(generate, "_speech", lambda root, words: _clip(words))


def test_a_days_incidents_are_counted_by_unit_type_code_and_nothing_else_is_kept(
    env: dict[str, str], site: DataSf
) -> None:
    _, reading, _ = scored(env, site)

    patterns, more = glossary.patterns_of(reading.incidents)

    # The fire names E36, T6, and B2; the medical call M552 and E5; the two alarms
    # are one incident with E31 and T12; and the collision is two incidents of E5.
    assert more == 0
    assert patterns == [
        Pattern(
            code="E",
            units=3,
            incidents=5,
            by_category=(("collision", 2), ("alarm", 1), ("fire", 1), ("medical response", 1)),
            alone=2,
            mean_units=1.8,
        ),
        Pattern(
            code="T",
            units=2,
            incidents=2,
            by_category=(("alarm", 1), ("fire", 1)),
            alone=0,
            mean_units=2.5,
        ),
        Pattern(
            code="B", units=1, incidents=1, by_category=(("fire", 1),), alone=0, mean_units=3.0
        ),
        Pattern(
            code="M",
            units=1,
            incidents=1,
            by_category=(("medical response", 1),),
            alone=0,
            mean_units=2.0,
        ),
    ]
    # What Ember would be sent of them names no unit, no street, and no call.
    sent = json.dumps([pattern.as_data() for pattern in patterns])
    for kept_out in ("E36", "M552", "Hayes", "Gough", "260620"):
        assert kept_out not in sent


def test_datasfs_unit_types_are_asked_for_by_two_columns_and_give_a_kind_to_a_code(
    site: DataSf,
) -> None:
    http = site.http()
    types = glossary.fetch_unit_types(http, date(2026, 3, 3))
    http.close()

    (request,) = site.requests
    assert request.url.params["$select"] == "unit_id,unit_type"
    assert request.url.params["$group"] == "unit_id,unit_type"
    # Each unit in its stored form; one whose id Beacon keeps no unit by is left out.
    assert types == {
        "E36": "ENGINE",
        "E5": "ENGINE",
        "E31": "ENGINE",
        "T6": "TRUCK",
        "T12": "TRUCK",
        "B2": "CHIEF",
        "M552": "MEDIC",
    }
    assert glossary.truth_of(types) == {"E": "engine", "T": "truck", "B": "chief", "M": "medic"}
    # Two of DataSF's types can be one kind of unit, and a code whose units are of
    # several kinds, none of them more than half, has no kind to compare with.
    mixed = {"AM1": "BLS", "AM2": "PRIVATE", "AM3": "PRIVATE", "EMS1": "CP", "EMS2": "CHIEF"}
    assert glossary.truth_of(mixed) == {"AM": "ambulance"}


def test_a_stand_in_run_compares_before_and_after_and_says_a_stand_in_proposed_it(
    env: dict[str, str], site: DataSf
) -> None:
    status, said = command(env, site)

    assert status == 0
    numbers, texts = numbers_of(env), texts_of(env)
    assert (numbers["glossary_codes"], numbers["glossary_checked"]) == (4, 4)
    # The four are codes a person wrote down, so Beacon had them before. The
    # stand-in's rule takes the engines for trucks and guesses at nothing named in
    # fewer than five incidents: what a person wrote is never replaced by it.
    assert (numbers["glossary_before"], numbers["glossary_after"]) == (4, 4)
    assert (numbers["glossary_new"], numbers["glossary_wrong"], numbers["glossary_unknown"]) == (
        0,
        1,
        3,
    )
    assert "A stand-in proposed them, by a rule of thumb" in texts["glossary_after"]
    assert "says nothing of Muse Ember" in texts["glossary_after"]
    assert "DataSF's dispatch pattern, not what Beacon heard on a radio" in texts["glossary_codes"]
    assert "The learner is never shown them" in texts["glossary_checked"]
    assert numbers["glossary_cost_usd"] is None
    assert texts["glossary_cost_usd"].startswith("Not measured: a stand-in proposed")
    assert "Codes Beacon has the kind of, after: 4 codes" in said
    layout = run.Layout.under(env, "day")
    kept = json.loads((layout.root / glossary.FILE).read_text())
    assert (kept["state"], kept["model"], kept["roles"]) == ("made", "fake", {"E": "truck"})
    # The glossary is learned from the whole day, so the hearing is closed for it.
    made = results.read(layout)
    assert made is not None
    assert (made["complete"], results.CLOSED in made["caveats"]) == (True, True)


def test_a_paid_run_asks_ember_once_for_its_glossary_under_a_scope_of_its_own(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)

    assert command(env, site, *PAID, "--taps", "0", paid=meta.client(env))[0] == 0

    # One request, with the table of counts and no word of a call.
    (table,) = meta.tables
    assert [row["code"] for row in table["codes"]] == ["E", "T", "B", "M"]
    assert set(table["codes"][0]) == {
        "code", "units", "incidents", "by_category", "alone", "mean_units",
    }  # fmt: skip
    numbers, texts = numbers_of(env), texts_of(env)
    # A thousand tokens in and a hundred out: the one call's own cost.
    assert numbers["glossary_cost_usd"] == pytest.approx(0.00015)
    assert "scale:glossary" in texts["glossary_cost_usd"]
    assert "no cost of hearing radio" in texts["glossary_cost_usd"]
    assert "Muse Ember proposed them" in texts["glossary_after"]
    assert "agreement is not proof that the counts showed it" in texts["glossary_after"]
    ledger = EmberClient.replay_only(Storage.under(Path(env["BEACON_DATA_DIR"])))
    assert ledger.scope_spend(glossary.GLOSSARY_SCOPE).calls == 1
    # What hearing cost is still the hearing scope's own.
    assert numbers["radio_paid_calls"] == meta.paid

    # Given again, the glossary the run has is read, and Ember is not asked.
    assert command(env, site, *PAID, "--taps", "0", paid=meta.client(env))[0] == 0
    assert len(meta.tables) == 1
    assert numbers_of(env) == numbers


def test_a_glossary_ember_failed_on_is_not_asked_for_again_and_the_run_is_still_complete(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    meta.fail_glossary = True

    status, _ = command(env, site, *PAID, "--taps", "0", paid=meta.client(env))

    # The request was sent and may have been paid for: the claim stays, nothing is
    # left for the same command to do, and the result says no glossary was learned.
    assert status == 0
    numbers, texts = numbers_of(env), texts_of(env)
    assert numbers["glossary_after"] is None
    assert texts["glossary_after"].startswith("Not learned: the call was claimed")
    assert "glossary_cost_usd" not in numbers
    kept = json.loads((layout.root / glossary.FILE).read_text())
    assert kept["state"] == "claimed"

    # Given again while the hold the failure put on is running: the run is complete,
    # so it is read as it is, and Ember is asked nothing.
    meta.fail_glossary = False
    status, said = command(env, site, *PAID, "--taps", "0", paid=meta.client(env))
    assert (status, len(meta.tables)) == (0, 1)
    assert "leaving Muse Ember alone" not in said
    assert numbers_of(env)["glossary_after"] is None


def test_a_glossary_that_was_refused_before_anything_was_sent_leaves_the_run_unfinished(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    paid = (*PAID, "--taps", "0")

    # A budget for the glossary that one request's input alone would pass.
    status, said = command(env, site, *paid, "--glossary-budget", "0.00001", paid=meta.client(env))

    assert (status, meta.tables) == (1, [])
    assert not (layout.root / glossary.FILE).exists()
    made = results.read(layout)
    assert made is not None
    assert (made["heard_complete"], made["complete"]) == (True, False)
    assert any(
        caveat.startswith("The glossary is not learned yet: the call was refused before")
        for caveat in made["caveats"]
    )
    assert texts_of(env)["glossary_after"].startswith("Not learned yet: the call was refused")
    assert "The run is not finished, and a hold is on." in said
    kept = layout.results.read_bytes()

    # Given again at once with the whole budget: the refusal put the run's hold on,
    # so nothing is asked and what the run came to is left as it was.
    status, said = command(env, site, *paid, paid=meta.client(env))
    assert (status, meta.tables) == (1, [])
    assert "in the replay of taps or in learning the glossary" in said
    assert layout.results.read_bytes() == kept

    # After the hold, the glossary is asked for, once.
    settings = run.read_settings(layout)
    assert settings is not None
    reading_of = scored_reading(layout, settings)
    runs = Runs(layout.taps, settings.mode)
    later = datetime.now(UTC) + timedelta(minutes=11)
    ember = meta.client(env)(SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000))
    learned = glossary.learn(layout, reading_of, ember, runs, wall_clock=lambda: later)
    runs.close()
    assert (learned.state, learned.model, len(meta.tables)) == ("made", "live", 1)
    assert learned.cost_nanousd == 150_000


def scored_reading(layout: run.Layout, settings: run.Settings) -> Any:
    store = run.open_store(layout, settings)
    try:
        return score.read_incidents(store, run.read_lists(layout)[1]).incidents
    finally:
        store.close()


def test_a_claim_with_no_glossary_is_never_asked_about_again_and_a_hold_is_kept_to(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout, reading, _ = scored(env, site)
    meta = Meta(layout)
    scope = SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000)
    runs = Runs(layout.taps, "mock")
    # The stand-in's run made a glossary: it is gone, as for a run that has none.
    (layout.root / glossary.FILE).unlink()
    now = datetime.now(UTC)
    # A hold is on, as after a failure in the replay of taps.
    runs.save_state({"held_until": (now + timedelta(minutes=5)).isoformat(), "unusable_run": 0})

    held = glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)

    assert (held.state, held.waiting, meta.tables) == ("held", True, [])
    assert not (layout.root / glossary.FILE).exists()
    text = glossary.waiting_text(held, live=True)
    assert text is not None
    assert "leaving Muse Ember alone" in text
    assert "ten minutes" not in text

    # A process that died after its claim left the claim and no glossary.
    (layout.root / glossary.FILE).write_text(
        json.dumps({"format": 1, "state": "claimed", "patterns": [], "more": 0})
    )
    after = now + timedelta(minutes=6)
    claimed = glossary.learn(
        layout, reading.incidents, meta.client(env)(scope), runs, wall_clock=lambda: after
    )
    runs.close()
    assert (claimed.state, claimed.waiting, meta.tables) == ("claimed", False, [])
    assert glossary.waiting_text(claimed, live=True) is None


def test_the_comparison_counts_what_agrees_with_datasf_and_never_replaces_what_a_person_wrote() -> (
    None
):
    def of(code: str) -> Pattern:
        return Pattern(code, units=2, incidents=9, by_category=(), alone=0, mean_units=2.0)

    learned = glossary.Learned(
        state="made",
        patterns=tuple(of(code) for code in ("E", "M", "RC", "AM", "RS", "QRV", "XX")),
        # Right for RC, wrong for AM and for the engine a person wrote down, and
        # nothing for RS; QRV has no one kind in DataSF, and XX is not listed.
        roles={"E": "truck", "RC": "rescue_captain", "AM": "medic", "QRV": "support"},
        left_out=2,
        whole=True,
        model="live",
        cost_nanousd=2_000_000,
    )
    truth = {
        "E": "engine",
        "M": "medic",
        "RC": "rescue_captain",
        "AM": "ambulance",
        "RS": "rescue_squad",
    }

    made = {number.key: number for number in glossary.numbers(learned, truth, generated=False)}

    assert (made["glossary_codes"].value, made["glossary_checked"].value) == (7, 5)
    # Before: E and M, which a person wrote down. After: those and RC. The wrong
    # proposal for E does not replace what the person wrote.
    assert (made["glossary_before"].value, made["glossary_after"].value) == (2, 3)
    assert made["glossary_new"].value == 1
    assert "RC (rescue captain)" in made["glossary_new"].text
    assert (made["glossary_wrong"].value, made["glossary_unknown"].value) == (2, 2)
    assert made["glossary_left_out"].value == 2
    assert made["glossary_cost_usd"].value == pytest.approx(0.002)
    assert "of the incidents Beacon made of recorded radio" in made["glossary_codes"].text
    # A run whose incidents name no unit has no glossary and no numbers of one.
    assert glossary.numbers(glossary.Learned(state="nothing"), truth, generated=True) == []
