"""The investigation desk, the routes that answer a tap, and the server around them."""

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import beacon.investigate.desk as desk_module
import beacon.main as main_module
from beacon import taps as beacon_taps
from beacon.api import taps as api_taps
from beacon.ember import (
    BudgetExceededError,
    EmberCallError,
    EmberClient,
    EmberClosedError,
    InputTooLargeError,
    LedgerReplacedError,
    Script,
    Storage,
    fake_response,
)
from beacon.evidence import EvidenceStore
from beacon.incident import NO_BLOCKS, Blocks, Engine, open_engine
from beacon.incident.build import CREDIT_511
from beacon.ingest import Clock, build
from beacon.ingest.paid import HELD_INTERVAL_SECONDS, UNUSABLE_RUN_LIMIT, read_hold
from beacon.investigate import (
    BUDGET_VARIABLE,
    KEPT,
    QUEUE,
    STALE,
    Cited,
    InvestigateConfigError,
    Investigation,
    InvestigationDesk,
    Outcome,
    Run,
    Runs,
    RunsError,
    open_investigations,
    stand_in_investigator,
)
from beacon.investigate import card as words
from beacon.main import create_app
from beacon.taps import TapMedia, Taps
from tests.incident_rows import HAYES, NOON, Rows, at, mock_store
from tests.test_incident_aircraft import offset
from tests.test_investigate import BLOCKS, NAMES, TAP, city, form
from tests.test_taps import PHONE

OTHER = "tap-AAAAAAAA0002"
THIRD = "tap-AAAAAAAA0003"
# Three kilometres north of the fire, where the story has nothing.
FAR = offset(HAYES, 3000, 0)
START = NOON - timedelta(minutes=20)


@pytest.fixture(autouse=True)
def a_host_that_hands_no_core_to_a_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beacon_taps, "core_dump_problem", lambda: None)


@dataclass
class World:
    """One evidence store with its engine and its taps, and the time, which a test
    moves."""

    path: Path
    rows: Rows
    now: list[datetime]
    engine: Engine
    taps: Taps
    storage: Storage

    def desk(self, responder: Any = stand_in_investigator, **more: Any) -> InvestigationDesk:
        more.setdefault("workers", 0)
        return InvestigationDesk(
            EmberClient.fake(self.storage, responder),
            Runs(self.path / "investigate" / "mock.sqlite3", "mock"),
            self.engine,
            self.taps,
            clock=lambda: self.now[0],
            wall_clock=lambda: self.now[0],
            **more,
        )

    def app(self, desk: InvestigationDesk | None, runs: Runs | None = None) -> TestClient:
        """A server over this world, with a desk, or with only the file of what
        was investigated before, or with neither."""
        app = create_app()
        app.state.taps, app.state.incidents = self.taps, self.engine
        if desk is not None:
            app.state.investigations, app.state.runs = desk, desk.runs
        elif runs is not None:
            app.state.runs = runs
        return TestClient(app)


@pytest.fixture
def world(tmp_path: Path) -> Any:
    rows = Rows(mock_store(tmp_path))
    now = [at(6.5)]
    listed = [(street, block, p.lat, p.lon) for (street, block), p in BLOCKS.points.items()]
    blocks = Blocks.of([*listed, ("Oak St", 100, FAR.lat, FAR.lon)])
    engine = Engine(rows.store, blocks, names=NAMES, clock=lambda: now[0])
    taps = Taps(rows.store, TapMedia(tmp_path / "taps", "mock"), clock=lambda: now[0])
    made = World(tmp_path, rows, now, engine, taps, Storage.under(tmp_path))
    city(rows)
    yield made
    taps.close()


def failing(monkeypatch: pytest.MonkeyPatch, desk: InvestigationDesk, failures: list[Any]) -> list:
    """Has the desk's Ember raise the errors of `failures`, in order, in place of
    answering, and returns the list of what it was asked."""
    ember = desk._ember
    respond = ember.respond
    asked: list[str] = []

    def responding(**request: Any) -> Any:
        asked.append(request["task"])
        failure = failures.pop(0) if failures else None
        if failure is not None:
            raise failure
        return respond(**request)

    monkeypatch.setattr(ember, "respond", responding)
    return asked


def fire_id(world: World) -> str:
    (fire,) = [i for i in world.engine.incidents() if i.category == "fire"]
    return fire.id


# The desk.


def test_a_tap_is_investigated_once_and_its_run_and_its_decision_are_kept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk()
    asked = failing(monkeypatch, desk, [])

    run = desk.investigate(TAP)

    assert run is not None
    assert (run.outcome.status, run.outcome.incident_id) == ("incident", fire_id(world))
    assert (run.model, run.stand_in, run.started_at) == ("fake", True, at(6.5))
    assert desk.runs.get(TAP) == (at(6.5), run)
    decision = desk.runs.crisis(fire_id(world))
    assert decision is not None
    assert (decision.stand_in, decision.model) == (True, "fake")
    # Asked for again, by this desk or the next: nothing is made, and nothing asked.
    made = len(asked)
    assert desk.investigate(TAP) is None
    assert world.desk().investigate(TAP) is None
    assert len(asked) == made == 3
    # What is no tap is not looked into, and leaves no claim behind.
    assert desk.investigate("tap-AAAAAAAA9999") is None
    assert desk.runs.get("tap-AAAAAAAA9999") is None


