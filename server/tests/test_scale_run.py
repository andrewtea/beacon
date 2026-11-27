"""The scale run, end to end, on an invented day in DataSF's shape: the command, a
run that is stopped and gone on with, the matching, the replayed taps, and what the
results say a number is. Nothing here is DataSF's data, and nothing is a recording."""

import base64
import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import EmberClient, SpendScope, Storage, fake_response
from beacon.evidence import EvidenceStore, NewEvidence, Point
from beacon.ingest.recordings import GENERATED_MARK
from beacon.investigate import Runs, stand_in_investigator
from beacon.radio import Ffmpeg
from beacon.radio.audio import pcm_to_wav, plain_wav
from beacon.scale import cli, datasf, generate, results, run, score, taps
from beacon.scale.cli import main
from tests.scale_day import (
    DAY,
    FELL_GOUGH,
    GEARY_17TH,
    HAYES_GOUGH,
    HAYES_OCTAVIA,
    DataSf,
    call,
)
from tests.test_ember_client import MetaApi

FIRE_TYPE = "Structure Fire / Smoke in Building"
# The invented day. Each call is there for what the run has to make of it.
ROWS = [
    # A fire, with its arrival and its clearing: found, at the nearest listed block.
    *call("260620001", FIRE_TYPE, "08:01:30", HAYES_GOUGH, ("E36", "T06", "B02"),
          on_scene="08:05:00", available="08:40:00", alarms="2"),
    # A medical call at the same corner while the fire is going: an incident of its
    # own, never the fire's.
    *call("260620002", "Medical Incident", "08:10:00", HAYES_GOUGH, ("M552", "E05"),
          on_scene="08:16:00", available="08:50:00"),
    # Two alarms at one corner eight minutes apart: Beacon makes one incident of
    # them, so the second call is inside the first's.
    *call("260620003", "Alarms", "10:00:00", GEARY_17TH, ("E31",), available="10:20:00"),
    *call("260620004", "Alarms", "10:08:00", GEARY_17TH, ("T12",), available="10:25:00"),
    # A call whose only unit has no id Beacon keeps: no call is generated for it.
    *call("260620005", "Other", "11:00:00", FELL_GOUGH, ("SCRT1",)),
    # A unit that arrives seventy minutes on, after its incident went quiet: Beacon
    # makes a second incident of the one call.
    *call("260620006", "Traffic Collision", "13:00:00", HAYES_OCTAVIA, ("E05",),
          on_scene="14:10:00", available="14:30:00"),
]  # fmt: skip
# Dispatch, arrival, and clearing for the fire, the medical call, and the collision,
# and dispatch and clearing for each alarm.
CALLS = 3 + 3 + 2 + 2 + 3


@pytest.fixture
def site() -> DataSf:
    return DataSf(ROWS)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"BEACON_DATA_DIR": str(tmp_path / "data")}


def command(
    env: dict[str, str], site: DataSf, *more: str, paid: Any = None, name: str = "day"
) -> tuple[int, str]:
    out = StringIO()
    http = site.http()
    try:
        status = main(["--name", name, "--day", DAY, *more], env, out, http, paid)
    finally:
        http.close()
    return status, out.getvalue()


def numbers_of(env: dict[str, str], name: str = "day") -> dict[str, Any]:
    made = results.read(run.Layout.under(env, name))
    assert made is not None
    return {number["key"]: number["value"] for number in made["numbers"]}


def texts_of(env: dict[str, str], name: str = "day") -> dict[str, str]:
    made = results.read(run.Layout.under(env, name))
    assert made is not None
    return {number["key"]: number["text"] for number in made["numbers"]}


def test_a_stand_in_run_goes_from_datasfs_record_to_results_that_say_what_they_are(
    env: dict[str, str], site: DataSf
) -> None:
    status, said = command(env, site)

    assert status == 0
    layout = run.Layout.under(env, "day")
    made = results.read(layout)
    assert made is not None
    # What the run is comes first, on the terminal and in the file.
    assert said.startswith(results.GENERATED_BY_STAND_IN)
    assert (made["basis_text"], made["stand_in"]) == (results.GENERATED_BY_STAND_IN, True)
    assert (made["heard_complete"], made["complete"]) == (True, True)
    said_of = texts_of(env)
    assert "0 of them are counted above as found and 0 as taken" in said_of["not_generated"]
    assert "nothing checks that it is right" in said_of["found"]
    assert "DataSF's number for the call" in said_of["records"]
    assert (made["audio"], made["voice"], made["hearer"]) == ("generated", "tones", "fake")
    assert made["scored_against"]["url"] == "https://data.sf.gov/d/nuek-vuh3"
    numbers = numbers_of(env)
    assert (numbers["records"], numbers["not_generated"], numbers["unplaced"]) == (6, 1, 0)
    assert (numbers["transmissions"], numbers["no_row"]) == (CALLS, 0)
    assert (numbers["unready"], numbers["unusable"]) == (0, 0)
    # The fire, the medical call, the alarms' one incident, and the collision's two.
    assert numbers["incidents"] == 5
    assert (numbers["found"], numbers["found_share"]) == (4, 66.7)
    assert (numbers["merged"], numbers["missed"], numbers["split"], numbers["extra"]) == (
        1,
        1,
        1,
        0,
    )
    assert numbers["other_kind"] == 0
    # From each corner to the middle of the listed block nearest it: fifty metres
    # up a cross street for three, and seven along Geary for the alarms.
    assert numbers["placed"] == 4
    assert numbers["place_median_m"] == pytest.approx(50.3, abs=1.0)
    assert numbers["place_p90_m"] == pytest.approx(50.3, abs=1.0)
    # The calls are generated, and marked so.
    assert (layout.calls / GENERATED_MARK).is_file()
    assert generate.read_script(layout.calls).calls == CALLS


def test_a_stand_ins_run_reports_no_cost_no_speed_and_no_lead_time(
    env: dict[str, str], site: DataSf
) -> None:
    command(env, site)

    numbers, texts = numbers_of(env), texts_of(env)
    for key in (
        "radio_cost_usd",
        "radio_cost_per_1000_usd",
        "heard_after_dispatch_s",
        "tap_cost_usd",
        "tap_seconds_p95",
    ):
        assert numbers[key] is None
        assert texts[key].startswith("Not measured:")
    assert "radio_paid_calls" not in numbers
    # And what is counted says, beside the count, that it is no measure of accuracy.
    for key in ("found", "found_share"):
        assert "a check of the pipeline, not a measure of accuracy" in texts[key]
    assert "not a location error of Beacon's hearing" in texts["place_median_m"]
    assert "a stand-in for Ember, by rule" in texts["taps"]
    # Nothing was paid: the ledger holds a stand-in's calls and no live one.
    ember = EmberClient.replay_only(Storage.under(Path(env["BEACON_DATA_DIR"])))
    assert ember.spent_usd() == 0
    for scope in (run.RADIO_SCOPE, run.SPEECH_SCOPE, run.TAPS_SCOPE):
        assert ember.scope_spend(scope).calls == 0


def test_every_row_of_a_generated_run_is_a_stand_ins_and_no_tap_is_stored(
    env: dict[str, str], site: DataSf
) -> None:
    command(env, site)

    layout = run.Layout.under(env, "day")
    store = EvidenceStore(layout.evidence, "mock")
    rows = store.search(limit=1000)
    assert len(rows) == CALLS
    assert {(row.kind, row.source, row.origin, row.stand_in) for row in rows} == {
        ("radio", "recordings", "mock", True)
    }
    # The server's own stores are not touched: a run has its own.
    assert not (Path(env["BEACON_DATA_DIR"]) / "evidence").exists()
    assert not (Path(env["BEACON_DATA_DIR"]) / "investigate").exists()


