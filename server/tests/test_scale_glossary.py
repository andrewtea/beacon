"""The scale run's glossary: learned once from the run's incidents, and compared with
DataSF's unit types, which the learner is never shown."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from beacon.ember import EmberClient, SpendScope, Storage
from beacon.glossary import Pattern
from beacon.ingest.paid import read_hold
from beacon.investigate import Runs, RunsError
from beacon.scale import generate, glossary, results, run, score, taps
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
        "B2": ["CHIEF"],
        "E31": ["ENGINE"],
        "E36": ["ENGINE"],
        "E5": ["ENGINE"],
        "M552": ["MEDIC"],
        "T12": ["TRUCK"],
        "T6": ["TRUCK"],
    }
    assert glossary.truth_of(types) == {"E": "engine", "T": "truck", "B": "chief", "M": "medic"}
    # Two of DataSF's types can be one kind of unit, and a code whose units are of
    # several kinds, none of them more than half, has no kind to compare with.
    mixed = {
        "AM1": ["BLS"],
        "AM2": ["PRIVATE"],
        "AM3": ["BLS", "PRIVATE"],
        "EMS1": ["CP"],
        "EMS2": ["CHIEF"],
    }
    assert glossary.truth_of(mixed) == {"AM": "ambulance"}


def test_every_unit_of_a_code_counts_and_one_with_no_one_kind_counts_for_none(
    site: DataSf,
) -> None:
    def rows(*given: tuple[str, str]) -> dict[str, list[str]]:
        site.type_rows = [{"unit_id": unit, "unit_type": kind} for unit, kind in given]
        http = site.http()
        try:
            return glossary.fetch_unit_types(http, date(2026, 3, 3))
        finally:
            http.close()

    # One engine and two units of a type Beacon has no kind for: the engine is one
    # of three, which is no majority. The type DataSF wrote is not kept.
    types = rows(("E1", "ENGINE"), ("E2", "HOVERCRAFT"), ("E3", "Some new type"))
    assert types == {"E1": ["ENGINE"], "E2": ["UNLISTED"], "E3": ["UNLISTED"]}
    assert glossary.truth_of(types) == {}
    # A unit whose rows give two kinds counts for neither, in whichever order the
    # rows come: with one truck beside it, the code has no majority either way.
    for order in ((("T1", "ENGINE"), ("T1", "TRUCK")), (("T1", "TRUCK"), ("T1", "ENGINE"))):
        types = rows(*order, ("T2", "TRUCK"))
        assert types == {"T1": ["ENGINE", "TRUCK"], "T2": ["TRUCK"]}
        assert glossary.truth_of(types) == {}
    # With a second truck, the trucks are two of three.
    types = rows(("T1", "ENGINE"), ("T1", "TRUCK"), ("T2", "TRUCK"), ("T3", "TRUCK"))
    assert glossary.truth_of(types) == {"T": "truck"}


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
        0,
        3,
    )
    # Its proposal for the engines is compared, and never applied.
    assert numbers["glossary_overruled"] == 1
    assert "never applied: what the person wrote stands" in texts["glossary_overruled"]
    for key in ("glossary_after", "glossary_new", "glossary_wrong", "glossary_overruled"):
        assert "A stand-in proposed them, by a rule of thumb" in texts[key]
        assert "says nothing of Muse Ember" in texts[key]
    # The table is of Beacon's incidents of generated calls, which follow DataSF's
    # record and are not DataSF's record itself.
    assert "the incidents Beacon made of generated calls" in texts["glossary_codes"]
    assert "are of nothing heard on a radio" in texts["glossary_codes"]
    assert "The learner is never shown them" in texts["glossary_checked"]
    assert "more than half of all the units" in texts["glossary_checked"]
    assert numbers["glossary_cost_usd"] is None
    assert texts["glossary_cost_usd"].startswith("Not measured: a stand-in was asked")
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

    # Given again while the hold the failure put on is running: the run is complete
    # and its plan of taps the same, so its result is shown as it was written, not
    # made again, and Ember is asked nothing.
    meta.fail_glossary = False
    written = layout.results.read_bytes()
    status, said = command(env, site, *PAID, "--taps", "0", paid=meta.client(env))
    assert (status, len(meta.tables)) == (0, 1)
    assert "leaving Muse Ember alone" not in said
    assert "Codes Beacon has the kind of, after: none" in said
    assert layout.results.read_bytes() == written

    # With taps it was not made with, there is something to ask, and the hold is
    # kept to: nothing is asked, and the result is left as it was.
    status, said = command(env, site, *PAID, paid=meta.client(env))
    assert (status, len(meta.tables), meta.tap_requests) == (1, 1, 0)
    assert "leaving Muse Ember alone after a refusal or a failure" in said
    assert layout.results.read_bytes() == written


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
        caveat.startswith(
            "No glossary was learned yet: the call was refused, so no paid request "
            "was sent to Muse Ember"
        )
        and "the run's hold was put on" in caveat
        for caveat in made["caveats"]
    )
    # What was not sent is the paid request: a live client counts tokens at Meta
    # before that. And the hold is on, so the command given again at once asks nothing.
    assert texts_of(env)["glossary_after"] == (
        "Not learned yet: no paid request was sent to Muse Ember and nothing was paid "
        "for it. The same command, given again after the run's hold has run out, asks."
    )
    assert "nothing was sent" not in json.dumps(made)
    # No glossary was learned, and nothing says one was: not the table's sentence,
    # and not the one about the closed hearing.
    assert "learned from" not in texts_of(env)["glossary_codes"]
    assert "was learned" not in results.CLOSED
    assert "glossary_cost_usd" not in numbers_of(env)
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

    made = {
        number.key: number
        for number in glossary.numbers(learned, truth, generated=False, live=True)
    }

    assert (made["glossary_codes"].value, made["glossary_checked"].value) == (7, 5)
    # Before: E and M, which a person wrote down. After: those and RC. The wrong
    # proposal for E does not replace what the person wrote.
    assert (made["glossary_before"].value, made["glossary_after"].value) == (2, 3)
    assert made["glossary_new"].value == 1
    assert "RC (rescue captain)" in made["glossary_new"].text
    # AM is added wrongly; the proposal for E is only compared; M and RS have none.
    assert (made["glossary_wrong"].value, made["glossary_overruled"].value) == (1, 1)
    assert "Of 1 proposals for codes among the five" in made["glossary_overruled"].text
    assert made["glossary_unknown"].value == 2
    assert made["glossary_left_out"].value == 2
    assert made["glossary_cost_usd"].value == pytest.approx(0.002)
    assert "the incidents Beacon made of recorded radio" in made["glossary_codes"].text
    assert "Muse Ember proposed them" in made["glossary_wrong"].text
    # A run whose incidents name no unit has no glossary and no numbers of one.
    nothing = glossary.Learned(state="nothing")
    assert glossary.numbers(nothing, truth, generated=True, live=False) == []
    # A claim that came to nothing names who was asked: a stand-in is not Ember.
    claimed = glossary.Learned(state="claimed", patterns=learned.patterns)
    by_key = {n.key: n for n in glossary.numbers(claimed, truth, generated=True, live=False)}
    assert by_key["glossary_after"].text.startswith(
        "Not learned: the call was claimed and nothing was kept of it. The stand-in for "
        "Ember failed"
    )
    assert "glossary_cost_usd" not in by_key


def test_an_answer_that_is_no_form_is_counted_with_the_taps_and_its_cost_is_still_reported(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout, reading, _ = scored(env, site)
    meta = Meta(layout)
    scope = SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000)
    runs = Runs(layout.taps, "mock")
    (layout.root / glossary.FILE).unlink()
    # Two answers to taps in a row were no form, as the replay counted them.
    runs.save_state({"held_until": None, "unusable_run": 2})
    meta.glossary_text = "The codes are engines and trucks."

    learned = glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)

    # A third in a row: the run is held, as it is after three answers to taps.
    assert (learned.state, learned.whole, len(meta.tables)) == ("made", False, 1)
    held_until, _ = read_hold(runs.state())
    assert held_until is not None
    assert held_until > datetime.now(UTC) + timedelta(minutes=9)
    # It was paid for, and its cost is reported though nothing of it is kept.
    by_key = {n.key: n for n in glossary.numbers(learned, {}, generated=True, live=True)}
    assert by_key["glossary_after"].value is None
    # No entry of it is a glossary's. The client does keep its recording of the
    # answer, so the sentence does not say that nothing of it is kept.
    assert by_key["glossary_after"].text == (
        "Not learned: what Muse Ember answered was not the form, and no glossary "
        "entries were kept. It is not asked again."
    )
    assert by_key["glossary_cost_usd"].value == pytest.approx(0.00015)
    assert "whether or not the answer was the form" in by_key["glossary_cost_usd"].text

    # Asked again with the same table, the client answers with its recording of
    # that answer, which nobody is paid for: it is not counted as news about Ember,
    # and it has no cost to report.
    (layout.root / glossary.FILE).unlink()
    runs.save_state({"held_until": None, "unusable_run": 1})
    again = glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)
    assert (again.model, again.whole, len(meta.tables)) == ("replay", False, 1)
    assert read_hold(runs.state()) == (None, 1)
    runs.close()
    by_key = {n.key: n for n in glossary.numbers(again, {}, generated=True, live=True)}
    assert by_key["glossary_cost_usd"].value is None
    assert "a recording of an earlier call" in by_key["glossary_cost_usd"].text


def test_an_answer_that_is_the_form_ends_the_runs_count_of_answers_that_were_not(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout, reading, _ = scored(env, site)
    meta = Meta(layout)
    runs = Runs(layout.taps, "mock")
    (layout.root / glossary.FILE).unlink()
    runs.save_state({"held_until": None, "unusable_run": 2})
    ember = meta.client(env)(SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000))

    learned = glossary.learn(layout, reading.incidents, ember, runs)

    assert (learned.model, learned.whole) == ("live", True)
    assert read_hold(runs.state()) == (None, 0)
    runs.close()


def test_a_client_that_was_closed_before_the_call_puts_no_hold_on_and_is_not_called_a_refusal(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout, reading, _ = scored(env, site)
    meta = Meta(layout)
    runs = Runs(layout.taps, "mock")
    (layout.root / glossary.FILE).unlink()
    ember = meta.client(env)(SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000))
    ember.close()

    learned = glossary.learn(layout, reading.incidents, ember, runs)

    assert (learned.state, learned.hold_set, meta.tables) == ("refused", False, [])
    assert read_hold(runs.state()) == (None, 0)
    runs.close()
    text = glossary.waiting_text(learned, live=True)
    assert text is not None
    assert "the client was closed, so no paid request was sent to Muse Ember" in text
    assert "hold" not in text
    assert "refused" not in text
    by_key = {n.key: n for n in glossary.numbers(learned, {}, generated=True, live=True)}
    assert by_key["glossary_after"].text.endswith("The same command, given again, asks.")
    # A stand-in is asked nothing and paid nothing, and is not said to be Ember.
    stand_in = glossary.waiting_text(learned, live=False)
    assert stand_in is not None
    assert "the stand-in for Ember was not asked" in stand_in
    assert "paid" not in stand_in


def test_no_glossary_is_asked_for_when_the_replays_hold_could_not_be_kept(
    env: dict[str, str], site: DataSf, speech: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    # The replay stopped because the investigations file could not be written: a
    # hold it put on after a failure may not be in the file.
    monkeypatch.setattr(
        taps, "replay", lambda *given, **more: taps.Replayed(made=0, stopped="store")
    )

    status, _ = command(env, site, *PAID, paid=meta.client(env))

    assert (status, meta.tables) == (1, [])
    assert not (layout.root / glossary.FILE).exists()
    assert "glossary_codes" not in numbers_of(env)


def test_a_run_with_unit_types_of_the_earlier_shape_fetches_them_again_and_keeps_its_glossary(
    env: dict[str, str], site: DataSf
) -> None:
    layout, _, _ = scored(env, site)
    before = numbers_of(env)
    # As an earlier Beacon left a run: one type for a unit under the old name, and
    # nothing under the new one. An empty file of that shape could also have meant
    # that every unit had a type Beacon has no kind for.
    (layout.root / glossary.TYPES_FILE).unlink()
    old = layout.root / "unit_types.json"
    old.write_text(json.dumps({"E36": "ENGINE"}))
    kept = (layout.root / glossary.FILE).read_bytes()
    asked = len(site.requests)

    status, _ = command(env, site, "--taps", "0")

    # The types were asked for once more, and nothing else was: the run's glossary
    # is the one it had, compared with the whole of DataSF's types.
    assert status == 0
    fetched = site.requests[asked:]
    assert [request.url.params["$select"] for request in fetched] == ["unit_id,unit_type"]
    assert json.loads((layout.root / glossary.TYPES_FILE).read_text())["E36"] == ["ENGINE"]
    assert json.loads(old.read_text()) == {"E36": "ENGINE"}
    assert (layout.root / glossary.FILE).read_bytes() == kept
    assert numbers_of(env) == before


def test_an_answer_is_never_kept_without_the_hold_it_put_on(
    env: dict[str, str], site: DataSf, speech: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, reading, _ = scored(env, site)
    meta = Meta(layout)
    scope = SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000)
    runs = Runs(layout.taps, "mock")
    (layout.root / glossary.FILE).unlink()
    runs.save_state({"held_until": None, "unusable_run": 2})
    meta.glossary_text = "The codes are engines and trucks."
    save = runs.save_state

    def fails(state: Any) -> None:
        raise RunsError("The investigations file is closed.")

    # The third answer in a row that is no form, and the investigations file
    # cannot be written when its hold is to be put in.
    monkeypatch.setattr(runs, "save_state", fails)
    with pytest.raises(RunsError):
        glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)
    monkeypatch.setattr(runs, "save_state", save)

    # The answer is kept, and so is what it did to the hold, beside it.
    assert json.loads((layout.root / glossary.FILE).read_text())["state"] == "made"
    assert read_hold(runs.state()) == (None, 2)
    assert (layout.root / glossary.HOLD_FILE).exists()
    # The next invocation puts the hold in before it reads it, as the command does,
    # and putting it in twice is putting it in once.
    written = (layout.root / glossary.HOLD_FILE).read_bytes()
    glossary.settle(layout, runs)
    held_until, streak = read_hold(runs.state())
    assert held_until is not None
    assert held_until > datetime.now(UTC) + timedelta(minutes=9)
    assert not (layout.root / glossary.HOLD_FILE).exists()
    (layout.root / glossary.HOLD_FILE).write_bytes(written)
    glossary.settle(layout, runs)
    assert read_hold(runs.state()) == (held_until, streak)
    assert taps.held_for(runs) is not None
    # Reading the glossary again asks nothing and changes nothing.
    again = glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)
    assert (again.state, again.whole, len(meta.tables)) == ("made", False, 1)
    assert read_hold(runs.state()) == (held_until, streak)
    runs.close()


def test_a_failure_is_never_kept_without_its_hold_and_a_refusal_gives_its_claim_up_after(
    env: dict[str, str], site: DataSf, speech: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, reading, _ = scored(env, site)
    meta = Meta(layout)
    scope = SpendScope(glossary.GLOSSARY_SCOPE, 250_000_000)
    runs = Runs(layout.taps, "mock")
    (layout.root / glossary.FILE).unlink()
    save = runs.save_state

    def fails(state: Any) -> None:
        raise RunsError("The investigations file is closed.")

    # Ember fails after it may have been asked, and the hold cannot be put in.
    meta.fail_glossary = True
    monkeypatch.setattr(runs, "save_state", fails)
    with pytest.raises(RunsError):
        glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)
    monkeypatch.setattr(runs, "save_state", save)
    assert json.loads((layout.root / glossary.FILE).read_text())["state"] == "claimed"
    assert read_hold(runs.state()) == (None, 0)

    # The claim is kept, so nothing is asked again, and the hold is put in first.
    meta.fail_glossary = False
    claimed = glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)
    assert (claimed.state, len(meta.tables)) == ("claimed", 1)
    assert taps.held_for(runs) is not None
    assert not (layout.root / glossary.HOLD_FILE).exists()

    # A refusal: the claim is given up, and the hold is written down before that,
    # so the call that is asked for later is not asked for during the hold.
    (layout.root / glossary.FILE).unlink()
    runs.save_state({"held_until": None, "unusable_run": 0})
    monkeypatch.setattr(runs, "save_state", fails)
    with pytest.raises(RunsError):
        glossary.learn(
            layout, reading.incidents, meta.client(env)(SpendScope(scope.label, 10_000)), runs
        )
    monkeypatch.setattr(runs, "save_state", save)
    assert not (layout.root / glossary.FILE).exists()
    held = glossary.learn(layout, reading.incidents, meta.client(env)(scope), runs)
    assert (held.state, len(meta.tables)) == ("held", 1)
    runs.close()


def test_the_command_puts_a_hold_the_glossary_wrote_down_in_before_it_replays_a_tap(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    status, _ = command(env, site, *PAID, "--taps", "0", paid=meta.client(env))
    assert status == 0
    asked = (len(meta.audio), meta.tap_requests, len(meta.tables))
    # As an invocation left it that kept its glossary's answer and then could not
    # write the investigations file: the hold is written down beside the glossary.
    until = datetime.now(UTC) + timedelta(minutes=10)
    (layout.root / glossary.HOLD_FILE).write_text(
        json.dumps({"held_until": until.isoformat(), "unusable_run": 3})
    )

    # Given again with taps to replay, which the complete run has none of.
    status, said = command(env, site, *PAID, "--taps", "5", paid=meta.client(env))

    assert status == 1
    assert "in the replay of taps or in learning the glossary" in said
    assert (len(meta.audio), meta.tap_requests, len(meta.tables)) == asked
    assert not (layout.root / glossary.HOLD_FILE).exists()
    runs = Runs(layout.taps, "mock")
    assert read_hold(runs.state()) == (until, 3)
    runs.close()

    # What was written down and cannot be read stops the run before anything is asked.
    (layout.root / glossary.HOLD_FILE).write_text('{"held_until": "soon"}')
    status, said = command(env, site, *PAID, "--taps", "5", paid=meta.client(env))
    assert status == 2
    assert "glossary_hold.json" in said
    assert (len(meta.audio), meta.tap_requests, len(meta.tables)) == asked