def test_after_ember_fails_the_desk_passes_taps_over_for_a_while_and_across_a_restart(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for minute, tap_id in ((6, TAP), (6.1, OTHER), (6.2, THIRD), (6.3, "tap-AAAAAAAA0004")):
        world.rows.tap(minute, ref=tap_id)
    desk = world.desk()
    asked = failing(monkeypatch, desk, [None, EmberCallError("The API failed.")])

    cut = desk.investigate(TAP)

    # The run that failed may have been paid for: it is kept, and not made again.
    assert cut is not None
    assert (cut.cut_short, cut.outcome.reason, len(cut.rounds)) == (
        "EmberCallError",
        "cut_short",
        1,
    )
    assert desk.investigate(TAP) is None
    # The next tap is passed over, and told so. Ember is asked nothing.
    world.now[0] += timedelta(seconds=HELD_INTERVAL_SECONDS - 1)
    assert desk.investigate(OTHER) is None
    _claimed, skipped = desk.runs.get(OTHER)  # type: ignore[misc]
    assert skipped.outcome == Outcome(status="unconfirmed", reason="not_investigated")
    assert len(asked) == 2
    # A restart does not shorten the hold.
    again = world.desk()
    assert again.investigate(THIRD) is None
    assert again.runs.get(THIRD)[1].outcome.reason == "not_investigated"  # type: ignore[index,union-attr]
    # Once it is over, a tap is looked into again; one passed over is not.
    world.now[0] += timedelta(seconds=2)
    fourth = again.investigate("tap-AAAAAAAA0004")
    assert fourth is not None
    assert fourth.outcome.status == "incident"
    assert again.investigate(OTHER) is None


def test_a_hold_is_never_made_shorter_by_a_run_that_fails_or_answers_badly_after_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tap_id in (TAP, OTHER, THIRD):
        world.rows.tap(6, ref=tap_id)
    desk = world.desk()
    investigate = desk_module.investigate_tap
    under_way, go = threading.Event(), threading.Event()
    hour = EmberCallError("The API failed.", status_code=429, retry_after=3600.0)

    def investigating(ember: EmberClient, scene: Any, **more: Any) -> Investigation:
        made = investigate(ember, scene, **more)
        if scene.tap_id == TAP:
            # This run is out while the other is told to stay away for an hour,
            # and then fails itself, with nothing said about how long.
            under_way.set()
            assert go.wait(5)
            return Investigation(run=made.run, failure=EmberCallError("The API failed."))
        return Investigation(run=made.run, failure=hour)

    monkeypatch.setattr(desk_module, "investigate_tap", investigating)
    first = threading.Thread(target=desk.investigate, args=(TAP,))
    first.start()
    try:
        assert under_way.wait(5)
        desk.investigate(OTHER)
        asked_for = read_hold(desk.runs.state())[0]
        assert asked_for == world.now[0] + timedelta(hours=1)
    finally:
        go.set()
        first.join(5)

    # Ten minutes from the later failure would end fifty minutes early.
    assert read_hold(desk.runs.state())[0] == asked_for
    # Nor does a run of answers that are no form, each of which holds for ten minutes.
    for _ in range(UNUSABLE_RUN_LIMIT):
        desk._paid.count_answer(desk._hold, usable=False)
    assert desk._hold.held_until == asked_for
    # And a restart keeps it: fifty-nine minutes on, a tap is still passed over.
    world.now[0] += timedelta(minutes=59)
    again = world.desk()
    assert again.investigate(THIRD) is None
    assert again.runs.get(THIRD)[1].outcome.reason == "not_investigated"  # type: ignore[index,union-attr]


def test_a_tap_whose_turn_comes_late_is_passed_over_and_never_claimed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6, ref=OTHER)
    desk = world.desk()
    asked = failing(monkeypatch, desk, [])
    claimed: list[str] = []
    claim = desk.runs.claim
    monkeypatch.setattr(
        desk.runs, "claim", lambda tap_id, now: claimed.append(tap_id) or claim(tap_id, now)
    )

    # Its turn comes a moment after it is too old: a queue that was slow, or a
    # server that was started again with the tap still in its store.
    world.now[0] = at(6) + STALE + timedelta(seconds=1)
    assert desk.investigate(TAP) is None

    assert (asked, claimed) == ([], [])
    assert desk.runs.get(TAP)[1].outcome.reason == "not_investigated"  # type: ignore[index,union-attr]
    # One that is just inside is looked into.
    world.rows.tap(6 + 1 / 30, ref=THIRD)
    assert desk.investigate(THIRD) is not None
    assert claimed == [THIRD]
    # A day on, the claims of that day are gone from the file, and a tap of that
    # day, whose row the store still has, is not claimed again and not paid for.
    world.now[0] = at(6) + KEPT + timedelta(minutes=30)
    desk.runs.sweep(world.now[0])
    assert (desk.runs.get(THIRD), desk.runs.get(OTHER)) == (None, None)
    made = len(asked)
    assert (desk.investigate(THIRD), desk.investigate(OTHER)) == (None, None)
    assert (len(asked), claimed) == (made, [THIRD])
    # Nothing is written for a tap that old: there is no answer of its to read.
    assert (desk.runs.get(THIRD), desk.runs.get(OTHER)) == (None, None)


def test_a_tap_that_waited_is_investigated_as_things_stood_when_it_came(world: World) -> None:
    world.rows.tap(6.5, FAR, ref=TAP)
    desk = world.desk()
    client = world.app(desk)
    # While it waits for its turn, a fire is dispatched to the block it came from.
    world.rows.radio(8.5, "fire", "dispatched", units=("E5",), street="Oak St", block=100)
    world.now[0] = at(11)

    run = desk.investigate(TAP)

    assert run is not None
    # Made at eleven minutes past, on the evidence as it stood at half past six.
    assert (run.started_at, run.as_of) == (at(11), at(6.5))
    assert run.outcome == Outcome(status="unconfirmed", reason="nothing_near")
    # What began since is listed by code, and said not to have been investigated.
    answer = client.get(f"/api/taps/{TAP}").json()
    assert (answer["status"], answer["nearby"]) == ("unconfirmed", [])
    assert [incident["title"] for incident in answer["since"]] == [
        "Fire at the 100 block of Oak St"
    ]
    assert answer["since_text"] == words.SINCE


def test_a_tap_ember_was_asked_nothing_about_is_said_not_to_have_been_investigated(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6.1, ref=OTHER)
    desk = world.desk()
    failing(monkeypatch, desk, [BudgetExceededError("The budget for investigate:tap is spent.")])

    run = desk.investigate(TAP)

    assert run is not None
    assert run.outcome == Outcome(status="unconfirmed", reason="not_investigated")
    assert (run.calls, run.cut_short) == ([], "BudgetExceededError")
    # It stays claimed, and the desk leaves Ember alone for a while.
    assert desk.investigate(TAP) is None
    assert desk.investigate(OTHER) is None
    assert desk.runs.get(OTHER)[1].outcome.reason == "not_investigated"  # type: ignore[index,union-attr]