def test_the_same_command_again_fetches_hears_and_investigates_nothing_again(
    env: dict[str, str], site: DataSf
) -> None:
    command(env, site)
    layout = run.Layout.under(env, "day")
    asked = len(site.requests)
    ledger = Storage.under(Path(env["BEACON_DATA_DIR"])).ledger_db
    booked = _ledger_rows(ledger)
    first = numbers_of(env)

    status, _ = command(env, site)

    assert status == 0
    assert len(site.requests) == asked
    # Not one more call to the stand-in, for a radio call or for a tap.
    assert _ledger_rows(ledger) == booked
    assert numbers_of(env) == first
    assert (layout.calls / GENERATED_MARK).is_file()


def _ledger_rows(ledger: Path) -> int:
    import sqlite3

    with sqlite3.connect(ledger) as db:
        return db.execute("SELECT COUNT(*) FROM ember_calls").fetchone()[0]


def test_a_run_is_one_kind_for_good(env: dict[str, str], site: DataSf) -> None:
    command(env, site)
    layout = run.Layout.under(env, "day")
    before = layout.results.read_bytes()

    for change in (["--voice", "speech"], ["--voice", "speech", "--paid"]):
        status, said = command(env, site, *change)
        assert status == 2
        assert "This run was begun as another" in said
    out = StringIO()
    http = site.http()
    assert main(["--name", "day", "--day", "2026-03-02"], env, out, http) == 2
    http.close()

    assert "This run was begun as another" in out.getvalue()
    assert layout.results.read_bytes() == before


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        (["--paid"], "Muse Ember is not paid to hear one"),
        (["--recordings", "/somewhere"], "--recordings needs --paid"),
        (["--recordings", "/somewhere", "--paid"], "set BEACON_RECORDINGS_PERMITTED=1"),
        (["--voice", "speech", "--paid", "--radio-budget", "1.26"], "at most $1.25"),
        (["--voice", "speech", "--paid", "--radio-budget", "0"], "more than nothing"),
        (["--voice", "speech", "--paid", "--radio-budget", "lots"], "A budget is a dollar amount"),
        (["--voice", "speech", "--paid", "--taps-budget", "0.51"], "at most $0.50"),
        (["--taps", "301"], "From 0 to 300 taps"),
        (["--voice", "speech", "--paid"], "A paid run needs a live Ember"),
    ],
)
def test_a_run_that_cannot_be_what_was_asked_for_is_not_made(
    env: dict[str, str],
    site: DataSf,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    problem: str,
) -> None:
    monkeypatch.setattr(generate, "can_speak", lambda: True)
    monkeypatch.setattr(generate, "_speech", lambda root, words: _clip(words))

    status, said = command(env, site, *options)

    assert status == 2
    assert said.splitlines()[-1].startswith("The scale run was not made.")
    assert problem in said
    # Nothing was fetched or heard, and nothing can have been paid.
    assert site.requests == []
    layout = run.Layout.under(env, "day")
    assert not layout.evidence.exists() or EvidenceStore(layout.evidence, "mock").last_id() == 0
    assert not layout.results.exists()


def test_a_day_that_is_not_over_and_a_name_that_is_a_path_are_refused(
    env: dict[str, str], site: DataSf
) -> None:
    out = StringIO()
    today = datetime.now(UTC).date().isoformat()

    assert main(["--name", "day", "--day", today], env, out, site.http()) == 2
    assert main(["--name", "../day", "--day", DAY], env, out, site.http()) == 2

    assert "--day is a day that is over" in out.getvalue()
    assert "A run is named with letters" in out.getvalue()
    assert site.requests == []
    assert not (Path(env["BEACON_DATA_DIR"]) / "scale").exists()


# A paid run: Muse Ember, over a transport that stands where Meta's API is.


def _clip(words: str) -> bytes:
    """Stands in for synthesized speech: a clip of its own for each call's words,
    two seconds long."""
    seed = hashlib.sha256(words.encode()).digest()
    return pcm_to_wav((seed * 2000)[:64_000])


