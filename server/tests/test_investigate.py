"""The investigator: what Ember may look at for a tap, how code checks what it
concludes, and what is kept of a run."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import beacon.investigate.tools as tools_module
from beacon.ember import EmberCallError, EmberClient, Script, Storage, fake_response
from beacon.evidence import Evidence, Point
from beacon.incident import Blocks, Engine, Incident
from beacon.investigate import (
    GROUNDS,
    KEPT,
    MAX_ROUNDS,
    TAP_BUDGET_USD,
    Outcome,
    Run,
    Runs,
    RunsError,
    Scene,
    investigate_tap,
)
from beacon.investigate import card as words
from beacon.investigate.loop import MAX_CALLS_PER_ROUND
from beacon.redact import Names
from tests.incident_rows import HAYES, NOON, Rows, at, mock_store
from tests.test_incident_aircraft import circle, offset

FELL = offset(HAYES, -100, 0)
BLOCKS = Blocks.of([("Hayes St", 400, HAYES.lat, HAYES.lon), ("Fell St", 400, FELL.lat, FELL.lon)])
NAMES = Names.of(streets=("Hayes St", "Fell St"), units=("E36", "T6", "B2", "M52"))
TAP = "tap-AAAAAAAA0001"
# What a call says with its arguments, when a test is not about that.
SAID = {"thinking": "an_incident", "lead": None, "why": "check_the_lead"}
NEAR = ("nearby_incidents", {"radius_m": 1000, "minutes": 30})
# A clip and a photo that could be found again if they were kept anywhere.
CLIP = b"RIFF a voice saying a name"
PHOTO = b"\xff\xd8 a face"


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


def city(rows: Rows) -> None:
    """A working fire on Hayes St with an alert, an order, and a closure, and a
    medical response a block south of it."""
    rows.radio(0, "fire", "dispatched", units=("E36", "T6"), street="Hayes St", block=400)
    rows.radio(1, "fire", "on_scene", units=("E36",), alarm=2)
    rows.radio(2, "fire", "working", units=("E36", "B2"), details=("evacuation",))
    rows.radio(2.5, "medical", units=("M52",), street="Fell St", block=400, details=("cpr",))
    rows.traffic(3)
    rows.alert(4, "shelter")
    rows.notice(5, "evacuation_order", instruction="Leave now.", summary="Leave Hayes St.")


def scene_of(rows: Rows, tap: Evidence, media: Any = None) -> Scene:
    """What an investigation of `tap` looks at, half a minute after it came."""
    moment = tap.occurred_at + timedelta(seconds=30)
    engine = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: moment)
    assert tap.point is not None
    return Scene(
        tap_id=tap.ref,
        at=tap.occurred_at,
        point=tap.point,
        has_photo=tap.data["photo"] is not None,
        has_clip=tap.data["clip"] is not None,
        snapshot=engine.snapshot(),
        store=rows.store,
        media=media or (lambda _tap_id, _kind: None),
    )


def of_category(scene: Scene, category: str) -> Incident:
    (incident,) = [i for i in scene.snapshot.incidents if i.category == category]
    return incident


def asks(*calls: tuple[str, dict[str, Any]], **tokens: int) -> dict[str, Any]:
    return fake_response(
        function_calls=[(name, SAID | arguments) for name, arguments in calls], **tokens
    )


def form(
    incident: str | None = None,
    grounds: tuple[str, ...] = (),
    crisis_signals: tuple[str, ...] | None = None,
    **more: Any,
) -> dict[str, Any]:
    answer = {
        "outcome": "unconfirmed" if incident is None else "incident",
        "incident": incident,
        "grounds": list(grounds),
        "crisis": crisis_signals is not None,
        "crisis_signals": list(crisis_signals or ()),
    }
    return fake_response(text=json.dumps(answer | more))


def told(request: dict[str, Any]) -> list[dict[str, Any]]:
    """What the tools of the earlier rounds told Ember, as a request has it."""
    return [
        json.loads(item["output"])
        for item in request["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def everything_on_disk(directory: Path) -> str:
    return "".join(
        path.read_bytes().decode("latin-1") for path in directory.rglob("*") if path.is_file()
    )


# What Ember may look at.


def test_ember_is_told_no_place_no_name_and_no_word_a_source_wrote(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    circle(rows, HAYES, 450, minutes=6, icao24="round1")
    rows.tap(5.5, offset(HAYES, 40, 40))
    tap = rows.tap(6, offset(HAYES, 30, 30), ref=TAP, clip=True, photo=True)
    scene = scene_of(rows, tap, media=lambda _id, kind: CLIP if kind == "clip" else PHOTO)
    fire = of_category(scene, "fire")
    reach = {"radius_m": 1000, "minutes": 30}
    script = Script(
        [
            asks(NEAR),
            asks(
                ("get_radio", {"minutes": 30}),
                ("get_aircraft", reach),
                ("get_official", {"incident": fire.id}),
                ("nearby_taps", reach),
            ),
            form(fire.id, ("near", "going_on")),
        ]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert made.run.outcome.status == "incident"
    sent = json.dumps(script.requests)
    # Not where the tap is, not where anything is, and no street, unit, or callsign.
    for private in ("37.7", "122.4", "Hayes", "Fell", "E36", "M52", "TEST1", "round1", TAP):
        assert private not in sent
    # Not what the alert, the closure, or the notice says, and not its address.
    for written in ("Headline", "Instruction", "Leave", "Activity", "notices.example"):
        assert written not in sent
    # What it is told of the official sources is their types and how they stand.
    (_, _, _, official, taps) = told(script.requests[2])
    assert official["official"] == [
        # An alert's type is the authority's own word for it, and is not told.
        {"kind": "alert", "type": None, "state": "in_force", "minutes_since_issued": 2},
        {
            "kind": "notice",
            "type": "evacuation_order",
            "state": "published",
            "minutes_since_issued": 2,
        },
    ]
    assert "Shelter" not in sent
    # The other tap is counted, and the one being looked into is not.
    assert taps["taps"] == 1


def test_a_medical_response_is_shown_as_its_category_and_its_distance_and_no_more(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    script = Script([asks(NEAR, ("get_radio", {"minutes": 30})), form()])

    investigate_tap(EmberClient.fake(storage, script), scene)

    nearby, radio = told(script.requests[1])
    fire, medical = nearby["incidents"]
    assert medical == {
        "id": of_category(scene, "medical response").id,
        "category": "medical response",
        "distance_m": 100,
        "going_on": True,
    }
    # The fire, which is not one, is shown with how it stands.
    assert (fire["distance_m"], fire["status"], fire["alarm"], fire["units"]) == (
        0,
        "working",
        2,
        3,
    )
    assert fire["signals"] == [signal.kind for signal in of_category(scene, "fire").signals]
    (call,) = [call for call in radio["calls"] if call["event"] == "medical response"]
    assert call == {"minutes_ago": 4, "incident": medical["id"], "event": "medical response"}
    assert "cpr" not in json.dumps(script.requests)


def test_a_call_runs_only_a_tool_that_is_offered_with_arguments_from_its_lists(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    fire = of_category(scene, "fire")
    said = fake_response(
        function_calls=[
            (NEAR[0], SAID | NEAR[1]),
            # A radius of its own, a truth value for a number, and a place.
            ("nearby_incidents", SAID | {"radius_m": 5000, "minutes": 30}),
            ("nearby_taps", SAID | {"radius_m": True, "minutes": 30}),
            ("get_aircraft", SAID | {"radius_m": 500, "minutes": 15, "lat": 37.7, "lon": -122.4}),
            # No such tool, and a tool this tap is not offered: it sent no clip.
            ("read_notice", SAID | {"url": "https://example.com/a.pdf"}),
            ("listen_clip", SAID),
        ]
    )
    # An incident that no tool of the run returned, and a lead that is no text.
    second = fake_response(
        function_calls=[
            ("get_official", SAID | {"incident": "inc-0000000000000000"}),
            ("get_official", {"thinking": "certain", "lead": ["x"], "why": 7, "incident": fire.id}),
        ]
    )
    script = Script([said, second, form()])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    first, then = made.run.rounds
    assert [(call.tool, call.ok, dict(call.args)) for call in first.calls] == [
        ("nearby_incidents", True, {"radius_m": 1000, "minutes": 30}),
        ("nearby_incidents", False, {}),
        ("nearby_taps", False, {}),
        # What is on the lists is used, and what is not is not read.
        ("get_aircraft", True, {"radius_m": 500, "minutes": 15}),
        ("unknown", False, {}),
        ("unknown", False, {}),
    ]
    assert [answer.get("error") for answer in told(script.requests[1])] == [
        None,
        "bad_arguments",
        "bad_arguments",
        None,
        "no_such_tool",
        "no_such_tool",
    ]
    assert [(call.ok, call.thinking, call.lead, call.why) for call in then.calls] == [
        (False, "an_incident", None, "check_the_lead"),
        (True, None, None, None),
    ]
    # The clip and the photo are not among the tools of a tap that sent none.
    offered = {tool["name"] for tool in script.requests[0]["tools"]}
    assert offered == set(tools_module.TOOL_NAMES) - {"listen_clip", "look_photo"}


def test_a_clip_and_a_photo_are_each_looked_at_once_in_a_sensitive_call_and_kept_as_tags(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    city(rows)
    asked: list[tuple[str, str]] = []

    def media(tap_id: str, kind: str) -> bytes:
        asked.append((tap_id, kind))
        return CLIP if kind == "clip" else PHOTO

    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True, photo=True), media)
    fire = of_category(scene, "fire")
    script = Script(
        [
            asks(NEAR, ("listen_clip", {}), ("look_photo", {})),
            # What Ember says of the clip: a tag, a word of its own, and a sentence.
            fake_response(text=json.dumps({"heard": ["siren", "gunshots", "A man shouts Jane"]})),
            fake_response(text="Smoke, and a woman at a window."),
            asks(("listen_clip", {}), ("look_photo", {})),
            form(fire.id, ("near", "heard_in_clip", "seen_in_photo")),
        ]
    )
    ember = EmberClient.fake(storage, script)

    made = investigate_tap(ember, scene)

    first, again = made.run.rounds
    assert [dict(call.returned) for call in first.calls[1:]] == [
        {"heard": ["siren"]},
        # An answer that is no form says nothing was made out.
        {"seen": ["none"]},
    ]
    assert [dict(call.returned) for call in again.calls] == [
        {"heard": ["siren"], "again": True},
        {"seen": ["none"], "again": True},
    ]
    # Read once each, and paid for once each: five requests, not seven.
    assert asked == [(TAP, "clip"), (TAP, "photo")]
    assert len(script.requests) == 5
    assert [call.ember is not None for call in first.calls + again.calls] == [
        False,
        True,
        True,
        False,
        False,
    ]
    # The siren holds as a ground, and the photo nothing was made out in does not.
    outcome = made.run.outcome
    assert (outcome.status, outcome.grounds) == ("incident", ("near", "heard_in_clip"))
    assert outcome.grounds_dropped == 1
    # Every call was sensitive: the ledger and the trace hold no request or answer.
    kept = everything_on_disk(tmp_path)
    for private in ("a voice", "a face", "gunshots", "Jane", "window"):
        assert private not in kept
        assert private not in repr(made.run)
    for request_text in ("You are the investigator", "nearby_incidents", "Say which of these"):
        assert request_text not in kept
    assert len(ember.ledger_calls([call.ledger for call in made.run.calls])) == 5


def test_a_clip_that_is_no_longer_kept_is_said_to_be_gone(rows: Rows, storage: Storage) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True))
    script = Script([asks(("listen_clip", {})), form()])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    (call,) = made.run.rounds[0].calls
    assert (call.ok, dict(call.returned), call.ember) == (False, {"error": "no_clip"}, None)
    assert words.returned_text(call) == "The tap's clip was no longer kept."


# The run, and what code makes of Ember's answer.


def test_a_match_stands_on_the_grounds_code_finds_to_hold(rows: Rows, storage: Storage) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 100, 0), ref=TAP))
    fire = of_category(scene, "fire")
    script = Script([asks(NEAR), form(fire.id, (*GROUNDS, "because I say so"))])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    outcome = made.run.outcome
    assert (outcome.status, outcome.incident_id) == ("incident", fire.id)
    # No clip, no photo, no aircraft, and no other tap: those four are dropped, and
    # so is the one that is not on the list.
    assert (outcome.grounds, outcome.grounds_dropped) == (("near", "going_on"), 5)
    assert outcome.confidence == "medium"
    assert (made.run.model, made.run.stand_in, made.usable) == ("fake", True, True)


@pytest.mark.parametrize(
    ("north_m", "grounds", "taps_before", "expected"),
    [
        # Near, and that alone is a match: trusted little without `going_on`.
        (100, ("near",), 0, ("incident", ("near",), "low")),
        # Close and going on is trusted as far as a tap ever is.
        (240, ("near", "going_on"), 0, ("incident", ("near", "going_on"), "medium")),
        (300, ("near", "going_on"), 0, ("incident", ("near", "going_on"), "low")),
        # Too far to be near: `near` is dropped, and one other ground is too thin.
        (700, ("near", "going_on"), 0, ("unconfirmed", (), None)),
        (700, ("going_on", "other_taps"), 0, ("unconfirmed", (), None)),
        # Two other grounds that hold carry a match that is not near.
        (700, ("going_on", "other_taps"), 1, ("incident", ("going_on", "other_taps"), "low")),
        # No grounds at all is a guess.
        (100, (), 0, ("unconfirmed", (), None)),
    ],
)
def test_thin_evidence_is_unconfirmed_and_a_match_is_never_trusted_more_than_medium(
    rows: Rows,
    storage: Storage,
    north_m: float,
    grounds: tuple[str, ...],
    taps_before: int,
    expected: tuple[str, tuple[str, ...], str | None],
) -> None:
    city(rows)
    where = offset(HAYES, north_m, 0)
    for _ in range(taps_before):
        rows.tap(5.5, where)
    scene = scene_of(rows, rows.tap(6, where, ref=TAP))
    fire = of_category(scene, "fire")
    script = Script(
        [
            asks(NEAR, ("nearby_taps", {"radius_m": 250, "minutes": 15})),
            form(fire.id, grounds, crisis_signals=("alarm",)),
        ]
    )

    outcome = investigate_tap(EmberClient.fake(storage, script), scene).run.outcome

    assert (outcome.status, outcome.grounds, outcome.confidence) == expected
    if outcome.status == "unconfirmed":
        # A match that did not stand names no incident and decides nothing.
        assert (outcome.reason, outcome.incident_id, outcome.crisis) == (
            "not_grounded",
            None,
            False,
        )


def test_an_aircraft_over_the_incident_is_a_ground_only_when_one_was_found(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 700, 0), ref=TAP))
    fire = of_category(scene, "fire")
    answers = [asks(NEAR), form(fire.id, ("going_on", "aircraft_overhead"))]

    without = investigate_tap(EmberClient.fake(storage, Script(answers)), scene).run.outcome
    circle(rows, HAYES, 450, minutes=6, icao24="round1")
    scene = scene_of(rows, rows.store.find("app", TAP))  # type: ignore[arg-type]
    with_one = investigate_tap(EmberClient.fake(storage, Script(answers)), scene).run.outcome

    assert (without.status, without.grounds_dropped) == ("unconfirmed", 1)
    assert (with_one.status, with_one.grounds) == ("incident", ("going_on", "aircraft_overhead"))
    # Twenty minutes after it was last seen, the same aircraft is no ground for a tap.
    late = scene_of(rows, rows.tap(27, offset(HAYES, 700, 0), ref="tap-AAAAAAAA0009"))
    assert of_category(late, "fire").aircraft
    later = investigate_tap(EmberClient.fake(storage, Script(answers)), late).run.outcome
    assert (later.status, later.grounds_dropped) == ("unconfirmed", 1)


def test_an_incident_no_tool_of_the_run_returned_cannot_be_the_answer(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    # Two kilometres off: the radio tool names the fire, and nothing says it is near.
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 2000, 0), ref=TAP))
    fire = of_category(scene, "fire")
    near = ("nearby_incidents", {"radius_m": 1000, "minutes": 30})
    script = Script(
        [asks(near, ("get_radio", {"minutes": 30})), form(fire.id, ("near", "going_on"))]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert fire.id in json.dumps(told(script.requests[1])[1])
    assert made.run.outcome == Outcome(status="unconfirmed", reason="not_grounded")
    # And an id Beacon never had is no answer either.
    script = Script([asks(near), form("inc-ffffffffffffffff", ("near",))])
    made = investigate_tap(EmberClient.fake(storage, script), scene)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="not_grounded")


@pytest.mark.parametrize(
    ("answer", "reason", "usable"),
    [
        (form(), "not_sure", True),
        (fake_response(text="It is the fire on Hayes St. Leave now."), "no_answer", False),
        (fake_response(text=json.dumps({"outcome": "certain"})), "no_answer", False),
        (fake_response(text=json.dumps(["incident"])), "no_answer", False),
    ],
)
def test_an_answer_that_names_nothing_or_is_no_form_is_unconfirmed(
    rows: Rows, storage: Storage, answer: dict[str, Any], reason: str, usable: bool
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))

    made = investigate_tap(EmberClient.fake(storage, Script([asks(NEAR), answer])), scene)

    assert made.run.outcome == Outcome(status="unconfirmed", reason=reason)
    assert (made.usable, made.failure) == (usable, None)
    assert "Hayes" not in repr(made.run)


def test_with_nothing_near_the_answer_is_unconfirmed_and_says_so(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 3000, 0), ref=TAP))

    made = investigate_tap(EmberClient.fake(storage, Script([asks(NEAR), form()])), scene)

    assert made.run.outcome == Outcome(status="unconfirmed", reason="nothing_near")
    assert words.status_text(made.run.outcome) == (
        "Unconfirmed. Beacon's investigator looked and found no incident near where this tap "
        "came from."
    )
    # An investigator that never asked what is near has not shown that nothing is:
    # here the fire is a hundred metres off.
    scene = scene_of(rows, rows.tap(6.1, offset(HAYES, 100, 0), ref="tap-AAAAAAAA0009"))
    for answers in ([form()], [asks(("get_radio", {"minutes": 30})), form()]):
        made = investigate_tap(EmberClient.fake(storage, Script(answers)), scene)
        assert made.run.outcome == Outcome(status="unconfirmed", reason="not_looked")
    assert words.status_text(made.run.outcome) == (
        "Unconfirmed. Beacon's investigator answered without asking what is near where this "
        "tap came from."
    )


@pytest.mark.parametrize(
    ("category", "cited", "stands", "refused"),
    [
        # Signals the engine found for the incident, in the engine's order.
        ("fire", ("official_alert", "alarm"), ("alarm", "official_alert"), False),
        # One it did not find: the whole decision falls.
        ("fire", ("alarm", "aircraft_circling"), (), True),
        ("fire", ("alarm", "it is very bad"), (), True),
        # A decision that rests on nothing.
        ("fire", (), (), True),
        # A medical response is never one, whatever is cited.
        ("medical response", ("alarm",), (), True),
        ("medical response", (), (), True),
    ],
)
def test_crisis_mode_stands_only_on_signals_the_engine_found_and_never_for_a_medical_response(
    rows: Rows,
    storage: Storage,
    category: str,
    cited: tuple[str, ...],
    stands: tuple[str, ...],
    refused: bool,
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    incident = of_category(scene, category)
    script = Script([asks(NEAR), form(incident.id, ("near", "going_on"), crisis_signals=cited)])

    outcome = investigate_tap(EmberClient.fake(storage, script), scene).run.outcome

    # The match stands either way: only the decision on crisis mode is refused.
    assert (outcome.status, outcome.incident_id) == ("incident", incident.id)
    assert (outcome.crisis, outcome.crisis_signals, outcome.crisis_refused) == (
        bool(stands),
        stands,
        refused,
    )


def test_no_word_ember_writes_is_read_kept_or_shown(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    fire = of_category(scene, "fire")
    note = "Evacuate now, Jane Doe says"
    script = Script(
        [
            asks(("nearby_incidents", NEAR[1] | {"note": note, "thinking": note, "why": note})),
            form(fire.id, ("near", note), crisis_signals=("alarm",), advice=note, summary=note),
        ]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)
    runs = Runs(tmp_path / "investigate" / "mock.sqlite3", "mock")
    runs.claim(TAP, at(6))
    runs.write(made.run)

    assert made.run.outcome.status == "incident"
    (call,) = made.run.rounds[0].calls
    assert (call.thinking, call.why, dict(call.args)) == (None, None, NEAR[1])
    shown = [
        words.status_text(made.run.outcome),
        words.call_text(call),
        words.returned_text(call),
        *(words.ground_text(ground, made.run, 0.0) for ground in made.run.outcome.grounds),
    ]
    for text in (repr(made.run), everything_on_disk(tmp_path), *shown):
        assert "Evacuate" not in text
        assert "Jane" not in text
    assert runs.get(TAP) == (at(6), made.run)


def test_a_run_has_four_rounds_of_calls_and_then_a_request_that_can_only_answer(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    fire = of_category(scene, "fire")
    # An investigator that never stops calling tools, and answers when it must.
    script = Script([asks(NEAR)] * MAX_ROUNDS + [form(fire.id, ("near",))])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert [round_.n for round_ in made.run.rounds] == [1, 2, 3, 4]
    assert ["tools" in request for request in script.requests] == [True] * 4 + [False]
    assert (made.run.outcome.status, made.run.final is not None) == ("incident", True)
    assert len(made.run.calls) == 5

    # One that calls a tool on the last request as well gets nothing run for it.
    script = Script([asks(NEAR)] * (MAX_ROUNDS + 1))
    made = investigate_tap(EmberClient.fake(storage, script), scene)
    assert len(script.requests) == MAX_ROUNDS + 1
    assert len(made.run.rounds) == MAX_ROUNDS
    assert made.run.outcome == Outcome(status="unconfirmed", reason="no_answer")


def test_only_so_many_calls_of_one_round_are_run_and_every_one_is_answered(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    script = Script([asks(*[NEAR] * (MAX_CALLS_PER_ROUND + 2)), form()])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert len(made.run.rounds[0].calls) == MAX_CALLS_PER_ROUND
    answers = told(script.requests[1])
    assert len(answers) == MAX_CALLS_PER_ROUND + 2
    assert answers[-2:] == [{"error": "too_many_calls"}] * 2


def test_a_tap_that_has_cost_its_most_is_cut_short_before_the_next_call_to_ember(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True, photo=True), lambda *_: CLIP)
    # A tenth of a dollar per million tokens of input: this request alone is the cap.
    dear = {"input_tokens": 100_000}
    script = Script([asks(NEAR, **dear), form()])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert made.run.cost_usd == pytest.approx(TAP_BUDGET_USD)
    assert (made.run.cut_short, len(script.requests)) == ("budget", 1)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="cut_short")
    assert words.cut_short_text(made.run) == (
        "The investigation reached the most one tap may cost, and stopped."
    )
    # The round that was paid for is kept, with what it cost.
    assert [(round_.ember.cost_usd, round_.calls) for round_ in made.run.rounds] == [
        (pytest.approx(0.01), ())
    ]

    # A tool's own call counts: the clip took the tap to its cap, so the photo is
    # not looked at.
    script = Script(
        [
            asks(("listen_clip", {}), ("look_photo", {})),
            fake_response(text=json.dumps({"heard": ["siren"]}), **dear),
        ]
    )
    made = investigate_tap(EmberClient.fake(storage, script), scene)
    assert (made.run.cut_short, len(script.requests)) == ("budget", 2)
    assert [call.tool for call in made.run.rounds[0].calls] == ["listen_clip"]


def test_a_run_ember_fails_on_ends_unconfirmed_with_what_was_paid_for_and_the_error_beside_it(
    rows: Rows, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True), lambda *_: CLIP)
    ember = EmberClient.fake(storage, Script([asks(NEAR), asks(("listen_clip", {}))]))
    respond = ember.respond
    asked: list[str] = []

    def failing(**request: Any) -> Any:
        asked.append(request["task"])
        if len(asked) == 3:
            raise EmberCallError("The API failed.", status_code=500)
        return respond(**request)

    monkeypatch.setattr(ember, "respond", failing)

    made = investigate_tap(ember, scene)

    assert asked == ["investigate_tap", "investigate_tap", "listen_clip"]
    assert isinstance(made.failure, EmberCallError)
    assert made.run.cut_short == "EmberCallError"
    assert made.run.outcome == Outcome(status="unconfirmed", reason="cut_short")
    # Both rounds that were paid for are kept; the call that failed has no row.
    assert [len(round_.calls) for round_ in made.run.rounds] == [1, 0]
    assert (made.run.final, len(made.run.calls)) == (None, 2)
    assert words.cut_short_text(made.run) == (
        "Muse Ember refused a call or failed on one, and the investigation stopped."
    )


def test_a_tool_of_beacons_own_that_fails_ends_the_run_and_keeps_nothing_of_the_error(
    rows: Rows, storage: Storage
) -> None:
    city(rows)

    def media(_tap_id: str, _kind: str) -> bytes:
        raise OSError("/data/taps/mock/tap-AAAAAAAA0001.clip.wav")

    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True), media)
    script = Script([asks(NEAR, ("listen_clip", {})), form()])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert (made.run.cut_short, made.failure) == ("tool_failed", None)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="cut_short")
    assert [call.tool for call in made.run.rounds[0].calls] == ["nearby_incidents"]
    assert "clip.wav" not in repr(made)


# The words.


def test_every_sentence_about_a_run_is_written_by_code_from_its_counts_and_tags(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    circle(rows, HAYES, 450, minutes=6, icao24="round1")
    rows.tap(5.5)
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 120, 0), ref=TAP, clip=True), lambda *_: CLIP)
    fire = of_category(scene, "fire")
    reach = {"radius_m": 500, "minutes": 15}
    script = Script(
        [
            fake_response(
                function_calls=[
                    (
                        "nearby_incidents",
                        {"thinking": "no_lead", "lead": None, "why": "look_for_a_lead"} | reach,
                    )
                ]
            ),
            fake_response(
                function_calls=[
                    (
                        name,
                        {"thinking": "an_incident", "lead": fire.id, "why": "check_the_lead"}
                        | args,
                    )
                    for name, args in (
                        ("get_official", {"incident": fire.id}),
                        ("get_aircraft", reach),
                        ("nearby_taps", reach),
                        ("get_radio", {"minutes": 15}),
                        ("listen_clip", {}),
                        ("look_photo", {}),
                    )
                ]
            ),
            fake_response(text=json.dumps({"heard": ["helicopter", "siren"]})),
            form(fire.id, GROUNDS, crisis_signals=("alarm",)),
        ]
    )

    run = investigate_tap(EmberClient.fake(storage, script), scene).run

    calls = [call for round_ in run.rounds for call in round_.calls]
    assert [
        (words.thinking_text(call), words.call_text(call), words.returned_text(call))
        for call in calls
    ] == [
        (
            "No lead yet.",
            "Asked which incidents are within 500 m, going on or with a call in the last "
            "15 min, to look for a lead.",
            "Found 2 incidents.",
        ),
        (
            "Thinks it is an incident.",
            "Asked for the official alerts, notices, and closures of its lead, to check the lead.",
            "Found 2 official alerts or notices, 1 of them an alert in force, and 1 road "
            "closure in force.",
        ),
        (
            "Thinks it is an incident.",
            "Asked for aircraft within 500 m in the last 15 min, to check the lead.",
            "Found 1 aircraft: 1 circling, 1 low.",
        ),
        (
            "Thinks it is an incident.",
            "Asked how many other taps came from within 500 m in the last 15 min, to check "
            "the lead.",
            "Found 1 other tap.",
        ),
        (
            "Thinks it is an incident.",
            "Asked for the radio calls of the last 15 min, to check the lead.",
            "Found 4 radio calls, of 2 incidents.",
        ),
        (
            "Thinks it is an incident.",
            "Had the tap's clip listened to, to check the lead.",
            "The investigator made out in the clip: a siren, a helicopter.",
        ),
        # A photo was asked for, and this tap sent none: no such tool was offered.
        (
            "Thinks it is an incident.",
            "Made a call, to check the lead.",
            "Beacon ran nothing: the call named no tool it offers.",
        ),
    ]
    assert run.outcome.grounds == (
        "near",
        "going_on",
        "heard_in_clip",
        "aircraft_overhead",
        "other_taps",
    )
    assert [words.ground_text(ground, run, 120.0) for ground in run.outcome.grounds] == [
        "The incident is about 100 m from where the tap came from.",
        "The incident was going on when the tap came, or had been shortly before.",
        "In the clip sent with the tap, the investigator made out: a siren, a helicopter.",
        "An aircraft was over the incident shortly before the tap, within reach of where the "
        "tap came from.",
        "1 other tap came from nearby. Beacon cannot tell whether taps are from different people.",
    ]
    # With the incident gone, the distance is no longer said as if it were known.
    assert words.ground_text("near", run, None) == (
        "The incident was within 500 m of where the tap came from."
    )
    # No sentence tells a reader what to do.
    for text in (words.MATCHED, words.CRISIS, words.STAND_IN, words.SINCE, words.NEARBY):
        lowered = text.lower()
        assert not any(word in lowered.split() for word in ("stay", "go", "leave", "evacuate"))


# Where runs are kept.


def run_of(tap_id: str = TAP, *, minute: float = 6, **changes: Any) -> Run:
    run = Run(
        tap_id=tap_id,
        started_at=at(minute),
        finished_at=at(minute + 0.1),
        outcome=Outcome(status="unconfirmed", reason="nothing_near"),
        model="fake",
        stand_in=True,
        as_of=at(minute),
    )
    return replace(run, **changes)


def crisis_run(tap_id: str, incident_id: str, minute: float, signals: tuple[str, ...]) -> Run:
    outcome = Outcome(
        status="incident",
        incident_id=incident_id,
        grounds=("near",),
        confidence="low",
        crisis=True,
        crisis_signals=signals,
    )
    return run_of(tap_id, minute=minute, outcome=outcome)


def runs_file(tmp_path: Path, mode: str = "mock") -> Runs:
    return Runs(tmp_path / "investigate" / f"{mode}.sqlite3", mode)  # type: ignore[arg-type]


def test_a_tap_is_claimed_once_and_its_run_is_written_once_under_the_claim(
    tmp_path: Path,
) -> None:
    runs = runs_file(tmp_path)

    assert runs.get(TAP) is None
    with pytest.raises(RunsError, match="only under a claim"):
        runs.write(run_of())
    assert runs.claim(TAP, at(6)) is True
    # Claimed, and not yet made: another process is told the tap is taken.
    assert runs_file(tmp_path).claim(TAP, at(7)) is False
    assert runs.get(TAP) == (at(6), None)
    runs.write(run_of(), state={"held_until": None, "unusable_run": 1})
    with pytest.raises(RunsError, match="once"):
        runs.write(run_of(cut_short="budget"))

    assert runs.get(TAP) == (at(6), run_of())
    assert runs.state() == {"held_until": None, "unusable_run": 1}
    assert runs.claim(TAP, at(8)) is False


def test_a_tap_that_was_passed_over_is_not_claimed_later_and_a_claimed_one_is_not_passed_over(
    tmp_path: Path,
) -> None:
    runs = runs_file(tmp_path)
    runs.skip(TAP, at(6))
    runs.claim("tap-AAAAAAAA0002", at(6))
    runs.skip("tap-AAAAAAAA0002", at(7))

    assert runs.claim(TAP, at(7)) is False
    _claimed, skipped = runs.get(TAP)  # type: ignore[misc]
    assert skipped is not None
    assert skipped.outcome == Outcome(status="unconfirmed", reason="not_investigated")
    assert (skipped.model, skipped.calls, skipped.stand_in) == (None, [], False)
    # The claim stands: its run is still to be written.
    assert runs.get("tap-AAAAAAAA0002") == (at(6), None)


def test_the_first_decision_on_crisis_mode_for_an_incident_is_the_one_kept_without_its_tap(
    tmp_path: Path,
) -> None:
    runs = runs_file(tmp_path)
    for tap_id, minute, signals in (
        ("tap-AAAAAAAA0001", 6, ("alarm", "official_alert")),
        ("tap-AAAAAAAA0002", 9, ("closure",)),
    ):
        runs.claim(tap_id, at(minute))
        runs.write(crisis_run(tap_id, "inc-1", minute, signals))

    decision = runs.crisis("inc-1")

    assert decision is not None
    assert (decision.decided_at, decision.signals) == (at(6.1), ("alarm", "official_alert"))
    assert (decision.model, decision.stand_in) == ("fake", True)
    assert runs.crisis("inc-2") is None
    with closing(sqlite3.connect(tmp_path / "investigate" / "mock.sqlite3")) as db:
        (kept,) = db.execute("SELECT incident_id, decided_at, decision FROM crisis").fetchall()
    assert "tap-" not in json.dumps(kept)


def test_the_live_file_takes_nothing_a_stand_in_had_a_part_in(tmp_path: Path) -> None:
    runs = runs_file(tmp_path, "live")
    runs.claim(TAP, at(6))

    with pytest.raises(RunsError, match="nothing a stand-in made"):
        runs.write(crisis_run(TAP, "inc-1", 6, ("alarm",)))
    assert (runs.get(TAP), runs.crisis("inc-1")) == ((at(6), None), None)
    runs.write(run_of(model="live", stand_in=False))

    assert runs.get(TAP)[1].model == "live"  # type: ignore[index,union-attr]


def test_a_file_of_the_other_mode_or_of_an_unknown_shape_is_refused_and_left_as_it_was(
    tmp_path: Path,
) -> None:
    path = tmp_path / "investigate" / "mock.sqlite3"
    runs = runs_file(tmp_path)
    runs.claim(TAP, at(6))
    runs.write(run_of())
    before = path.read_bytes()

    with pytest.raises(RunsError, match="begun as the mock one, and cannot be the live one"):
        Runs(path, "live")
    assert path.read_bytes() == before

    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version = 99")
    newer = path.read_bytes()
    with pytest.raises(RunsError, match="Do not delete the file or move it away") as refusal:
        Runs(path, "mock")
    assert path.read_bytes() == newer
    assert "paid for again" in str(refusal.value)


def test_a_file_of_an_earlier_shape_keeps_its_claims_and_loses_its_runs(tmp_path: Path) -> None:
    path = tmp_path / "investigate" / "mock.sqlite3"
    runs = runs_file(tmp_path)
    runs.claim(TAP, at(6))
    runs.write(crisis_run(TAP, "inc-1", 6, ("alarm",)))
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version = 0")

    again = Runs(path, "mock")

    # What was paid for is not paid for again, and what cannot be read is not read.
    assert again.get(TAP) == (at(6), None)
    assert again.claim(TAP, at(7)) is False
    assert again.crisis("inc-1") is None


def test_a_closed_file_takes_nothing_more_and_is_still_read(tmp_path: Path) -> None:
    runs = runs_file(tmp_path)
    runs.claim(TAP, at(6))
    runs.close()

    for write in (
        lambda: runs.claim("tap-AAAAAAAA0002", at(7)),
        lambda: runs.write(run_of()),
        lambda: runs.skip("tap-AAAAAAAA0002", at(7)),
        lambda: runs.save_state({}),
    ):
        with pytest.raises(RunsError, match="closed"):
            write()
    assert runs.get(TAP) == (at(6), None)
    assert runs.get("tap-AAAAAAAA0002") is None


def test_runs_and_decisions_older_than_a_day_are_deleted_when_a_tap_is_claimed(
    tmp_path: Path,
) -> None:
    runs = runs_file(tmp_path)
    runs.claim(TAP, at(0))
    runs.write(crisis_run(TAP, "inc-1", 0, ("alarm",)))
    day = KEPT.total_seconds() / 60

    runs.claim("tap-AAAAAAAA0002", at(day - 1))
    assert (runs.get(TAP) is not None, runs.crisis("inc-1") is not None) == (True, True)
    runs.claim("tap-AAAAAAAA0003", at(day + 1))
    assert (runs.get(TAP), runs.crisis("inc-1")) == (None, None)
    assert runs.get("tap-AAAAAAAA0002") is not None


def test_what_is_kept_of_a_run_holds_nothing_the_tap_sent_and_nothing_a_source_said(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    city(rows)
    where = Point(37.776_9, -122.423_9)
    tap = rows.tap(6, where, ref=TAP, clip=True, photo=True)
    scene = scene_of(rows, tap, lambda _id, kind: CLIP if kind == "clip" else PHOTO)
    fire = of_category(scene, "fire")
    reach = {"radius_m": 1000, "minutes": 30}
    script = Script(
        [
            asks(
                NEAR,
                ("get_radio", {"minutes": 30}),
                ("get_aircraft", reach),
                ("nearby_taps", reach),
                ("listen_clip", {}),
                ("look_photo", {}),
            ),
            fake_response(text=json.dumps({"heard": ["siren"]})),
            fake_response(text=json.dumps({"seen": ["smoke", "fire_apparatus"]})),
            asks(("get_official", {"incident": fire.id})),
            form(fire.id, GROUNDS, crisis_signals=("alarm", "official_alert")),
        ]
    )
    run = investigate_tap(EmberClient.fake(storage, script), scene).run
    runs = runs_file(tmp_path)
    runs.claim(TAP, at(6))

    runs.write(run)

    assert run.outcome.crisis
    assert runs.get(TAP) == (at(6), run)
    kept = "".join(
        path.read_bytes().decode("latin-1")
        for directory in (tmp_path / "investigate", tmp_path / "ember")
        for path in directory.rglob("*")
        if path.is_file()
    )
    for private in (
        # Where the tap came from, and where anything is.
        "37.77",
        "122.42",
        # The clip and the photo.
        "a voice",
        "a face",
        # What sources said: streets, units, an alert's and a notice's words.
        "Hayes",
        "Fell",
        "E36",
        "Headline",
        "Instruction",
        "Leave",
        "cpr",
    ):
        assert private not in kept
        assert private not in repr(run)
    # What it does hold: ids, counts, tags, and the accounting of each call.
    assert fire.id in kept
    assert "fire_apparatus" in kept


def test_the_time_of_a_run_is_the_clocks_it_was_given(rows: Rows, storage: Storage) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    moments = iter([at(6.5), at(6.6)])

    run = investigate_tap(
        EmberClient.fake(storage, Script([form()])), scene, clock=lambda: next(moments)
    ).run

    assert (run.started_at, run.finished_at, run.as_of) == (at(6.5), at(6.6), at(6.5))
    assert isinstance(run.started_at, datetime)
    assert run.started_at > NOON


def test_a_medical_response_is_not_put_in_crisis_mode_even_with_a_signal_of_its_own(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    fire, medical = of_category(scene, "fire"), of_category(scene, "medical response")
    # The engine gives a medical response no signals. Should one ever have one, the
    # investigator still cannot rest crisis mode on it.
    marked = replace(medical, signals=fire.signals[:1])
    others = [i for i in scene.snapshot.incidents if i.id != medical.id]
    scene = replace(scene, snapshot=replace(scene.snapshot, incidents=(marked, *others)))
    cited = (fire.signals[0].kind,)
    script = Script([asks(NEAR), form(medical.id, ("near",), crisis_signals=cited)])

    outcome = investigate_tap(EmberClient.fake(storage, script), scene).run.outcome

    assert (outcome.status, outcome.incident_id) == ("incident", medical.id)
    assert (outcome.crisis, outcome.crisis_signals, outcome.crisis_refused) == (False, (), True)


def test_a_round_whose_tool_took_the_tap_to_its_cap_is_the_last(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True), lambda *_: CLIP)
    script = Script(
        [
            asks(("listen_clip", {})),
            fake_response(text=json.dumps({"heard": ["siren"]}), input_tokens=100_000),
            form(),
        ]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    # The clip was listened to and is kept; no request follows it.
    assert (made.run.cut_short, made.failure, len(script.requests)) == ("budget", None, 2)
    assert [dict(call.returned) for call in made.run.rounds[0].calls] == [{"heard": ["siren"]}]
    assert made.run.final is None


def test_the_official_sources_of_an_incident_no_tool_returned_are_not_handed_out(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    # Two kilometres off: the fire is in the reading, and not near this tap.
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 2000, 0), ref=TAP))
    fire = of_category(scene, "fire")
    script = Script([asks(NEAR), asks(("get_official", {"incident": fire.id})), form()])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    (call,) = made.run.rounds[1].calls
    assert (call.ok, dict(call.returned)) == (False, {"error": "bad_arguments"})
    assert told(script.requests[2])[-1] == {"error": "bad_arguments"}
    assert "official_alert" not in json.dumps(script.requests[2]["input"])