def test_a_refusal_after_ember_was_asked_something_leaves_a_run_that_was_cut_short(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk()
    failing(monkeypatch, desk, [None, BudgetExceededError("The budget is spent.")])

    run = desk.investigate(TAP)

    assert run is not None
    assert (run.outcome.reason, len(run.calls)) == ("cut_short", 1)


def test_a_replaced_ledger_stops_the_desk_for_the_life_of_the_process(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6.1, ref=OTHER)
    desk = world.desk()
    asked = failing(monkeypatch, desk, [LedgerReplacedError("The ledger was replaced.")])

    run = desk.investigate(TAP)

    # A replaced ledger can follow a request that was sent: it is not "nothing asked".
    assert run is not None
    assert run.outcome.reason == "cut_short"
    assert "The investigator poller has stopped: the Ember ledger was replaced" in caplog.text
    world.now[0] += timedelta(days=2)
    assert desk.investigate(OTHER) is None
    assert len(asked) == 1


@pytest.mark.parametrize("failure", [EmberClosedError("Closed."), InputTooLargeError("Too large.")])
def test_a_closing_client_and_a_request_too_large_are_no_news_about_ember(
    world: World, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6.1, ref=OTHER)
    desk = world.desk()
    failing(monkeypatch, desk, [failure])

    desk.investigate(TAP)

    assert desk.runs.state()["held_until"] is None
    other = desk.investigate(OTHER)
    assert other is not None
    assert other.outcome.status == "incident"


def test_answers_that_are_no_form_are_paid_for_once_each_and_hold_the_desk(world: World) -> None:
    taps = [f"tap-AAAAAAAA10{n:02d}" for n in range(UNUSABLE_RUN_LIMIT + 1)]
    for tap_id in taps:
        world.rows.tap(6, ref=tap_id)
    script = Script([fake_response(text="It is a fire.")] * UNUSABLE_RUN_LIMIT)
    desk = world.desk(script)

    made = [desk.investigate(tap_id) for tap_id in taps]

    assert [run.outcome.reason for run in made[:-1]] == ["no_answer"] * UNUSABLE_RUN_LIMIT  # type: ignore[union-attr]
    assert made[-1] is None
    assert len(script.requests) == UNUSABLE_RUN_LIMIT
    assert desk.runs.get(taps[-1])[1].outcome.reason == "not_investigated"  # type: ignore[index,union-attr]


def test_a_run_that_ends_well_does_not_take_off_the_hold_another_run_put_on_meanwhile(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tap_id in (TAP, OTHER, THIRD):
        world.rows.tap(6, ref=tap_id)
    desk = world.desk()
    investigate = desk_module.investigate_tap
    under_way, go = threading.Event(), threading.Event()

    def investigating(ember: EmberClient, scene: Any, **more: Any) -> Investigation:
        if scene.tap_id == TAP:
            # The first run is out while the second fails.
            under_way.set()
            assert go.wait(5)
            return investigate(ember, scene, **more)
        made = investigate(ember, scene, **more)
        return Investigation(run=made.run, failure=EmberCallError("The API failed."))

    monkeypatch.setattr(desk_module, "investigate_tap", investigating)
    first = threading.Thread(target=desk.investigate, args=(TAP,))
    first.start()
    try:
        assert under_way.wait(5)
        desk.investigate(OTHER)
    finally:
        go.set()
        first.join(5)

    # The first run was finished, and the hold the second put on is still on, in
    # the desk and in what a restart reads.
    assert desk.runs.get(TAP)[1].outcome.status == "incident"  # type: ignore[index,union-attr]
    assert desk.runs.state()["held_until"] is not None
    assert desk.investigate(THIRD) is None
    assert desk.runs.get(THIRD)[1].outcome.reason == "not_investigated"  # type: ignore[index,union-attr]


def test_a_shutdown_waits_from_before_the_claim_until_the_run_is_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk()
    claiming, go = threading.Event(), threading.Event()
    claim = desk.runs.claim

    def slow_claim(*given: Any) -> bool:
        claiming.set()
        assert go.wait(5)
        return claim(*given)

    monkeypatch.setattr(desk.runs, "claim", slow_claim)
    made: list[Any] = []
    thread = threading.Thread(target=lambda: made.append(desk.investigate(TAP)))
    thread.start()
    try:
        assert claiming.wait(5)
        # The claim is about to be made. A shutdown that closed the file now would
        # leave a tap claimed that nobody looked into: `drain` says work is out.
        assert desk.drain(0.05) is False
    finally:
        go.set()
        thread.join(5)

    assert desk.drain(1) is True
    assert desk.runs.get(TAP) == (at(6.5), made[0])


def test_a_tap_that_comes_after_the_desk_is_closed_is_not_claimed_and_not_taken(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk()
    asked = failing(monkeypatch, desk, [])

    desk.close()

    assert desk.active is False
    assert desk.drain(0.1) is True
    assert desk.submit(TAP) is False
    assert desk.investigate(TAP) is None
    # Nothing was claimed and nothing asked, so the next run of Beacon can look.
    assert (desk.runs.get(TAP), asked) == (None, [])
    assert desk.stop(0.1) is True
    with pytest.raises(RunsError, match="closed"):
        desk.runs.save_state({})


def test_a_tap_that_finds_the_queue_full_is_passed_over_and_costs_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = world.desk()
    asked = failing(monkeypatch, desk, [])

    taken = [desk.submit(f"tap-AAAAAAAA2{n:03d}") for n in range(QUEUE + 2)]

    assert taken == [True] * QUEUE + [False, False]
    assert desk.runs.get("tap-AAAAAAAA2000") is None
    _claimed, skipped = desk.runs.get(f"tap-AAAAAAAA2{QUEUE:03d}")  # type: ignore[misc]
    assert skipped.outcome == Outcome(status="unconfirmed", reason="not_investigated")
    assert asked == []


def test_the_desks_threads_look_into_the_taps_handed_in_and_outlive_one_that_fails(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6, ref=OTHER)
    desk = world.desk(workers=1)
    investigate = desk.investigate

    def investigating(tap_id: str) -> Run | None:
        if tap_id == TAP:
            raise RuntimeError("tap-AAAAAAAA0001 at 37.7766")
        return investigate(tap_id)

    monkeypatch.setattr(desk, "investigate", investigating)

    assert (desk.submit(TAP), desk.submit(OTHER)) == (True, True)
    for _ in range(200):
        found = desk.runs.get(OTHER)
        if found is not None and found[1] is not None:
            break
        time.sleep(0.02)

    assert desk.runs.get(OTHER)[1].outcome.status == "incident"  # type: ignore[index,union-attr]
    assert "A tap could not be looked into (RuntimeError)." in caplog.text
    assert "37.77" not in caplog.text
    assert TAP not in caplog.text
    assert desk.stop(1) is True
    assert all(not thread.is_alive() for thread in desk._threads)


def test_a_live_desk_pays_only_under_its_own_scope(world: World) -> None:
    class Live:
        mode = "live"
        scope = None

    with pytest.raises(ValueError, match="investigate:tap"):
        InvestigationDesk(
            Live(),  # type: ignore[arg-type]
            Runs(world.path / "investigate" / "mock.sqlite3", "mock"),
            world.engine,
            world.taps,
            workers=0,
        )


# The desk the environment asks for.


def test_on_the_mock_store_a_stand_in_investigates_and_says_so(tmp_path: Path) -> None:
    env = {"BEACON_DATA_DIR": str(tmp_path)}
    store = mock_store(tmp_path)
    engine = open_engine(env, store)
    taps = Taps(store, TapMedia(tmp_path / "taps", "mock"))

    runs, desk = open_investigations(env, engine, taps)

    assert desk is not None
    try:
        assert (runs.mode, desk.runs, desk.active) == ("mock", runs, True)
        assert (tmp_path / "investigate" / "mock.sqlite3").exists()
    finally:
        desk.stop(1)
        taps.close()


def test_on_the_live_store_taps_are_investigated_only_under_a_budget_and_by_a_live_ember(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = EvidenceStore(tmp_path / "evidence" / "live.sqlite3", "live")
    engine = Engine(store, NO_BLOCKS)
    taps = Taps(store, TapMedia(tmp_path / "taps", "live"))
    env = {"BEACON_DATA_DIR": str(tmp_path)}

    try:
        with pytest.raises(InvestigateConfigError, match="in dollars"):
            open_investigations(env | {BUDGET_VARIABLE: "plenty"}, engine, taps)
        # A budget, and no live Ember to spend it: refused, and no file is left.
        with pytest.raises(InvestigateConfigError, match="live Ember"):
            open_investigations(env | {BUDGET_VARIABLE: "0.25"}, engine, taps)
        assert not (tmp_path / "investigate").exists()

        # With no budget nothing is investigated, and the file is opened all the
        # same: what was investigated and paid for before is still read.
        runs, desk = open_investigations(env, engine, taps)

        assert desk is None
        assert "No budget for investigations (BEACON_INVESTIGATE_BUDGET_USD)" in caplog.text
        assert (runs.mode, (tmp_path / "investigate" / "live.sqlite3").exists()) == ("live", True)
        runs.close()
    finally:
        taps.close()


# The routes.


def test_a_tap_on_the_mock_scenario_is_answered_with_the_fire_and_every_part_says_what_it_is(
    tmp_path: Path,
) -> None:
    moment = [NOON]
    env = {"BEACON_DATA_DIR": str(tmp_path)}
    ingest = build(env, Clock(lambda: moment[0]))
    engine = open_engine(env, ingest.store, clock=lambda: moment[0])
    taps = Taps(ingest.store, TapMedia(tmp_path / "taps", "mock"), clock=lambda: moment[0])
    desk = InvestigationDesk(
        EmberClient.fake(Storage.under(tmp_path), stand_in_investigator),
        Runs(tmp_path / "investigate" / "mock.sqlite3", "mock"),
        engine,
        taps,
        workers=0,
        clock=lambda: moment[0],
    )
    app = create_app()
    app.state.taps, app.state.incidents, app.state.investigations = taps, engine, desk
    app.state.runs = desk.runs
    client = TestClient(app)
    # Twenty minutes into the story: the fire is at its third alarm.
    ingest.catch_up()

    receipt = client.post("/api/taps", json={"location": PHONE}).json()
    waiting = client.get(f"/api/taps/{receipt['id']}").json()
    desk.investigate(receipt["id"])
    answer = client.get(f"/api/taps/{receipt['id']}").json()
    run = client.get(f"/api/taps/{receipt['id']}/run").json()
    listed = client.get("/api/incidents").json()["incidents"]

    assert receipt["investigating"] is True
    assert (waiting["status"], waiting["text"]) == ("pending", "Beacon is looking into this tap.")
    assert (waiting["incident"], waiting["match"], waiting["stand_in"]) == (None, None, False)
    # The answer: the fire, said to be the investigator's reading and a stand-in's.
    assert (answer["status"], answer["reason"], answer["mode"]) == ("incident", None, "mock")
    assert answer["text"] == words.MATCHED
    # Who investigated is said, and so is what the evidence is: two things.
    by_rule = "A stand-in for Muse Ember made this investigation, by rule. No model investigated."
    assert (answer["investigated_by"], answer["investigator_text"]) == ("stand_in", by_rule)
    assert (answer["model"], answer["stand_in"], answer["stand_in_text"]) == (
        "fake",
        True,
        f"{by_rule} {words.EVIDENCE_STAND_IN}",
    )
    assert (answer["cost_incomplete"], answer["cost_text"]) == (False, None)
    fire = answer["incident"]
    assert (fire["title"], fire["stand_in"]) == ("Fire at the 400 block of Hayes St", True)
    assert answer["incident_id"] == fire["id"]
    assert (answer["incident_gone"], answer["incident_gone_text"]) == (False, None)
    assert answer["match"] == {
        "confidence": "medium",
        "text": words.MATCHED,
        "grounds": [
            {
                "kind": "near",
                "text": "When Beacon looked, the incident was about 100 m from where the tap "
                "came from.",
                "basis": "nearby_incidents",
                "credit": None,
            },
            {
                "kind": "going_on",
                "text": "The incident was going on when the tap came, or had been shortly before.",
                "basis": "nearby_incidents",
                "credit": None,
            },
            {
                "kind": "aircraft_overhead",
                "text": "An aircraft that was over the incident was also within reach of where "
                "the tap came from, shortly before the tap.",
                "basis": "incident_aircraft",
                "credit": None,
            },
        ],
        "grounds_dropped": 0,
    }
    # Crisis mode: a decision, said to be the investigator's and a stand-in's, on
    # signals that are kept as they were then and shown as they are now.
    crisis = fire["crisis"]
    assert (crisis["active"], crisis["text"], crisis["model"], crisis["stand_in"]) == (
        True,
        words.CRISIS,
        "fake",
        True,
    )
    assert (crisis["decided_by"], crisis["decided_by_text"]) == ("stand_in", by_rule)
    assert crisis["stand_in_text"] == f"{by_rule} {words.EVIDENCE_STAND_IN}"
    kinds = ["alarm", "evacuation_on_radio", "official_notice", "official_alert"]
    assert [cited["kind"] for cited in crisis["cited"]] == kinds
    for cited in crisis["cited"]:
        assert cited["confidence"] in ("official", "high", "medium", "low")
        assert (bool(cited["evidence_ids"]), cited["now"]) == (True, "same")
    assert [signal["kind"] for signal in crisis["signals"]] == kinds
    assert crisis["signals_text"] == words.CRISIS_NOW
    for signal in crisis["signals"]:
        assert signal["confidence"] in ("official", "high", "medium", "low")
        assert signal["sources"]
        assert all(source["stand_in"] for source in signal["sources"])
    # The medical response a block away was found and not taken, and has no crisis mode.
    (medical,) = answer["nearby"]
    assert (medical["title"], medical["crisis"]) == (
        "Medical response at the 400 block of Fell St",
        None,
    )
    assert answer["nearby_text"] == words.NEARBY
    assert (answer["watching"], answer["since"], answer["since_text"]) == (False, [], None)
    assert (answer["ember_calls"], answer["cost_usd"]) == (3, 0.0)
    # The list of incidents carries the decision too, and names no tap.
    by_id = {incident["id"]: incident for incident in listed}
    assert by_id[fire["id"]]["crisis"]["active"] is True
    assert by_id[medical["id"]]["crisis"] is None
    assert receipt["id"] not in client.get("/api/incidents").text
    assert receipt["id"] not in client.get(f"/api/incidents/{fire['id']}").text

    # The run: each round, each call with why, what came back, and what it cost.
    assert (run["state"], run["mode"], run["model"], run["stand_in"]) == (
        "finished",
        "mock",
        "fake",
        True,
    )
    assert (run["investigated_by"], run["investigator_text"]) == ("stand_in", by_rule)
    assert run["stand_in_text"] == f"{by_rule} {words.EVIDENCE_STAND_IN}"
    assert run["evidence_as_of"] == receipt["received_at"]
    assert (run["unaccounted_calls"], run["cost_incomplete"], run["cost_text"]) == (0, False, None)
    assert [
        (round_["n"], [(call["tool"], call["why"], call["ok"]) for call in round_["calls"]])
        for round_ in run["rounds"]
    ] == [
        (1, [("nearby_incidents", "look_for_a_lead", True)]),
        (
            2,
            [
                ("get_official", "check_the_lead", True),
                ("get_aircraft", "check_the_lead", True),
                ("nearby_taps", "check_the_lead", True),
            ],
        ),
    ]
    first = run["rounds"][0]["calls"][0]
    assert (first["thinking"], first["thinking_text"], first["lead"]) == (
        "no_lead",
        "No lead yet.",
        None,
    )
    # Nearest first: the medical response is nearer, and the fire is the lead.
    assert [found["id"] for found in first["returned"]["incidents"]] == [
        medical["id"],
        fire["id"],
    ]
    assert first["returned_text"] == "Found 2 incidents."
    assert run["rounds"][1]["calls"][0]["lead"] == fire["id"]
    assert set(run["rounds"][0]["ember"]) == {
        "source",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cost_usd",
        "latency_ms",
    }
    assert run["final"]["source"] == "fake"
    assert run["outcome"] == {
        "status": "incident",
        "reason": None,
        "text": words.MATCHED,
        "incident_id": fire["id"],
        "grounds": ["near", "going_on", "aircraft_overhead"],
        "grounds_dropped": 0,
        "confidence": "medium",
        "crisis": True,
        "crisis_signals": kinds,
        "crisis_cited": [
            {key: cited[key] for key in ("kind", "confidence", "evidence_ids")}
            for cited in crisis["cited"]
        ],
        "crisis_refused": False,
    }
    assert (run["ember_calls"], run["cut_short"], run["cut_short_text"]) == (3, None, None)
    # Nothing of where the phone said it was is in either answer.
    for text in (client.get(f"/api/taps/{receipt['id']}").text, str(run)):
        assert "7763549" not in text
        assert "4244123" not in text
    desk.stop(1)
    taps.close()


def test_a_tap_with_nothing_near_is_unconfirmed_and_beacon_lists_what_begins_there_since(
    world: World,
) -> None:
    desk = world.desk()
    client = world.app(desk)
    where = {"lat": FAR.lat, "lon": FAR.lon}
    hayes = fire_id(world)

    tap_id = client.post("/api/taps", json={"location": where}).json()["id"]
    desk.investigate(tap_id)
    answer = client.get(f"/api/taps/{tap_id}").json()

    assert (answer["status"], answer["reason"]) == ("unconfirmed", "nothing_near")
    assert answer["text"] == (
        "Unconfirmed. Beacon's investigator looked and found no incident near where this tap "
        "came from."
    )
    assert (answer["incident"], answer["match"], answer["nearby"]) == (None, None, [])
    assert (answer["watching"], answer["since"], answer["since_text"]) == (True, [], None)
    # What the invented fire's evidence has no part in is still a stand-in's answer.
    assert (answer["stand_in"], answer["model"]) == (True, "fake")
    assert answer["investigated_by"] == "stand_in"

    # Something begins on that block two minutes later. Nobody investigates again.
    world.rows.radio(8.5, "fire", "dispatched", units=("E36",), street="Oak St", block=100)
    world.now[0] = at(9)
    answer = client.get(f"/api/taps/{tap_id}").json()
    assert (answer["status"], answer["watching"]) == ("unconfirmed", True)
    (since,) = answer["since"]
    assert since["title"] == "Fire at the 100 block of Oak St"
    assert answer["since_text"] == words.SINCE
    assert "Nobody has investigated" in answer["since_text"]
    # The fire on Hayes St began before the tap and is far from it: it is not listed.
    assert since["id"] != hayes

    # Half an hour after the tap, Beacon no longer lists anything for it.
    world.now[0] = at(6.5 + 31)
    answer = client.get(f"/api/taps/{tap_id}").json()
    assert (answer["watching"], answer["since"], answer["since_text"]) == (False, [], None)


def read(client: TestClient, tap_id: str) -> tuple[str, str | None, str]:
    answer = client.get(f"/api/taps/{tap_id}").json()
    return (
        answer["status"],
        answer["reason"],
        client.get(f"/api/taps/{tap_id}/run").json()["state"],
    )


def test_a_tap_is_pending_for_as_long_as_the_desk_has_it_however_long_that_is(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = world.desk(workers=1)
    investigate = desk_module.investigate_tap
    under_way, go = threading.Event(), threading.Event()

    def investigating(ember: EmberClient, scene: Any, **more: Any) -> Investigation:
        under_way.set()
        assert go.wait(5)
        return investigate(ember, scene, **more)

    monkeypatch.setattr(desk_module, "investigate_tap", investigating)
    client = world.app(desk)
    first = client.post("/api/taps", json={"location": PHONE}).json()["id"]
    try:
        assert under_way.wait(5)
        second = client.post("/api/taps", json={"location": PHONE}).json()["id"]
        # One is being looked into, and one waits for the desk's only thread.
        assert (desk.state_of(first), desk.state_of(second)) == ("running", "queued")
        assert read(client, first) == read(client, second) == ("pending", None, "pending")
        # A run can take longer than anybody guessed. Until the desk has done with
        # a tap its answer is still coming, and is not said to be lost or missing.
        world.now[0] += timedelta(hours=2)
        assert read(client, first) == read(client, second) == ("pending", None, "pending")
    finally:
        go.set()
    for _ in range(250):
        if desk.state_of(first) is None and desk.state_of(second) is None:
            break
        time.sleep(0.02)

    # The moment the desk is done with a tap, its run is there to read.
    assert read(client, first) == ("incident", None, "finished")
    # The second one's turn came two hours late: it was passed over.
    assert read(client, second) == ("unconfirmed", "not_investigated", "finished")
    assert desk.stop(1) is True


def test_a_run_that_was_begun_and_never_kept_is_said_to_be_lost_and_its_cost_unknown(
    world: World,
) -> None:
    desk = world.desk()
    client = world.app(desk)
    tap_id = client.post("/api/taps", json={"location": PHONE}).json()["id"]
    assert read(client, tap_id) == ("pending", None, "pending")

    # The server stops with the tap still waiting. Nothing was begun for it.
    desk.close()
    desk.drain(0.1)
    assert read(client, tap_id) == ("pending", None, "pending")
    assert desk.stop(0.1) is True
    assert read(client, tap_id) == ("unconfirmed", "not_investigated", "none")

    # A run was begun for it, and the server was stopped under that run: the next
    # server has the claim in the file and no run, and its desk is making none.
    Runs(world.path / "investigate" / "mock.sqlite3", "mock").claim(tap_id, world.now[0])
    for again in (world.app(world.desk()), world.app(None, runs=world.desk().runs)):
        assert read(again, tap_id) == ("unconfirmed", "cut_short", "lost")
        answer = again.get(f"/api/taps/{tap_id}").json()
        assert answer["text"] == (
            "Unconfirmed. The investigation was cut short before it had an answer."
        )
        assert (answer["model"], answer["investigated_at"], answer["ember_calls"]) == (
            None,
            None,
            0,
        )
        # It may have asked Ember something, which nothing here has the cost of.
        run = again.get(f"/api/taps/{tap_id}/run").json()
        for shown in (answer, run):
            assert (shown["cost_usd"], shown["cost_incomplete"]) == (0.0, True)
            assert shown["cost_text"] == words.COST_INCOMPLETE


def test_what_was_investigated_before_is_read_by_a_server_that_investigates_nothing(
    world: World,
) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk()
    desk.investigate(TAP)
    assert desk.stop(1) is True
    fire = fire_id(world)

    # A server with the file and no desk: no budget for investigations was set.
    client = world.app(None, runs=Runs(world.path / "investigate" / "mock.sqlite3", "mock"))

    answer = client.get(f"/api/taps/{TAP}").json()
    assert (answer["status"], answer["incident_id"]) == ("incident", fire)
    assert answer["incident"]["crisis"]["active"] is True
    assert client.get(f"/api/taps/{TAP}/run").json()["state"] == "finished"
    listed = client.get("/api/incidents").json()["incidents"]
    assert [i["crisis"]["active"] for i in listed if i["id"] == fire] == [True]
    assert client.get(f"/api/incidents/{fire}").json()["incident"]["crisis"]["active"] is True
    # A tap that comes now is taken, and is said to have no investigation.
    receipt = client.post("/api/taps", json={"location": PHONE}).json()
    assert receipt["investigating"] is False
    assert read(client, receipt["id"]) == ("unconfirmed", "not_investigated", "none")
    # A server with neither reads no decision, and says nothing was investigated.
    bare = world.app(None)
    assert bare.get(f"/api/incidents/{fire}").json()["incident"]["crisis"] is None
    assert read(bare, TAP) == ("unconfirmed", "not_investigated", "none")


def test_a_failed_call_that_may_have_been_paid_for_is_said_in_the_answer_and_the_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6, ref=OTHER)
    desk = world.desk()
    failing(monkeypatch, desk, [None, EmberCallError("The API failed.")])
    client = world.app(desk)

    desk.investigate(TAP)

    answer, run = (client.get(f"/api/taps/{TAP}{path}").json() for path in ("", "/run"))
    assert (answer["reason"], answer["ember_calls"]) == ("cut_short", 1)
    assert (run["ember_calls"], run["unaccounted_calls"], run["cut_short"]) == (
        1,
        1,
        "EmberCallError",
    )
    for shown in (answer, run):
        assert (shown["cost_incomplete"], shown["cost_text"]) == (True, words.COST_INCOMPLETE)
    # A call that was refused before it was sent leaves nothing unknown.
    world.now[0] += timedelta(seconds=HELD_INTERVAL_SECONDS + 1)
    failing(monkeypatch, desk, [None, BudgetExceededError("The budget is spent.")])
    desk.investigate(OTHER)
    run = client.get(f"/api/taps/{OTHER}/run").json()
    assert (run["ember_calls"], run["unaccounted_calls"], run["cost_incomplete"]) == (1, 0, False)


def test_the_distance_and_the_credit_of_a_match_are_the_runs_and_stay_when_the_incident_moves(
    world: World,
) -> None:
    # With no list of blocks the fire is where 511 says its closure is.
    engine = Engine(world.rows.store, NO_BLOCKS, names=NAMES, clock=lambda: world.now[0])
    world = replace(world, engine=engine)
    world.rows.tap(6, offset(HAYES, 100, 0), ref=TAP)
    desk = world.desk()
    client = world.app(desk)
    credit = {"text": CREDIT_511.text, "url": CREDIT_511.url}
    assert credit["url"] == "https://511.org"

    desk.investigate(TAP)
    answer, run = (client.get(f"/api/taps/{TAP}{path}").json() for path in ("", "/run"))

    near = answer["match"]["grounds"][0]
    assert (answer["incident"]["point_from"], near["kind"]) == ("closure", "near")
    was = "When Beacon looked, the incident was about 100 m from where the tap came from."
    # The distance is measured from a point of 511's, and says so.
    assert (near["text"], near["basis"], near["credit"]) == (was, "nearby_incidents", credit)
    assert all(ground["credit"] is None for ground in answer["match"]["grounds"][1:])
    calls = {call["tool"]: call for round_ in run["rounds"] for call in round_["calls"]}
    found = calls["nearby_incidents"]
    assert found["credits"] == [credit]
    assert [(i["distance_m"], i["point_from"]) for i in found["returned"]["incidents"]] == [
        (100, "closure")
    ]
    # A count of the closures 511 lists carries its credit too.
    assert (calls["get_official"]["returned"]["closures"], calls["get_official"]["credits"]) == (
        1,
        [credit],
    )
    assert calls["nearby_taps"]["credits"] == []

    # 511's next version of the closure is two kilometres on. The incident is
    # there now, and the match is still what the run found.
    world.rows.traffic(7, point=offset(HAYES, 2000, 0))
    world.now[0] = at(8)
    moved = client.get(f"/api/taps/{TAP}").json()
    assert moved["incident"]["point"] != answer["incident"]["point"]
    assert moved["match"]["grounds"][0] == near


def test_who_investigated_and_what_the_evidence_is_are_said_apart(world: World) -> None:
    fire = fire_id(world)
    client = world.app(world.desk())
    outcome = Outcome(
        status="incident", incident_id=fire, grounds=("near", "going_on"), confidence="medium"
    )
    # A run a model made on the invented scenario's rows, and one a stand-in made.
    for tap_id, model in ((TAP, "live"), (OTHER, "fake")):
        world.rows.tap(6, ref=tap_id)
        desk = world.desk()
        desk.runs.claim(tap_id, at(6.5))
        desk.runs.write(
            Run(
                tap_id=tap_id,
                started_at=at(6.5),
                finished_at=at(6.5),
                outcome=outcome,
                model=model,
                stand_in=True,
            )
        )
    by_model, by_rule = (client.get(f"/api/taps/{tap_id}").json() for tap_id in (TAP, OTHER))

    # The model is called a model, and the evidence is still said to be invented.
    assert (by_model["investigated_by"], by_model["investigator_text"]) == (
        "model",
        "Beacon's investigator is a model, Muse Ember. Its reading can be wrong.",
    )
    assert (by_model["stand_in"], by_model["stand_in_text"]) == (True, words.EVIDENCE_STAND_IN)
    assert "stand-in for Muse Ember" not in by_model["stand_in_text"]
    # The stand-in is not called a model anywhere in its answer.
    assert by_rule["investigated_by"] == "stand_in"
    assert "No model investigated." in by_rule["investigator_text"]
    assert by_rule["stand_in_text"] == f"{by_rule['investigator_text']} {words.EVIDENCE_STAND_IN}"
    for answer in (by_model, by_rule):
        assert "model" not in answer["text"] + answer["match"]["text"]


def test_an_answer_nobody_investigated_is_a_stand_ins_once_it_lists_a_stand_ins_incident(
    world: World,
) -> None:
    client = world.app(None)
    where = {"lat": FAR.lat, "lon": FAR.lon}
    tap_id = client.post("/api/taps", json={"location": where}).json()["id"]

    answer = client.get(f"/api/taps/{tap_id}").json()
    assert (answer["stand_in"], answer["stand_in_text"], answer["since"]) == (False, None, [])

    # An incident of the invented scenario's begins there, and is listed.
    world.rows.radio(8.5, "fire", "dispatched", units=("E36",), street="Oak St", block=100)
    world.now[0] = at(9)
    answer = client.get(f"/api/taps/{tap_id}").json()
    assert [incident["stand_in"] for incident in answer["since"]] == [True]
    assert (answer["investigated_by"], answer["model"]) == (None, None)
    assert (answer["stand_in"], answer["stand_in_text"]) == (True, words.EVIDENCE_STAND_IN)


def test_a_server_with_no_investigator_takes_the_tap_and_says_it_was_not_investigated(
    world: World,
) -> None:
    client = world.app(None)

    receipt = client.post("/api/taps", json={"location": PHONE}).json()
    answer = client.get(f"/api/taps/{receipt['id']}").json()

    assert receipt["investigating"] is False
    assert (answer["status"], answer["reason"]) == ("unconfirmed", "not_investigated")
    assert answer["text"] == "Unconfirmed. Beacon has no investigation of this tap."
    assert client.get(f"/api/taps/{receipt['id']}/run").json() == {
        "mode": "mock",
        "tap_id": receipt["id"],
        "state": "none",
        "started_at": None,
        "finished_at": None,
        "evidence_as_of": None,
        "model": None,
        "stand_in": False,
        "stand_in_text": None,
        "rounds": [],
        "final": None,
        "outcome": None,
        "cut_short": None,
        "cut_short_text": None,
        "ember_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "investigated_by": None,
        "investigator_text": None,
        "unaccounted_calls": 0,
        "cost_incomplete": False,
        "cost_text": None,
    }


def test_a_closing_desk_takes_no_tap_and_the_receipt_says_so(world: World) -> None:
    desk = world.desk()
    client = world.app(desk)
    desk.close()

    receipt = client.post("/api/taps", json={"location": PHONE}).json()

    assert receipt["investigating"] is False
    assert client.get(f"/api/taps/{receipt['id']}").json()["reason"] == "not_investigated"


@pytest.mark.parametrize("path", ["", "/run"])
def test_only_a_tap_beacon_has_and_still_answers_for_is_read(world: World, path: str) -> None:
    world.rows.tap(6, ref=TAP)
    # A row that is no tap, under a reference shaped like a tap's id.
    world.rows._add(
        kind="radio",
        source="app",
        ref="tap-AAAAAAAA7777",
        occurred_at=at(6),
        confidence="low",
        point=HAYES,
        data={},
    )
    desk = world.desk()
    desk.investigate(TAP)
    client = world.app(desk)

    assert client.get(f"/api/taps/{TAP}{path}").status_code == 200
    for tap_id in ("tap-AAAAAAAA9999", "tap-AAAAAAAA7777", "x", "a" * 41, "tap.AAAAAAAA"):
        response = client.get(f"/api/taps/{tap_id}{path}")
        assert (response.status_code, response.json()) == (404, {"detail": "There is no such tap."})
    # A day on, its run is no longer kept: it is a tap Beacon does not have, and not
    # one it is said not to have looked into.
    world.now[0] += api_taps.ANSWERED + timedelta(minutes=1)
    response = client.get(f"/api/taps/{TAP}{path}")
    assert (response.status_code, response.json()) == (404, {"detail": "There is no such tap."})


def test_an_incident_beacon_no_longer_has_is_said_to_be_gone_and_not_shown_as_another(
    world: World,
) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk()
    run = desk.investigate(TAP)
    assert run is not None
    # The run is as it was, and the incident's calls have since been regrouped:
    # what is kept names an incident this reading does not have.
    gone = Run(
        tap_id=OTHER,
        started_at=run.started_at,
        finished_at=run.finished_at,
        outcome=Outcome(
            status="incident",
            incident_id="inc-0000000000000000",
            grounds=("near", "going_on"),
            confidence="medium",
        ),
        model="fake",
        stand_in=True,
        as_of=run.as_of,
    )
    world.rows.tap(6, ref=OTHER)
    desk.runs.claim(OTHER, at(6.5))
    desk.runs.write(gone)

    answer = world.app(desk).get(f"/api/taps/{OTHER}").json()

    assert (answer["status"], answer["incident_id"]) == ("incident", "inc-0000000000000000")
    assert (answer["incident"], answer["incident_gone"]) == (None, True)
    assert answer["incident_gone_text"] == words.INCIDENT_GONE
    # The run that is kept found no incident by that name, so no distance is said.
    assert answer["match"]["grounds"][0] == {
        "kind": "near",
        "text": "When Beacon looked, the incident was within 500 m of where the tap came from.",
        "basis": "nearby_incidents",
        "credit": None,
    }


def test_crisis_mode_is_the_first_decision_that_turned_it_on_with_its_signals_then_and_now(
    world: World,
) -> None:
    world.rows.tap(6, ref=TAP)
    world.rows.tap(6, ref=OTHER)
    desk = world.desk()
    client = world.app(desk)
    fire = fire_id(world)

    def crisis() -> Any:
        return client.get(f"/api/incidents/{fire}").json()["incident"]["crisis"]

    # A run that took a tap to be about the fire and decided against crisis mode.
    against = Run(
        tap_id=OTHER,
        started_at=at(6.4),
        finished_at=at(6.4),
        outcome=Outcome(status="incident", incident_id=fire, grounds=("near",), confidence="low"),
        model="fake",
        stand_in=True,
    )
    desk.runs.claim(OTHER, at(6.4))
    desk.runs.write(against)
    assert crisis() is None

    # The next run decides for it, and that is the decision kept.
    desk.investigate(TAP)
    decided = crisis()
    assert (decided["active"], datetime.fromisoformat(decided["decided_at"])) == (True, at(6.5))
    then = {cited["kind"]: cited for cited in decided["cited"]}
    assert set(then) == {"alarm", "evacuation_on_radio", "official_notice", "official_alert"}
    assert {cited["now"] for cited in decided["cited"]} == {"same"}

    # A third alarm is called: the incident's alarm signal rests on another row now.
    third = world.rows.radio(7, "fire", "working", units=("E36",), alarm=3)
    world.now[0] = at(8)
    now = crisis()
    assert {cited["kind"]: cited["now"] for cited in now["cited"]}["alarm"] == "changed"
    # What the decision rested on is still shown as it was.
    assert [
        {key: cited[key] for key in ("kind", "confidence", "evidence_ids")}
        for cited in now["cited"]
    ] == [
        {key: cited[key] for key in ("kind", "confidence", "evidence_ids")}
        for cited in decided["cited"]
    ]
    assert third.id not in then["alarm"]["evidence_ids"]
    (alarm,) = [signal for signal in now["signals"] if signal["kind"] == "alarm"]
    assert third.id in [source["evidence_id"] for source in alarm["sources"]]
    assert now["signals_text"] == words.CRISIS_NOW

    # The alert is cancelled and the radio has it under control and then quiet.
    world.rows.alert(20, "shelter-cancel", message_type="Cancel", replaces=("shelter",))
    world.rows.radio(21, "fire", "cleared", units=("E36", "T6", "B2"))
    world.now[0] = at(22)
    over = crisis()

    assert client.get(f"/api/incidents/{fire}").json()["incident"]["open"] is False
    # It is not shown as on once the incident is over.
    assert over["active"] is False
    # The signal that is gone is said to be gone, and is not among the ones the
    # incident has now. The decision is still the one that was made.
    assert {cited["kind"]: cited["now"] for cited in over["cited"]}["official_alert"] == "gone"
    assert "official_alert" not in [signal["kind"] for signal in over["signals"]]
    assert over["decided_at"] == decided["decided_at"]


def test_a_medical_response_is_never_shown_in_crisis_mode_whatever_is_kept(world: World) -> None:
    world.rows.tap(6, ref=TAP)
    desk = world.desk(Script([form()]))
    (medical,) = [i for i in world.engine.incidents() if i.category == "medical response"]
    outcome = Outcome(
        status="incident",
        incident_id=medical.id,
        grounds=("near",),
        confidence="low",
        crisis=True,
        crisis_cited=(Cited("alarm", "medium", (1,)),),
    )
    desk.runs.claim(TAP, at(6.5))
    desk.runs.write(
        Run(
            tap_id=TAP,
            started_at=at(6.5),
            finished_at=at(6.5),
            outcome=outcome,
            model="fake",
            stand_in=True,
        )
    )

    shown = world.app(desk).get(f"/api/incidents/{medical.id}").json()["incident"]

    assert desk.runs.crisis(medical.id) is not None
    assert shown["crisis"] is None


# The server.


def test_the_server_investigates_a_tap_and_stops_cleanly() -> None:
    with TestClient(create_app()) as client:
        desk = client.app.state.investigations  # type: ignore[attr-defined]
        # The pollers play the story's first twenty minutes when they start.
        for _ in range(200):
            if any(
                i["category"] == "fire" for i in client.get("/api/incidents").json()["incidents"]
            ):
                break
            time.sleep(0.05)
        receipt = client.post("/api/taps", json={"location": PHONE}).json()
        answer: dict[str, Any] = {}
        for _ in range(200):
            answer = client.get(f"/api/taps/{receipt['id']}").json()
            if answer["status"] != "pending":
                break
            time.sleep(0.05)
        run = client.get(f"/api/taps/{receipt['id']}/run").json()

    assert receipt["investigating"] is True
    assert (answer["status"], answer["model"], answer["stand_in"]) == ("incident", "fake", True)
    assert answer["incident"]["category"] == "fire"
    assert (run["state"], run["outcome"]["incident_id"]) == ("finished", answer["incident_id"])
    # The server has shut down: the desk takes nothing more and writes nothing.
    assert desk.active is False
    assert desk.submit(receipt["id"]) is False
    with pytest.raises(RunsError, match="closed"):
        desk.runs.save_state({})
    assert all(not thread.is_alive() for thread in desk._threads)


def test_shutting_down_closes_the_investigator_before_it_waits_for_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app()
    seen: list[Any] = []

    async def sweeping(_taps: Any, stop: asyncio.Event) -> None:
        # The first thing a shutdown waits for, and the wait can be a long one.
        await stop.wait()
        desk = app.state.investigations
        seen.append((desk.active, desk.submit(TAP), desk.investigate(TAP)))

    monkeypatch.setattr(main_module, "_sweep_tap_media", sweeping)
    with TestClient(app):
        pass

    assert seen == [(False, False, None)]


def test_the_server_sweeps_the_investigations_whether_or_not_a_tap_comes(tmp_path: Path) -> None:
    runs = Runs(tmp_path / "investigate" / "mock.sqlite3", "mock")
    runs.claim(TAP, datetime.now(UTC) - KEPT - timedelta(minutes=1))
    runs.claim(OTHER, datetime.now(UTC) - KEPT + timedelta(hours=1))

    async def sweep_once() -> None:
        stop = asyncio.Event()
        sweeping = asyncio.create_task(main_module._sweep_runs(runs, stop))
        for _ in range(250):
            if runs.get(TAP) is None:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await sweeping

    asyncio.run(sweep_once())

    assert runs.get(TAP) is None
    assert runs.get(OTHER) is not None
    # A server with no investigator closes the file it only read, and a sweep
    # that finds it closed ends.
    asyncio.run(main_module._stop_investigations(None, runs, None))
    with pytest.raises(RunsError, match="closed"):
        runs.sweep(datetime.now(UTC))
    asyncio.run(asyncio.wait_for(main_module._sweep_runs(runs, asyncio.Event()), 5))


def test_the_pollers_are_closed_to_paid_work_when_the_investigator_could_not_be(
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed: list[str] = []

    class Desk:
        def close(self) -> None:
            raise RuntimeError("Jane Doe")

    class Stories:
        def close(self) -> None:
            closed.append("stories")

    class Pollers:
        def close_paid(self) -> None:
            closed.append("pollers")

    main_module._close_paid(Stories(), Desk(), Pollers())  # type: ignore[arg-type]

    assert closed == ["stories", "pollers"]
    assert "Paid work could not be closed (RuntimeError)." in caplog.text
    assert "Jane" not in caplog.text


def test_reading_a_taps_answer_is_as_private_as_sending_the_tap() -> None:
    assert main_module._is_private(f"/api/taps/{TAP}")
    assert main_module._is_private(f"/api/taps/{TAP}/run")
    assert main_module._is_private(f"/served/under/api/taps/{TAP}/run")
    assert not main_module._is_private("/api/incidents")


def test_what_was_there_before_the_investigator_looked_is_not_listed_as_begun_since(
    world: World,
) -> None:
    # An investigator that answers at once, having looked at nothing.
    desk = world.desk(Script([form()]))
    client = world.app(desk)

    tap_id = client.post("/api/taps", json={"location": PHONE}).json()["id"]
    desk.investigate(tap_id)
    world.now[0] = at(8)
    answer = client.get(f"/api/taps/{tap_id}").json()

    # The fire a hundred metres off began before the tap: it is not what began since.
    assert (answer["status"], answer["reason"]) == ("unconfirmed", "not_looked")
    assert (answer["watching"], answer["since"], answer["nearby"]) == (True, [], [])