class Meta:
    """Answers as Meta's API would, by rule: a radio call with the form the calls'
    script has for its audio, and a tap's investigation as the stand-in does. It
    counts each audio it was sent, and can fail on one."""

    def __init__(self, layout: run.Layout, heard: Callable[[dict[str, Any]], Any] | None = None):
        self.layout = layout
        self.heard = heard
        self.audio: list[str] = []
        self.fail_on: int | None = None
        # Requests of the taps' investigations, which can fail on one, and what is
        # done before one is answered.
        self.tap_requests = 0
        self.fail_tap_on: int | None = None
        self.before_tap: Callable[[], None] | None = None
        self.api = MetaApi(input_tokens=1_000, create=self.create)

    def create(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("text", {}).get("format", {}).get("name") != "transmission":
            self.tap_requests += 1
            if self.before_tap is not None:
                self.before_tap()
            if self.fail_tap_on == self.tap_requests:
                return httpx2.Response(500, json={"error": {"message": "It failed."}})
            answer = dict(stand_in_investigator(body))
        else:
            digest = _digest(body)
            self.audio.append(digest)
            if self.fail_on is not None and len(self.audio) == self.fail_on:
                return httpx2.Response(500, json={"error": {"message": "It failed."}})
            hear = self.heard or generate.read_script(self.layout.calls).hearer
            answer = dict(hear(body))
        # A thousand tokens in and a hundred out: $0.00015 a request.
        answer["usage"] = {
            "input_tokens": 1_000,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 1_100,
        }
        return httpx2.Response(200, json=answer)

    def client(self, env: dict[str, str]) -> Callable[[SpendScope], EmberClient]:
        storage = Storage.under(Path(env["BEACON_DATA_DIR"]))
        return lambda scope: self.api.client(storage, scope=scope)

    @property
    def paid(self) -> int:
        return self.api.paths().count("/v1/responses")


def _digest(body: dict[str, Any]) -> str:
    (message,) = body["input"]
    return hashlib.sha256(
        base64.b64decode(message["content"][1]["input_audio"]["data"])
    ).hexdigest()


@pytest.fixture
def speech(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    monkeypatch.setattr(generate, "can_speak", lambda: True)
    monkeypatch.setattr(generate, "_speech", lambda root, words: _clip(words))


PAID = ("--voice", "speech", "--paid")


def test_a_paid_run_of_generated_speech_reports_what_the_ledger_holds_and_says_what_it_heard(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)

    status, said = command(env, site, *PAID, paid=meta.client(env))

    assert status == 0
    made = results.read(layout)
    assert made is not None
    # Paid, and still not radio: a stand-in had a part in every number. What the
    # run is says what Ember is paid for, and the numbers say what it was paid.
    assert said.startswith(results.GENERATED_BY_EMBER)
    assert "is paid for each one it is asked to hear" in results.GENERATED_BY_EMBER
    assert "was paid" not in results.GENERATED_BY_EMBER + results.RECORDED_BY_EMBER
    assert (made["basis_text"], made["stand_in"], made["hearer"]) == (
        results.GENERATED_BY_EMBER,
        True,
        "live",
    )
    numbers, texts = numbers_of(env), texts_of(env)
    assert (numbers["transmissions"], numbers["found"]) == (CALLS, 4)
    # Each call once, at $0.00015: twelve of them.
    assert sorted(meta.audio) == sorted(set(meta.audio))
    assert numbers["radio_paid_calls"] == CALLS
    assert numbers["radio_cost_usd"] == pytest.approx(CALLS * 0.00015)
    assert numbers["radio_cost_per_1000_usd"] == pytest.approx(0.15)
    assert "generated speech, not radio" in texts["radio_cost_usd"]
    assert "not of real radio" in texts["found"]
    # Generated calls are placed at DataSF's times, paid for or not.
    assert numbers["heard_after_dispatch_s"] is None
    # The taps were investigated by the paid client too, under their own scope.
    ember = EmberClient.replay_only(Storage.under(Path(env["BEACON_DATA_DIR"])))
    radio, tapped = ember.scope_spend(run.SPEECH_SCOPE), ember.scope_spend(run.TAPS_SCOPE)
    assert ember.scope_spend(run.RADIO_SCOPE).calls == 0
    assert (radio.calls, radio.unanswered) == (CALLS, 0)
    assert tapped.calls == meta.paid - CALLS > 0
    assert numbers["tap_cost_total_usd"] == pytest.approx(tapped.spent_nanousd / 1e9)
    assert numbers["tap_cost_usd"] == pytest.approx(tapped.spent_nanousd / 1e9 / numbers["taps"])
    # The rows are a stand-in's all the same: the audio was generated.
    rows = EvidenceStore(layout.evidence, "mock").search(limit=1000)
    assert {(row.origin, row.model, row.stand_in) for row in rows} == {("mock", "live", True)}


def test_a_run_that_stops_at_its_cap_says_so_and_pays_for_nothing_past_it(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)

    # Each request could cost $0.0001 for its input and more for its answer: a cap
    # of a tenth of a cent admits a few of the twelve and then refuses.
    status, said = command(env, site, *PAID, "--radio-budget", "0.001", paid=meta.client(env))

    assert status == 1
    assert "The run is not finished. Give the same command again" in said
    made = results.read(layout)
    assert made is not None
    assert made["complete"] is False
    assert made["caveats"][:2] == [
        results.INCOMPLETE,
        "The run stopped at its spending cap or the spending stop.",
    ]
    heard = numbers_of(env)["transmissions"]
    assert 0 < heard < CALLS
    assert meta.paid == heard
    spend = EmberClient.replay_only(Storage.under(Path(env["BEACON_DATA_DIR"]))).scope_spend(
        run.SPEECH_SCOPE
    )
    assert 0 < spend.spent_nanousd <= 1_000_000
    # No tap is replayed against part of a day, and none is claimed.
    assert "No tap is replayed until every call has been heard." in said
    assert "taps" not in numbers_of(env)
    planned = taps.plan(run.read_records(layout), 300)
    assert [state for _, state, _ in taps.kept(planned, Runs(layout.taps, "mock"))] == [
        "waiting"
    ] * len(planned)

    # Given again at once, the run is leaving Ember alone, and pays nothing.
    status, said = command(env, site, *PAID, "--radio-budget", "0.001", paid=meta.client(env))
    assert status == 1
    assert meta.paid == heard
    assert "leaving Muse Ember alone for ten minutes" in said


def test_a_run_that_failed_goes_on_without_paying_for_a_call_twice(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    settings = run.begin(
        layout, run.Settings(day=DAY, audio="generated", voice="speech", hearer="live")
    )
    http = site.http()
    run.prepare(layout, settings, http)
    http.close()
    meta = Meta(layout)
    meta.fail_on = 5
    store = run.open_store(layout, settings)
    noon = datetime(2026, 3, 10, 20, 0, tzinfo=UTC)

    def hear(at: datetime) -> run.Heard:
        ember = run.radio_ember(layout, settings, env, run.RADIO_BUDGET_NANOUSD, meta.client(env))
        return run.hear(layout, settings, store, ember, env, now=noon, wall_clock=lambda: at)

    first = hear(noon)

    # Meta failed on the fifth call, after its request was sent.
    assert (first.stopped, first.last_pass) == ("ember", None)
    assert (store.last_id(), meta.paid) == (4, 5)
    # Sooner than ten minutes on, nothing is asked.
    assert hear(noon + timedelta(minutes=9)).stopped == "held"
    assert meta.paid == 5

    last = hear(noon + timedelta(minutes=11))

    assert last.stopped is None
    assert last.last_pass == run.PassCount(stored=CALLS - 5)
    # Every call was sent once: the one Meta failed on may have been paid for, so
    # it stays claimed, has no row, and is not asked about again.
    assert len(meta.audio) == len(set(meta.audio)) == CALLS
    assert (store.last_id(), store.claimed_without_row("recordings")) == (CALLS - 1, 1)
    assert last.spend is not None
    assert (last.spend.calls, last.spend.unanswered) == (CALLS, 1)


def test_one_scale_run_is_made_at_a_time_under_a_data_directory(
    env: dict[str, str], site: DataSf
) -> None:
    directory = Path(env["BEACON_DATA_DIR"]) / "scale"
    directory.mkdir(parents=True)
    with open(directory / ".lock", "a", encoding="utf-8") as another:
        # Another invocation, of this name or any other, is under way.
        fcntl.flock(another, fcntl.LOCK_EX)
        status, said = command(env, site)
        assert status == 2
        assert "Another scale run is under way under this data directory" in said
        assert site.requests == []
        assert not run.Layout.under(env, "day").root.exists()

    # Its lock went with its file, and the run is made.
    assert command(env, site)[0] == 0


def test_a_replay_that_ember_fails_on_leaves_the_run_unfinished_and_counts_no_answer(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    meta.fail_tap_on = 1

    status, said = command(env, site, *PAID, paid=meta.client(env))

    # Every call was heard, and the run is not complete: the replay stopped.
    assert status == 1
    assert "The run is not finished." in said
    made = results.read(layout)
    assert made is not None
    assert (made["heard_complete"], made["complete"]) == (True, False)
    assert results.INCOMPLETE not in made["caveats"]
    assert any("4 planned taps have not been put to the investigator" in c for c in made["caveats"])
    numbers, texts = numbers_of(env), texts_of(env)
    # The tap Ember failed on is kept as cut short, and is no answer: not for the
    # count of answers, and not for the cost or the time of one.
    assert (numbers["taps"], numbers["taps_unanswered"], numbers["taps_lost"]) == (0, 1, 0)
    assert (numbers["taps_near_matched"], numbers["taps_away_unconfirmed"]) == (0, 0)
    assert (numbers["tap_cost_usd"], numbers["tap_seconds_p95"]) == (None, None)
    assert (
        "1 requests ended in an error after they may have been sent"
        in (texts["tap_cost_total_usd"])
    )
    paid = meta.paid

    assert any("A call to Muse Ember failed" in caveat for caveat in made["caveats"])
    kept = layout.results.read_bytes()

    # Given again at once, the whole run is leaving Ember alone: the hold of the
    # replay is asked about before anything is heard, nothing is asked of Ember for
    # a call or for a tap, and what the run came to is left as it was.
    status, said = command(env, site, *PAID, paid=meta.client(env))
    assert (status, meta.paid) == (1, paid)
    assert "leaving Ember alone after a refusal or a failure in the replay of taps" in said
    assert layout.results.read_bytes() == kept


def test_a_tap_is_not_begun_when_its_scope_has_less_left_than_a_tap_may_cost(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)

    status, _ = command(env, site, *PAID, "--taps-budget", "0.005", paid=meta.client(env))

    # Half a cent is less than the cent one tap is held to: none is claimed.
    assert (status, meta.tap_requests) == (1, 0)
    made = results.read(layout)
    assert made is not None
    assert any("less left than one tap may cost" in caveat for caveat in made["caveats"])
    planned = taps.plan(run.read_records(layout), 300)
    runs = Runs(layout.taps, "mock")
    assert [state for _, state, _ in taps.kept(planned, runs)] == ["waiting"] * 5
    runs.close()

    # With the whole budget, the same taps are investigated.
    assert command(env, site, *PAID, paid=meta.client(env))[0] == 0
    assert numbers_of(env)["taps"] == 5


def _delete_ledger(env: dict[str, str]) -> None:
    ledger = Storage.under(Path(env["BEACON_DATA_DIR"])).ledger_db
    for path in (
        ledger,
        ledger.with_name(ledger.name + "-wal"),
        ledger.with_name(ledger.name + "-shm"),
    ):
        path.unlink(missing_ok=True)


def test_a_ledger_replaced_between_the_hearing_and_the_taps_is_not_trusted_as_empty(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    # The ledger is deleted when the first tap's first request is out.
    meta.before_tap = lambda: _delete_ledger(env)

    status, said = command(env, site, *PAID, paid=meta.client(env))

    assert status == 1
    assert said.rstrip().endswith(run.LEDGER_REPLACED)
    assert "Give the same command again" not in said
    made = results.read(layout)
    assert made is not None
    assert made["complete"] is False
    assert results.LEDGER_LOST in made["caveats"]
    # The taps' client was made on the ledger the hearing paid in, so it paid for
    # one request and no more: it did not begin a count from nothing in a new file.
    assert meta.tap_requests == 1
    numbers, texts = numbers_of(env), texts_of(env)
    assert numbers["radio_cost_usd"] is None
    assert texts["radio_cost_usd"].startswith("Not known: the Ember ledger was replaced")
    assert "radio_paid_calls" not in numbers
    paid = meta.paid

    # And the run is not gone on with, in the new ledger or any other.
    status, said = command(env, site, *PAID, paid=meta.client(env))
    assert (status, meta.paid) == (2, paid)
    assert run.LEDGER_REPLACED in said


def test_a_paid_run_goes_on_only_in_the_ledger_it_began_to_pay_in(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    # Stopped at its cap part of the way.
    assert command(env, site, *PAID, "--radio-budget", "0.001", paid=meta.client(env))[0] == 1
    paid = meta.paid
    assert 0 < paid < CALLS

    # The ledger is deleted, and with it the count the cap is held by.
    _delete_ledger(env)
    status, said = command(env, site, *PAID, paid=meta.client(env))

    assert (status, meta.paid) == (2, paid)
    assert run.LEDGER_REPLACED in said


def test_a_ledger_put_back_from_a_copy_after_the_last_request_is_not_read_as_what_was_spent(
    env: dict[str, str], site: DataSf, speech: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    ledger = Storage.under(Path(env["BEACON_DATA_DIR"])).ledger_db
    real_hear = cli.hear

    def hear_then_restore(*args: Any, **options: Any) -> run.Heard:
        # A copy of the ledger from before anything was heard, of the same
        # generation, is put back over it when the last call has been paid for and
        # before the run reads what it spent.
        copy = ledger.with_name("copy.sqlite3")
        with sqlite3.connect(ledger) as source, sqlite3.connect(copy) as target:
            source.backup(target)
        heard = real_hear(*args, **options)
        for beside in ("-wal", "-shm"):
            ledger.with_name(ledger.name + beside).unlink(missing_ok=True)
        os.replace(copy, ledger)
        return heard

    monkeypatch.setattr(cli, "hear", hear_then_restore)

    status, said = command(env, site, *PAID, "--taps", "0", paid=meta.client(env))

    # Every call was paid for, and the file at the ledger's path says none was. No
    # request was left to refuse, and the run still says its ledger is another.
    assert meta.paid == CALLS
    assert status == 1
    assert said.rstrip().endswith(run.LEDGER_REPLACED)
    made = results.read(layout)
    assert made is not None
    assert (made["complete"], results.LEDGER_LOST in made["caveats"]) == (False, True)
    assert numbers_of(env)["radio_cost_usd"] is None
    assert "radio_paid_calls" not in numbers_of(env)
    monkeypatch.setattr(cli, "hear", real_hear)
    assert command(env, site, *PAID, "--taps", "0", paid=meta.client(env))[0] == 2


def test_a_paid_run_is_refused_in_a_copy_of_its_ledger_that_was_put_back(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    meta = Meta(layout)
    assert command(env, site, *PAID, "--radio-budget", "0.001", paid=meta.client(env))[0] == 1
    paid = meta.paid
    ledger = Storage.under(Path(env["BEACON_DATA_DIR"])).ledger_db
    # The same ledger, by its generation and by everything in it, and another file.
    copy = ledger.with_name("copy.sqlite3")
    with sqlite3.connect(ledger) as source, sqlite3.connect(copy) as target:
        source.backup(target)
    for beside in ("-wal", "-shm"):
        ledger.with_name(ledger.name + beside).unlink(missing_ok=True)
    os.replace(copy, ledger)

    status, said = command(env, site, *PAID, paid=meta.client(env))

    assert (status, meta.paid) == (2, paid)
    assert run.LEDGER_REPLACED in said


def test_results_are_published_while_the_run_still_holds_its_lock(
    env: dict[str, str], site: DataSf, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[bool] = []
    write = results.write

    def write_and_ask(layout: run.Layout, made: dict[str, Any]) -> None:
        with open(layout.root.parent / ".lock", "a", encoding="utf-8") as another:
            try:
                fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except OSError:
                held.append(True)
        write(layout, made)

    monkeypatch.setattr(results, "write", write_and_ask)

    assert command(env, site)[0] == 0
    assert held == [True]


def test_a_medical_call_of_generated_speech_is_kept_as_medical_whatever_ember_made_of_it(
    env: dict[str, str], site: DataSf, speech: None
) -> None:
    layout = run.Layout.under(env, "day")
    # An Ember that hears a working fire with an alarm level in every call.
    fire = form("working", units=["M552"], alarm=3, details=["evacuation"])
    meta = Meta(layout, lambda body: fake_response(text=json.dumps(fire)))

    command(env, site, *PAID, "--taps", "0", paid=meta.client(env))

    rows = EvidenceStore(layout.evidence, "mock").search(limit=1000)
    medical = [row for row in rows if row.data["talkgroup"] == generate.EMS_TALKGROUP]
    # The medical call's dispatch, arrival, and clearing were on the EMS-only talkgroup.
    assert len(medical) == 3
    assert {
        (row.data["event"], row.data["status"], row.data["alarm"], tuple(row.data["details"]))
        for row in medical
    } == {("medical response", "unknown", None, ())}
    assert len(rows) == CALLS


def test_recordings_of_a_day_are_heard_from_two_hours_after_its_end_by_the_citys_clock(
    env: dict[str, str], site: DataSf, recorder: tuple[Path, dict[str, Any]]
) -> None:
    # The day the clocks go forward is 23 hours long, and the day they go back 25.
    assert run.day_window(datasf.date(2026, 3, 8)) == (
        datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
    )
    assert run.day_window(datasf.date(2026, 11, 1)) == (
        datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
    )
    root, by_digest = recorder
    layout = run.Layout.under(env, "day")
    settings = run.begin(
        layout,
        run.Settings(
            day=DAY, audio="recorded", voice=None, hearer="live", system="sfd", talkgroups=(1201,),
            ems_talkgroups=(),
        ),
    )  # fmt: skip
    http = site.http()
    run.prepare(layout, settings, http)
    http.close()
    meta = Meta(layout, heard_by(by_digest))
    store = run.open_store(layout, settings)
    ember = run.radio_ember(layout, settings, env, run.RADIO_BUDGET_NANOUSD, meta.client(env))

    # A minute before the two hours are over, the day's last calls can still come:
    # a pass that ended now would be taken for the whole day's.
    with pytest.raises(run.ScaleError, match="The day's last calls can still be recorded"):
        run.hear(
            layout, settings, store, ember, env, recordings=root,
            now=datetime(2026, 3, 4, 9, 59, tzinfo=UTC),
        )  # fmt: skip
    assert meta.paid == 0


def test_what_a_run_fetched_is_whole_or_fetched_again(env: dict[str, str], site: DataSf) -> None:
    command(env, site, "--taps", "0")
    layout = run.Layout.under(env, "day")
    assert not list(layout.root.rglob("*.part"))
    # A preparation that was stopped part of the way: the file that says the lists
    # are all there, and the one that says the calls are, were never put in place.
    blocks = layout.lists / "blocks.csv"
    whole = blocks.read_text()
    blocks.unlink()
    (layout.lists / "blocks.csv.part").write_text(whole[:200])
    (layout.calls / generate.SCRIPT_FILE).unlink()
    asked = len(site.requests)

    assert command(env, site, "--taps", "0")[0] == 0

    assert len(site.requests) > asked
    assert blocks.read_text() == whole
    assert generate.read_script(layout.calls).calls == CALLS
    # The calls are the same ones under the same names, so none is heard again.
    assert EvidenceStore(layout.evidence, "mock").last_id() == CALLS


def test_a_call_of_datasfs_that_cannot_be_read_is_counted_and_said_to_be_in_no_count(
    env: dict[str, str],
) -> None:
    unread = call("260620099", "Alarms", "12:00:00", FELL_GOUGH, ("E36",))
    del unread[0]["dispatch_dttm"]
    # Two units' rows with no call number: of one call or of two, nothing says.
    nameless = call("260620098", "Alarms", "12:30:00", FELL_GOUGH, ("E36", "T06"))
    for row in nameless:
        del row["call_number"]
    site = DataSf([*ROWS, *unread, *nameless])

    _, said = command(env, site, "--taps", "0")

    assert "6 calls in DataSF's record of 2026-03-03 that Beacon could read." in said
    numbers, texts = numbers_of(env), texts_of(env)
    assert (numbers["records"], numbers["records_unreadable"]) == (6, 1)
    assert numbers["rows_unreadable"] == 2
    assert "They are in no count below" in texts["records_unreadable"]
    assert "a count of rows and not of calls" in texts["rows_unreadable"]
    assert "of the DataSF calls Beacon could read" in texts["found_share"]
    assert numbers["found_share"] == 66.7


# Matching, on the run's own incidents.


def scored(env: dict[str, str], site: DataSf) -> tuple[run.Layout, score.Reading, score.Matched]:
    command(env, site, "--taps", "0")
    layout = run.Layout.under(env, "day")
    records = run.read_records(layout)
    store = EvidenceStore(layout.evidence, "mock")
    reading = score.read_incidents(store, run.read_lists(layout)[1], records)
    return layout, reading, score.match(records, reading.incidents)


def test_each_call_is_found_inside_another_missed_or_split_as_the_day_was_made(
    env: dict[str, str], site: DataSf
) -> None:
    _, reading, matched = scored(env, site)

    found = {pair.record.number: pair.incident for pair in matched.found}
    assert sorted(found) == ["260620001", "260620002", "260620003", "260620006"]
    assert [record.number for record in matched.merged] == ["260620004"]
    assert [record.number for record in matched.missed] == ["260620005"]
    # A medical call is matched to a medical response, and the fire at its corner
    # to the fire, though both are at one block in the same hour.
    assert found["260620002"].category == "medical response"
    assert (found["260620001"].category, found["260620001"].alarm) == ("fire", 2)
    assert found["260620001"].place == found["260620002"].place == "300 block of Gough St"
    assert found["260620001"].id != found["260620002"].id
    # The second alarm is inside the first's incident, which names both engines.
    assert matched.incident_of("260620004") == found["260620003"].id
    assert found["260620003"].units == ("E31", "T12")
    assert matched.incident_of("260620005") is None
    # The collision's later incident has its unit and no place.
    (split,) = matched.split
    assert (split.category, split.units, split.place) == ("collision", ("E5",), None)
    assert matched.extra == ()
    assert reading.partial is False


def test_an_incident_no_call_of_the_day_fits_is_counted_as_one(
    env: dict[str, str], site: DataSf
) -> None:
    layout, _, _ = scored(env, site)
    store = EvidenceStore(layout.evidence, "mock")
    records = run.read_records(layout)
    for ref, unit, hour in (("far", "E36", 23), ("stranger", "E99", 18)):
        store.add(
            [
                NewEvidence(
                    kind="radio",
                    source="recordings",
                    ref=ref,
                    # Hours after its unit's call was cleared, or a unit of no call.
                    occurred_at=datetime(2026, 3, 3, hour, 30, tzinfo=UTC),
                    confidence="medium",
                    origin="mock",
                    model="fake",
                    data={"event": "fire", "category": "fire", "clarity": "clear",
                          "status": "working", "details": [], "units": [unit], "alarm": None,
                          "street": None, "block": None, "cross_street": None},
                )
            ]
        )  # fmt: skip

    reading = score.read_incidents(store, run.read_lists(layout)[1], records)
    matched = score.match(records, reading.incidents)

    # Neither is the fire's, though one names its engine: it opened hours after.
    assert sorted(incident.units for incident in matched.extra) == [("E36",), ("E99",)]
    assert len(matched.found) == 4


def test_a_call_is_taken_to_be_inside_only_an_incident_of_its_kind_that_is_anothers_own(
    env: dict[str, str], site: DataSf
) -> None:
    layout, _, _ = scored(env, site)
    store = EvidenceStore(layout.evidence, "mock")
    records = run.read_records(layout)
    # An incident that is no call's: one engine heard from twice, late in the day.
    for ref, minute in (("late-1", 0), ("late-2", 30)):
        store.add(
            [
                NewEvidence(
                    kind="radio",
                    source="recordings",
                    ref=ref,
                    occurred_at=datetime(2026, 3, 3, 23, minute, tzinfo=UTC),
                    confidence="medium",
                    origin="mock",
                    model="fake",
                    data={"event": "fire", "category": "fire", "clarity": "clear",
                          "status": "working", "details": [], "units": ["E36"], "alarm": None,
                          "street": None, "block": None, "cross_street": None},
                )
            ]
        )  # fmt: skip

    def record(number: str, kind: str, at: datetime) -> datasf.Record:
        return datasf.Record(
            number=number, kind=kind, received_at=at, dispatched_at=at, on_scene_at=None,
            cleared_at=None, streets=(), point=None, units=("E36",), alarms=None,
        )  # fmt: skip

    # A medical call of that engine's while the day's fire was going, and another
    # call of its while the late incident was, which opened before the call came.
    medical = record("260620101", "medical", datetime(2026, 3, 3, 16, 3, tzinfo=UTC))
    late = record("260620102", "alarm", datetime(2026, 3, 3, 23, 25, tzinfo=UTC))
    day = [*records, medical, late]

    reading = score.read_incidents(store, run.read_lists(layout)[1], day)
    matched = score.match(day, reading.incidents)

    # Neither is inside anything: the fire is no medical response, and the late
    # incident is counted as fitting no call, and so not as holding one.
    assert {"260620101", "260620102"} <= {missed.number for missed in matched.missed}
    assert [incident.units for incident in matched.extra] == [("E36",)]
    assert [inside.number for inside in matched.merged] == ["260620004"]


def test_a_tap_away_from_every_call_is_away_from_them_where_it_is_put_after_rounding() -> None:
    def at(number: str, lat: float, lon: float) -> datasf.Record:
        when = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
        return datasf.Record(
            number=number, kind="alarm", received_at=when, dispatched_at=when, on_scene_at=None,
            cleared_at=None, streets=(), point=Point(lat, lon), units=("E31",), alarms=None,
        )  # fmt: skip

    # A point 1,030 m north of a call, which rounding moves 44 m towards it.
    north = at("1", 37.7804, -122.4760)
    south = at("2", 37.7804 - 1030 / 111_195, -122.4760)
    assert north.point is not None
    assert south.point is not None
    assert north.point.distance_m(south.point) > taps.CLEAR_M
    assert Point(37.780, -122.476).distance_m(south.point) < taps.CLEAR_M
    hours_on = datetime(2026, 3, 3, 23, 0, tzinfo=UTC)

    # While the call to the south is going, the northern point is not away from it.
    busy = taps._away_from_all([north, south], [north], south.dispatched_at, 0)
    quiet = taps._away_from_all([north, south], [north], hours_on, 0)

    assert busy is None
    assert quiet == Point(37.780, -122.476)


def test_an_incident_read_at_several_moments_of_a_day_is_counted_once(
    env: dict[str, str], site: DataSf
) -> None:
    _, reading, _ = scored(env, site)

    # The engine is read every six hours and looks back twelve: each incident is
    # in two readings or more.
    assert len(reading.incidents) == 5
    assert len({incident.id for incident in reading.incidents}) == 5
    rows = [row for incident in reading.incidents for row in incident.evidence_ids]
    assert len(rows) == len(set(rows)) == CALLS
    assert set(reading.kept_as.values()) == {incident.id for incident in reading.incidents}


# Replayed taps.


def test_taps_are_planned_from_the_days_calls_by_rule_and_none_at_a_medical_call(
    env: dict[str, str], site: DataSf
) -> None:
    layout, _, _ = scored(env, site)
    records = run.read_records(layout)

    planned = taps.plan(records, 300)

    assert planned == taps.plan(records, 300)
    # One for each call that is no medical one and has a point, three minutes
    # after its dispatch, rounded to about a block.
    assert [tap.near for tap in planned] == [
        "260620001",
        "260620003",
        "260620004",
        "260620005",
        None,
    ]
    # A tap is named by what it asks, so a plan of another size never gives one
    # tap's name, and so its claim and its answer, to another.
    assert len({tap.id for tap in planned}) == 5
    assert all(tap.id.startswith("replay-") and len(tap.id) == 23 for tap in planned)
    fewer = taps.plan(records, 2)
    assert [tap.near for tap in fewer] == ["260620001", "260620004"]
    assert [tap.id for tap in fewer] == [planned[0].id, planned[2].id]
    assert planned[0].at == datetime(2026, 3, 3, 16, 4, 30, tzinfo=UTC)
    assert (planned[0].point.lat, planned[0].point.lon) == (37.777, -122.423)
    # The fifth is away from every call: at the alarms' corner, hours after them,
    # and not at a corner by the collision that is going on then.
    assert (planned[4].point.lat, planned[4].point.lon) == (37.78, -122.476)
    assert planned[4].at == datetime(2026, 3, 3, 21, 3, tzinfo=UTC)
    assert taps.plan(records, 0) == []


def test_replayed_taps_are_investigated_once_and_counted_by_what_they_were_answered(
    env: dict[str, str], site: DataSf
) -> None:
    command(env, site)

    numbers, texts = numbers_of(env), texts_of(env)
    assert numbers["taps"] == 5
    # At the fire, at the first alarm, and at the second, which is inside the
    # first's incident. The tap at the call nobody heard has no incident to match.
    assert numbers["taps_near_matched"] == 3
    assert "Of 4 answered taps placed at a DataSF call's point" in texts["taps_near_matched"]
    assert (numbers["taps_unanswered"], numbers["taps_lost"]) == (0, 0)
    assert numbers["taps_away_unconfirmed"] == 1
    assert "Of 1 answered taps placed where DataSF lists" in texts["taps_away_unconfirmed"]
    assert "No person tapped, and none is stored as a tap." in texts["taps"]
    # Nothing in the run's evidence says a person tapped.
    layout = run.Layout.under(env, "day")
    store = EvidenceStore(layout.evidence, "mock")
    assert store.search(kinds=["tap"]) == []
    assert all(
        incident.taps == 0
        for incident in score.read_incidents(
            store, run.read_lists(layout)[1], run.read_records(layout)
        ).incidents
    )


def test_a_run_given_again_with_more_taps_investigates_only_the_new_ones(
    env: dict[str, str], site: DataSf
) -> None:
    command(env, site, "--taps", "2")
    layout = run.Layout.under(env, "day")
    planned = taps.plan(run.read_records(layout), 300)
    runs = Runs(layout.taps, "mock")
    first = {tap.id: made.started_at for tap, _, made in taps.kept(planned, runs) if made}
    runs.close()
    assert (numbers_of(env)["taps"], len(first)) == (2, 2)

    command(env, site)

    runs = Runs(layout.taps, "mock")
    kept = {tap.id: made.started_at for tap, _, made in taps.kept(planned, runs) if made}
    runs.close()
    # The two that were investigated keep the answers they got, under their names.
    assert numbers_of(env)["taps"] == len(kept) == 5
    assert {name: kept[name] for name in first} == first


def test_a_replay_that_ember_fails_on_stops_and_goes_on_with_the_taps_nobody_claimed(
    env: dict[str, str], site: DataSf
) -> None:
    layout, _, _ = scored(env, site)
    records = run.read_records(layout)
    planned = taps.plan(records, 300)
    store = EvidenceStore(layout.evidence, "mock")
    blocks = run.read_lists(layout)[1]
    runs = Runs(layout.taps, "mock")
    storage = Storage.under(Path(env["BEACON_DATA_DIR"]))
    asked = 0

    def failing(body: dict[str, Any]) -> Any:
        nonlocal asked
        asked += 1
        if asked == 3:
            raise RuntimeError("Ember is down")
        return stand_in_investigator(body)

    now = [datetime(2026, 3, 10, 20, 0, tzinfo=UTC)]

    def replay(responder: Any) -> taps.Replayed:
        ember = EmberClient.fake(storage, responder)
        return taps.replay(planned, ember, store, blocks, runs, wall_clock=lambda: now[0])

    first = replay(failing)

    # The run it failed in is kept, as cut short, and nothing more was asked.
    assert (first.made, first.stopped) == (1, "ember")
    assert asked == 3
    ((_, state, cut), *rest) = taps.kept(planned, runs)
    assert state == "made"
    assert cut is not None
    assert cut.cut_short == "EmberError"
    assert [state for _, state, _ in rest] == ["waiting"] * 4

    # Given again at once, and nine minutes on, the replay leaves Ember alone: no
    # tap is claimed and nothing is asked. The hold is kept in the file.
    for minutes in (0, 9):
        now[0] = datetime(2026, 3, 10, 20, minutes, tzinfo=UTC)
        held = replay(failing)
        assert (held.made, held.stopped, asked) == (0, "held", 3)
    assert [state for _, state, _ in taps.kept(planned, runs)][1:] == ["waiting"] * 4

    now[0] = datetime(2026, 3, 10, 20, 11, tzinfo=UTC)
    again = replay(stand_in_investigator)

    assert (again.made, again.stopped) == (4, None)
    assert [state for _, state, _ in taps.kept(planned, runs)] == ["made"] * 5
    # And a third replay finds every tap claimed, and asks nothing.
    idle = replay(failing)
    assert (idle.made, idle.stopped, asked) == (0, None, 3)
    # Every claim has one stamp, so the file's sweep of what is a day old, which
    # goes by the stamp of a new claim, deletes none however much later a run goes on.
    assert {found[0] for tap in planned if (found := runs.get(tap.id))} == {taps._CLAIMED_AT}
    runs.close()


def test_paid_answers_that_are_no_form_three_times_running_hold_the_replay(
    env: dict[str, str], site: DataSf
) -> None:
    layout, reading, matched = scored(env, site)
    planned = taps.plan(run.read_records(layout), 300)
    store = EvidenceStore(layout.evidence, "mock")
    blocks = run.read_lists(layout)[1]
    runs = Runs(layout.taps, "mock")
    storage = Storage.under(Path(env["BEACON_DATA_DIR"]))
    asked = 0

    def babbling(body: dict[str, Any]) -> Any:
        nonlocal asked
        asked += 1
        return fake_response(text="No form at all.")

    first = taps.replay(planned, EmberClient.fake(storage, babbling), store, blocks, runs)

    assert (first.made, first.stopped, asked) == (3, "unusable", 3)
    again = taps.replay(planned, EmberClient.fake(storage, babbling), store, blocks, runs)
    assert (again.made, again.stopped, asked) == (0, "held", 3)
    # An answer that was no form is kept as "unconfirmed", and is nobody's answer:
    # the last planned tap, away from every call, is not counted as answered so.
    standing = taps.kept(planned, runs)
    numbers = {
        number.key: number
        for number in results._tap_numbers(standing, reading, matched, live=False, generated=True)
    }
    assert (numbers["taps"].value, numbers["taps_unanswered"].value) == (0, 3)
    assert numbers["taps_away_unconfirmed"].value == 0
    assert "Of 0 answered taps" in numbers["taps_near_matched"].text
    runs.close()
    # A stand-in's answers are not called Ember's, and nothing is said to be paid.
    told = results._taps_stopped("unusable", live=False)
    assert "Answers of the stand-in for Ember's were not the form" in told
    assert "paid" not in told
    assert "each of them paid for" in results._taps_stopped("unusable", live=True)
    assert "the stand-in for Ember failed" in results._stopped("ember", live=False)


def test_a_tap_that_was_claimed_and_never_answered_is_not_investigated_again(
    env: dict[str, str], site: DataSf
) -> None:
    layout, _, _ = scored(env, site)
    planned = taps.plan(run.read_records(layout), 300)
    runs = Runs(layout.taps, "mock")
    # A process that died between the claim and the run.
    assert runs.claim(planned[1].id, datetime.now(UTC))
    store = EvidenceStore(layout.evidence, "mock")
    storage = Storage.under(Path(env["BEACON_DATA_DIR"]))

    made = taps.replay(
        planned,
        EmberClient.fake(storage, stand_in_investigator),
        store,
        run.read_lists(layout)[1],
        runs,
    )

    assert made.made == 4
    assert [state for _, state, _ in taps.kept(planned, runs)] == [
        "made",
        "claimed",
        "made",
        "made",
        "made",
    ]
    runs.close()


# Recorded radio.


def record(root: Path, number: int, at: datetime, seconds: float = 2.0) -> bytes:
    """Write one call where trunk-recorder would, on the system `sfd`: a tone, never
    a recording. Returns its audio as Ember is sent it."""
    start_ms = round(at.timestamp() * 1000)
    stem = f"1201-{start_ms // 1000}.{start_ms % 1000:03d}_851012500-call_{number}"
    day = root.joinpath("sfd", str(at.year), str(at.month), str(at.day))
    day.mkdir(parents=True, exist_ok=True)
    audio = pcm_to_wav(number.to_bytes(2, "little") * round(16_000 * seconds))
    (day / f"{stem}.wav").write_bytes(audio)
    (day / f"{stem}.json").write_text(
        json.dumps(
            {
                "talkgroup": 1201,
                "start_time": start_ms // 1000,
                "start_time_ms": start_ms,
                "call_length": round(seconds),
                "call_length_ms": round(seconds * 1000),
                "emergency": 0,
                "encrypted": 0,
                "srcList": [{"src": 4512345, "tag": "Jane Doe"}],
            }
        )
    )
    return plain_wav(audio)


def form(status: str, **fields: Any) -> dict[str, Any]:
    return {
        "clarity": "clear",
        "category": "fire",
        "status": status,
        "details": [],
        "units": ["E36"],
        "alarm": None,
        "street": None,
        "block": None,
        "cross_street": None,
    } | fields


RECORDED = ("--paid", "--system", "sfd", "--talkgroups", "1201", "--taps", "300")


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    """A directory of calls with no mark, which is what recordings of real radio
    are, and what is "said" in each. The fire of the invented day is dispatched on
    the radio twenty seconds before DataSF's record says it was."""
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    monkeypatch.setattr(Ffmpeg, "problem", lambda self: None)
    monkeypatch.setattr(run, "core_dump_problem", lambda: None)
    monkeypatch.setattr(run, "forbid_core_dumps", lambda: True)
    root = tmp_path / "recordings"
    said = {
        record(root, 1, datetime(2026, 3, 3, 16, 1, 10, tzinfo=UTC)): form(
            "dispatched", street="Hayes St", block=400, units=["E36", "T6"], alarm=2
        ),
        record(root, 2, datetime(2026, 3, 3, 16, 6, 0, tzinfo=UTC)): form("on_scene"),
        # The evening before the day, and the morning after it.
        record(root, 3, datetime(2026, 3, 3, 7, 0, 0, tzinfo=UTC)): form("working"),
        record(root, 4, datetime(2026, 3, 4, 11, 0, 0, tzinfo=UTC)): form("working"),
    }
    by_digest = {hashlib.sha256(audio).hexdigest(): heard for audio, heard in said.items()}
    return root, by_digest


def heard_by(by_digest: dict[str, Any]) -> Callable[[dict[str, Any]], Any]:
    return lambda body: fake_response(text=json.dumps(by_digest[_digest(body)]))


def test_a_run_over_recordings_is_the_one_whose_numbers_are_about_real_radio(
    env: dict[str, str], site: DataSf, recorder: tuple[Path, dict[str, Any]]
) -> None:
    root, by_digest = recorder
    layout = run.Layout.under(env, "day")
    meta = Meta(layout, heard_by(by_digest))
    allowed = env | {"BEACON_RECORDINGS_PERMITTED": "1"}

    status, said = command(
        allowed, site, *RECORDED, "--recordings", str(root), paid=meta.client(env)
    )

    assert status == 0
    made = results.read(layout)
    assert made is not None
    assert said.startswith(results.RECORDED_BY_EMBER)
    assert (made["basis_text"], made["stand_in"], made["audio"], made["hearer"]) == (
        results.RECORDED_BY_EMBER,
        False,
        "recorded",
        "live",
    )
    numbers, texts = numbers_of(env), texts_of(env)
    # Only the day's two calls were heard: not the evening before, nor the morning after.
    assert (numbers["transmissions"], numbers["radio_paid_calls"]) == (2, 2)
    assert len(meta.audio) == 2
    assert (numbers["incidents"], numbers["found"], numbers["missed"]) == (1, 1, 5)
    assert "not_generated" not in numbers
    # The radio was twenty seconds ahead of DataSF's dispatch time.
    assert numbers["heard_after_dispatch_s"] == -20.0
    assert "the radio's timing, not how long Beacon took" in texts["heard_after_dispatch_s"]
    # From the middle of the 400 block of Hayes to the corner at Gough: 67 metres.
    assert numbers["place_median_m"] == pytest.approx(66.9, abs=1.5)
    assert "the block Beacon heard" in texts["place_median_m"]
    assert numbers["radio_cost_usd"] == pytest.approx(2 * 0.00015)
    assert "scale:radio" in texts["radio_cost_usd"]
    assert "recorded radio" in texts["radio_cost_usd"]
    # The rows are live ones, in a live store of the run's own, and name nobody.
    store = EvidenceStore(layout.evidence, "live")
    rows = store.search(limit=10)
    assert {(row.origin, row.model, row.stand_in) for row in rows} == {("live", "live", False)}
    assert "Jane" not in layout.evidence.read_bytes().decode("latin-1")
    # Taps are planned from DataSF's calls, heard or not: the one at the fire is
    # matched, and each was investigated by the paid client and is no stand-in's.
    assert (numbers["taps"], numbers["taps_near_matched"]) == (5, 1)
    kept = Runs(layout.taps, "live").get(taps.plan(run.read_records(layout), 300)[0].id)
    assert kept is not None
    assert kept[1] is not None
    assert (kept[1].model, kept[1].stand_in) == ("live", False)
    assert numbers["tap_cost_usd"] > 0
    # Nothing in the operator's directory was changed, and nothing marked.
    assert not (root / GENERATED_MARK).exists()
    assert len(list(root.rglob("*.wav"))) == 4


def test_the_cost_of_recorded_radio_is_never_mixed_with_generated_speechs_and_they_share_a_cap(
    env: dict[str, str],
    site: DataSf,
    speech: None,
    recorder: tuple[Path, dict[str, Any]],
) -> None:
    root, by_digest = recorder
    # A paid run of generated speech first: twelve requests under its own scope.
    spoken = Meta(run.Layout.under(env, "speech"))
    assert command(env, site, *PAID, "--taps", "0", paid=spoken.client(env), name="speech")[0] == 0
    allowed = env | {"BEACON_RECORDINGS_PERMITTED": "1"}
    meta = Meta(run.Layout.under(env, "radio"), heard_by(by_digest))
    recorded = (*RECORDED[:-1], "0", "--recordings", str(root))

    # Then recordings, with a budget that the speech has already used up but for
    # less than one request: the two share it, so nothing is heard.
    spent = f"{CALLS * 0.00015 + 0.00005:.5f}"
    status, said = command(
        allowed, site, *recorded, "--radio-budget", spent, paid=meta.client(env), name="radio"
    )
    assert (status, meta.paid) == (1, 0)
    assert "The run stopped at its spending cap" in said
    status, said = command(
        allowed, site, *recorded, "--radio-budget", "0.0018", paid=meta.client(env), name="radio"
    )
    assert status == 2
    assert "Nothing is left of the budget for hearing: the scope scale:speech holds it" in said

    # With the whole budget, the recordings are heard, after the hold has run out.
    held = datetime.now(UTC) + timedelta(minutes=11)
    layout = run.Layout.under(env, "radio")
    settings = run.read_settings(layout)
    assert settings is not None
    store = run.open_store(layout, settings)
    ember = run.radio_ember(layout, settings, allowed, run.RADIO_BUDGET_NANOUSD, meta.client(env))
    heard = run.hear(
        layout, settings, store, ember, allowed, recordings=root, wall_clock=lambda: held
    )

    # What is reported for recorded radio is its own two requests, and not the
    # twelve that were paid for generated speech.
    assert heard.stopped is None
    assert heard.spend is not None
    assert (heard.spend.calls, heard.spend.spent_nanousd) == (2, 300_000)
    ledger = EmberClient.replay_only(Storage.under(Path(env["BEACON_DATA_DIR"])))
    assert ledger.scope_spend(run.SPEECH_SCOPE).calls == CALLS
    assert numbers_of(env, "speech")["radio_paid_calls"] == CALLS
    assert "scale:speech" in texts_of(env, "speech")["radio_cost_usd"]


def test_once_taps_are_replayed_nothing_more_is_heard_so_every_answer_is_of_one_evidence(
    env: dict[str, str], site: DataSf, recorder: tuple[Path, dict[str, Any]]
) -> None:
    root, by_digest = recorder
    layout = run.Layout.under(env, "day")
    meta = Meta(layout, heard_by(by_digest))
    allowed = env | {"BEACON_RECORDINGS_PERMITTED": "1"}
    recorded = (*RECORDED, "--recordings", str(root))
    assert command(allowed, site, *recorded, paid=meta.client(env))[0] == 0
    first = numbers_of(env)
    paid = meta.paid

    # A recording of the day turns up after the taps were answered.
    late = record(root, 5, datetime(2026, 3, 3, 21, 0, tzinfo=UTC))
    by_digest[hashlib.sha256(late).hexdigest()] = form(
        "dispatched", category="collision", street="Octavia St", block=300, units=["E5"]
    )
    status, _ = command(allowed, site, *recorded, paid=meta.client(env))

    # It is not heard: the taps' answers are kept for good, and are of the evidence
    # as it was. Nothing is paid, and the result says the hearing is closed.
    assert (status, meta.paid, len(meta.audio)) == (0, paid, 2)
    made = results.read(layout)
    assert made is not None
    assert results.CLOSED in made["caveats"]
    assert numbers_of(env) == first
    assert EvidenceStore(layout.evidence, "live").last_id() == 2

    # A run with no taps is never closed: it hears what turns up.
    other = Meta(run.Layout.under(env, "open"), heard_by(by_digest))
    untapped = (*RECORDED[:-1], "0", "--recordings", str(root))
    for audio in (3, 3):
        assert command(allowed, site, *untapped, paid=other.client(env), name="open")[0] == 0
        assert len(other.audio) == audio
    made = results.read(run.Layout.under(env, "open"))
    assert made is not None
    assert results.CLOSED not in made["caveats"]


def test_a_run_over_recordings_of_a_day_that_is_hardly_over_fetches_nothing(
    env: dict[str, str],
    site: DataSf,
    recorder: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HalfPastOne(datetime):
        """Half past one in the morning after the day, by San Francisco's clock."""

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 3, 4, 9, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(cli, "datetime", HalfPastOne)
    root, by_digest = recorder
    meta = Meta(run.Layout.under(env, "day"), heard_by(by_digest))
    allowed = env | {"BEACON_RECORDINGS_PERMITTED": "1"}

    status, said = command(
        allowed, site, *RECORDED, "--recordings", str(root), paid=meta.client(env)
    )

    # Refused before DataSF's record of the day is fetched and kept: a record
    # fetched this early would be kept for every later invocation of the run.
    assert status == 2
    assert "The day's last calls can still be recorded" in said
    assert site.requests == []
    assert not run.Layout.under(env, "day").root.exists()


def test_recordings_are_not_heard_without_the_operators_word_or_by_a_stand_in(
    env: dict[str, str], site: DataSf, recorder: tuple[Path, dict[str, Any]]
) -> None:
    root, by_digest = recorder
    layout = run.Layout.under(env, "day")
    meta = Meta(layout, heard_by(by_digest))

    status, said = command(env, site, *RECORDED, "--recordings", str(root), paid=meta.client(env))

    assert (status, meta.paid) == (2, 0)
    assert "set BEACON_RECORDINGS_PERMITTED=1" in said
    assert site.requests == []

    # A directory that is marked as generated is no recording, whatever is said of it.
    (root / GENERATED_MARK).write_text("Generated audio. No radio.")
    allowed = env | {"BEACON_RECORDINGS_PERMITTED": "1"}
    status, said = command(
        allowed, site, *RECORDED, "--recordings", str(root), paid=meta.client(env)
    )

    assert (status, meta.paid) == (2, 0)
    assert "The directory of calls is not what this run is of" in said
    assert EvidenceStore(layout.evidence, "live").last_id() == 0


def test_the_lines_for_a_terminal_put_what_the_run_is_before_any_number(
    env: dict[str, str], site: DataSf
) -> None:
    command(env, site)
    made = results.read(run.Layout.under(env, "day"))
    assert made is not None

    lines = results.lines(made)

    assert lines[2] == results.GENERATED_BY_STAND_IN
    first_number = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("DataSF calls Beacon could read")
    )
    assert first_number > 2
    # Every number is followed by its sentence, and one with no value says "none".
    assert "Cost of hearing: none" in lines
    assert lines[lines.index("Cost of hearing: none") + 1].strip().startswith("Not measured:")
    assert "Share of DataSF calls found: 66.7%" in lines


def test_results_of_another_shape_are_not_read(env: dict[str, str], site: DataSf) -> None:
    command(env, site)
    layout = run.Layout.under(env, "day")
    assert results.read(run.Layout.under(env, "another")) is None

    layout.results.write_text(json.dumps({"format": 0, "numbers": []}))

    with pytest.raises(run.ScaleError, match="not in a shape Beacon reads"):
        results.read(layout)
