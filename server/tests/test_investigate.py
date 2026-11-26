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
from beacon.ember import (
    CallLimitError,
    EmberCallError,
    EmberClient,
    Script,
    SpendScope,
    Storage,
    fake_response,
)
from beacon.evidence import Evidence, Point
from beacon.incident import Blocks, Engine, Incident
from beacon.investigate import (
    GROUNDS,
    KEPT,
    MAX_ROUNDS,
    SCOPE,
    TAP_BUDGET_NANOUSD,
    TAP_BUDGET_USD,
    Cited,
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
from tests.test_ember_client import MetaApi
from tests.test_incident_aircraft import circle, offset

FELL = offset(HAYES, -100, 0)
BLOCKS = Blocks.of([("Hayes St", 400, HAYES.lat, HAYES.lon), ("Fell St", 400, FELL.lat, FELL.lon)])
NAMES = Names.of(streets=("Hayes St", "Fell St"), units=("E36", "T6", "B2", "M52", "E5"))
TAP = "tap-AAAAAAAA0001"
# What a call says with its arguments, when a test is not about that.
SAID = {"thinking": "an_incident", "lead": None, "why": "check_the_lead"}
NEAR = ("nearby_incidents", {"radius_m": 1000, "minutes": 30})
# A clip and a photo that could be found again if they were kept anywhere.
CLIP = b"RIFF a voice saying a name"
PHOTO = b"\xff\xd8 a face"


# A form that is whole and says the tap is unconfirmed.
WHOLE: dict[str, Any] = {
    "outcome": "unconfirmed",
    "incident": None,
    "grounds": [],
    "crisis": False,
    "crisis_signals": [],
}


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
    """What an investigation of `tap` looks at: the evidence as it stood when the
    tap came, as the desk has it read, though its turn came five minutes later."""
    moment = tap.occurred_at + timedelta(minutes=5)
    engine = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: moment)
    assert tap.point is not None
    return Scene(
        tap_id=tap.ref,
        at=tap.occurred_at,
        point=tap.point,
        has_photo=tap.data["photo"] is not None,
        has_clip=tap.data["clip"] is not None,
        snapshot=engine.at(tap.occurred_at),
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
            "minutes_since_issued": 1,
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
    medical_id = of_category(scene, "medical response").id
    script = Script(
        [
            asks(("get_radio", {"minutes": 60})),
            asks(NEAR),
            asks(("get_radio", {"minutes": 60}), ("get_official", {"incident": medical_id})),
            form(),
        ]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    before, nearby = told(script.requests[2])
    fire, medical = nearby["incidents"]
    # A handle, its category, and its distance. Not whether it is going on, and
    # not when anything was said of it.
    assert medical == {"id": medical_id, "category": "medical response", "distance_m": 100}
    # The fire, which is not one, is shown with how it stands.
    assert (fire["distance_m"], fire["going_on"], fire["status"], fire["alarm"]) == (
        0,
        True,
        "working",
        2,
    )
    assert fire["signals"] == [signal.kind for signal in of_category(scene, "fire").signals]
    # The radio tool shows the calls of the incidents the run has found. Asked
    # before any was found, it shows none.
    assert before == {"calls": [], "more": False, "of_incidents_found_so_far": 0}
    *_, radio, official = told(script.requests[3])
    # Three calls of the fire. Not the medical call: not its time, and not its id.
    assert [(call["incident"], call["event"]) for call in radio["calls"]] == [
        (fire["id"], "fire")
    ] * 3
    assert medical_id not in json.dumps(radio)
    assert official == {"incident": medical_id, "official": [], "signals": []}
    assert "cpr" not in json.dumps(script.requests)
    kept = made.run.rounds[2].calls[0].returned
    assert (kept["calls"], kept["incidents"]) == (3, [fire["id"]])


def test_the_radio_shown_is_of_the_incidents_near_the_tap_and_of_no_other(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    # Forty-five calls of an incident with no known place, newer than the fire's.
    for n in range(45):
        rows.radio(5 + n / 100, "rescue", "working", units=("E5",))
    # Near the fire, and two kilometres from it.
    for north, expected in ((100, 3), (2000, 0)):
        scene = scene_of(rows, rows.tap(6, offset(HAYES, north, 0)))
        fire = of_category(scene, "fire")
        script = Script([asks(NEAR), asks(("get_radio", {"minutes": 60})), form()])

        investigate_tap(EmberClient.fake(storage, script), scene)

        radio = told(script.requests[2])[-1]
        # What is elsewhere neither reaches Ember nor crowds out what is near.
        assert len(radio["calls"]) == expected
        assert {call["incident"] for call in radio["calls"]} <= {fire.id}
        assert "rescue" not in json.dumps(radio)


def test_only_a_closure_the_incidents_own_signal_rests_on_is_counted_as_one(
    rows: Rows, storage: Storage
) -> None:
    rows.radio(0, "fire", "working", units=("E36",), street="Hayes St", block=400)
    # 511 lists roadwork on the street, which closes nothing, and an event that
    # closed the street and whose version in force is about another road by now.
    rows.traffic(1, "511.org/works", closed=False)
    rows.traffic(2, "511.org/moved")
    rows.traffic(3, "511.org/moved", road="Oak St", point=offset(HAYES, 3000, 0))
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    fire = of_category(scene, "fire")
    assert len([entry for entry in fire.timeline if entry.kind == "closure"]) == 3
    assert "closure" not in [signal.kind for signal in fire.signals]
    answers = [asks(NEAR), asks(("get_official", {"incident": fire.id})), form()]
    script = Script(answers)

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert told(script.requests[2])[-1]["road_closures_in_force"] == 0
    (call,) = made.run.rounds[1].calls
    assert call.returned["closures"] == 0
    assert words.returned_text(call) == (
        "Found 0 official alerts or notices, 0 of them an alert in force. 0 511 events "
        "in force list a road as closed on a street of the incident's or near it."
    )

    # One that does close the street is counted, once.
    rows.traffic(4, "511.org/shut")
    scene = scene_of(rows, rows.tap(6.5, ref="tap-AAAAAAAA0009"))
    script = Script(answers)
    made = investigate_tap(EmberClient.fake(storage, script), scene)
    assert told(script.requests[2])[-1]["road_closures_in_force"] == 1
    assert words.returned_text(made.run.rounds[1].calls[0]) == (
        "Found 0 official alerts or notices, 0 of them an alert in force. 1 511 event "
        "in force lists a road as closed on a street of the incident's or near it."
    )


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
        # That it is going on and that others tapped are true of the incident and
        # of the neighbourhood, whoever tapped: they tie this tap to nothing.
        (700, ("going_on", "other_taps"), 1, ("unconfirmed", (), None)),
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
    # Two kilometres off: the fire is in the reading, and nothing says it is near.
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 2000, 0), ref=TAP))
    fire = of_category(scene, "fire")
    script = Script([asks(NEAR), form(fire.id, ("near", "going_on"))])

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    assert made.run.outcome == Outcome(status="unconfirmed", reason="not_grounded")
    # And an id Beacon never had is no answer either.
    script = Script([asks(NEAR), form("inc-ffffffffffffffff", ("near",))])
    made = investigate_tap(EmberClient.fake(storage, script), scene)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="not_grounded")


def test_what_began_after_the_tap_is_no_part_of_its_run_however_long_it_waited(
    rows: Rows, storage: Storage
) -> None:
    tap = rows.tap(6, ref=TAP)
    # Two minutes after the tap a fire is dispatched to the same block, and the
    # tap's turn comes three minutes after that.
    rows.radio(8, "fire", "working", units=("E36",), street="Hayes St", block=400, alarm=2)
    rows.tap(8.5)
    circle(rows, HAYES, 450, minutes=4, icao24="round1")
    now = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: at(11)).snapshot()
    (later,) = now.incidents
    scene = scene_of(rows, tap)
    reach = {"radius_m": 1000, "minutes": 60}
    script = Script(
        [
            asks(NEAR),
            asks(("get_radio", {"minutes": 60}), ("nearby_taps", reach)),
            form(later.id, ("near", "going_on", "other_taps")),
        ]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    # The run reads the evidence as it stood when the tap came: no fire yet.
    assert (scene.snapshot.as_of, scene.snapshot.incidents) == (at(6), ())
    assert made.run.as_of == at(6)
    nearby, radio, taps = told(script.requests[2])
    assert (nearby["incidents"], radio["calls"], taps["taps"]) == ([], [], 0)
    assert later.id not in json.dumps(script.requests)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="not_grounded")


def test_a_sound_or_a_sight_is_a_ground_only_for_an_incident_it_fits(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    # Six hundred metres from both: not near either.
    tap = rows.tap(6, offset(HAYES, 600, 0), ref=TAP, clip=True, photo=True)
    scene = scene_of(rows, tap, lambda _id, kind: CLIP if kind == "clip" else PHOTO)
    fire, medical = of_category(scene, "fire"), of_category(scene, "medical response")

    def answered(
        incident: str,
        seen: list[str],
        heard: list[str],
        grounds: tuple[str, ...] = ("going_on", "heard_in_clip", "seen_in_photo"),
    ) -> Outcome:
        script = Script(
            [
                asks(NEAR, ("listen_clip", {}), ("look_photo", {})),
                fake_response(text=json.dumps({"heard": heard})),
                fake_response(text=json.dumps({"seen": seen})),
                form(incident, grounds),
            ]
        )
        return investigate_tap(EmberClient.fake(storage, script), scene).run.outcome

    # Smoke and flames are evidence of a fire, and none of a medical response.
    assert answered(fire.id, ["smoke", "flames"], ["none"]).grounds == (
        "going_on",
        "seen_in_photo",
    )
    refused = answered(medical.id, ["smoke", "flames"], ["none"])
    assert (refused.status, refused.reason, refused.grounds_dropped) == (
        "unconfirmed",
        "not_grounded",
        2,
    )
    # A siren fits either. An ambulance fits a medical response.
    assert answered(medical.id, ["ambulance"], ["siren"]).grounds == (
        "going_on",
        "heard_in_clip",
        "seen_in_photo",
    )
    # A helicopter in the clip or the photo fits only where one was overhead, and
    # here none was. A blocked road fits an incident with a closure.
    assert answered(fire.id, ["helicopter"], ["helicopter"]).status == "unconfirmed"
    assert answered(fire.id, ["road_blocked"], ["none"]).grounds == ("going_on", "seen_in_photo")
    assert answered(medical.id, ["road_blocked"], ["none"]).status == "unconfirmed"
    # A police vehicle says nothing of a fire, and an answer of no tag nothing at all.
    assert answered(fire.id, ["police_vehicle"], ["a shout"]).status == "unconfirmed"
    # Smoke in the photo, with nothing beside it, is one ground that holds and no
    # match: six hundred metres off, a fire is not the only thing that smokes.
    alone = answered(fire.id, ["smoke"], ["none"], grounds=("seen_in_photo",))
    assert (alone.status, alone.reason, alone.grounds_dropped) == (
        "unconfirmed",
        "not_grounded",
        0,
    )


def test_a_grounds_sentence_says_what_code_checked_and_no_more(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    tap = rows.tap(6, ref=TAP, clip=True, photo=True)
    scene = scene_of(rows, tap, lambda _id, kind: CLIP if kind == "clip" else PHOTO)
    fire, medical = of_category(scene, "fire"), of_category(scene, "medical response")
    script = Script(
        [
            asks(NEAR, ("listen_clip", {}), ("look_photo", {})),
            fake_response(text=json.dumps({"heard": ["siren"]})),
            fake_response(text=json.dumps({"seen": ["ambulance", "flames"]})),
            form(medical.id, ("near", "going_on", "heard_in_clip", "seen_in_photo")),
        ]
    )

    run = investigate_tap(EmberClient.fake(storage, script), scene).run

    assert run.outcome.grounds == ("near", "going_on", "heard_in_clip", "seen_in_photo")
    assert [words.ground_text(ground, run) for ground in run.outcome.grounds] == [
        "When Beacon looked, the incident was about 100 m from where the tap came from.",
        # Open, or with an update shortly before: not "going on", which an incident
        # that is over and has a late press release is not.
        "The incident was open when the tap came, or Beacon had an update about it in the "
        "fifteen minutes before.",
        "In the clip sent with the tap, the investigator made out: a siren. That fits an "
        "incident of this kind.",
        # The ambulance fits a medical response and the flames do not: the ground
        # holds on one of them, and the sentence does not say that both fit.
        "In the photo sent with the tap, the investigator made out: flames, an ambulance. "
        "At least one of these fits an incident of this kind.",
    ]

    # The fire is where the tap is. Its distance was kept as 0, rounded to 50 m,
    # and is not said to be 50.
    script = Script([asks(NEAR), form(fire.id, ("near",))])
    scene = scene_of(rows, rows.tap(6.1, ref="tap-AAAAAAAA0009"))
    run = investigate_tap(EmberClient.fake(storage, script), scene).run
    assert words.found_then(run, fire.id)["distance_m"] == 0  # type: ignore[index]
    assert words.ground_text("near", run) == (
        "When Beacon looked, the incident was within 25 m of where the tap came from."
    )


def test_the_aircraft_over_the_incident_has_to_be_the_one_in_reach_of_the_tap(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    # A helicopter circles tightly over the fire. The tap is 1.6 km north of it,
    # where another aircraft passes low: one over each, and none over both.
    circle(rows, HAYES, 300, minutes=5.5, icao24="heli01")
    where = offset(HAYES, 1600, 0)
    for step in range(8):
        rows.aircraft(2 + step / 2, offset(where, 0, -600 + 150 * step), icao24="plane1")
    tap = rows.tap(6, where, ref=TAP, clip=True)
    scene = scene_of(rows, tap, lambda *_: CLIP)
    fire = of_category(scene, "fire")
    assert [track.icao24 for track in fire.aircraft] == ["heli01"]
    reach = {"radius_m": 2000, "minutes": 15}
    script = Script(
        [
            asks(("nearby_incidents", reach), ("get_aircraft", reach), ("listen_clip", {})),
            fake_response(text=json.dumps({"heard": ["helicopter"]})),
            form(fire.id, ("going_on", "aircraft_overhead", "heard_in_clip")),
        ]
    )

    made = investigate_tap(EmberClient.fake(storage, script), scene)

    nearby, aircraft, _heard = told(script.requests[2])
    # The tool finds aircraft near the tap, and Ember is told the fire has none
    # that is also in reach of the tap.
    assert len(aircraft["aircraft"]) == 2
    assert nearby["incidents"][0]["aircraft_over_it_in_reach_of_the_tap"] == 0
    outcome = made.run.outcome
    assert (outcome.status, outcome.reason, outcome.grounds_dropped) == (
        "unconfirmed",
        "not_grounded",
        2,
    )

    # A tap 700 m from the fire is in reach of the helicopter that is over it.
    near = rows.tap(6, offset(HAYES, 700, 0), ref="tap-AAAAAAAA0009", clip=True)
    scene = scene_of(rows, near, lambda *_: CLIP)
    script = Script(
        [
            asks(("nearby_incidents", reach), ("listen_clip", {})),
            fake_response(text=json.dumps({"heard": ["helicopter"]})),
            form(fire.id, ("going_on", "aircraft_overhead", "heard_in_clip")),
        ]
    )
    made = investigate_tap(EmberClient.fake(storage, script), scene)
    assert told(script.requests[2])[0]["incidents"][0]["aircraft_over_it_in_reach_of_the_tap"] == 1
    assert made.run.outcome.grounds == ("going_on", "heard_in_clip", "aircraft_overhead")


def test_a_helicopter_in_the_clip_fits_only_where_the_aircraft_overhead_was_one(
    rows: Rows, storage: Storage
) -> None:
    city(rows)
    # A light plane circles over the fire, in reach of a tap 700 m from it.
    circle(rows, HAYES, 300, minutes=5.5, icao24="plane1", category="light")
    tap = rows.tap(6, offset(HAYES, 700, 0), ref=TAP, clip=True)
    scene = scene_of(rows, tap, lambda *_: CLIP)
    fire = of_category(scene, "fire")
    script = Script(
        [
            asks(("nearby_incidents", {"radius_m": 2000, "minutes": 15}), ("listen_clip", {})),
            fake_response(text=json.dumps({"heard": ["helicopter"]})),
            form(fire.id, ("going_on", "aircraft_overhead", "heard_in_clip")),
        ]
    )

    outcome = investigate_tap(EmberClient.fake(storage, script), scene).run.outcome

    # An aircraft was overhead, which holds. That a helicopter was heard does not
    # fit it: what was over the fire was no helicopter.
    assert (outcome.grounds, outcome.grounds_dropped) == (("going_on", "aircraft_overhead"), 1)


@pytest.mark.parametrize(
    ("answer", "reason", "usable"),
    [
        (form(), "not_sure", True),
        (fake_response(text="It is the fire on Hayes St. Leave now."), "no_answer", False),
        (fake_response(text=json.dumps({"outcome": "certain"})), "no_answer", False),
        (fake_response(text=json.dumps(["incident"])), "no_answer", False),
        # A form with fields missing names no incident, whatever it says it did.
        (fake_response(text=json.dumps({"outcome": "incident"})), "no_answer", False),
        (fake_response(text=json.dumps(WHOLE | {"outcome": "incident"})), "no_answer", False),
        (fake_response(text=json.dumps(WHOLE | {"grounds": "near"})), "no_answer", False),
        (fake_response(text=json.dumps(WHOLE | {"crisis": "no"})), "no_answer", False),
        (fake_response(text=json.dumps(WHOLE | {"crisis_signals": [1]})), "no_answer", False),
        (fake_response(text=json.dumps(WHOLE)), "not_sure", True),
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


@pytest.mark.parametrize("field", ["outcome", "incident", "grounds", "crisis", "crisis_signals"])
def test_a_form_with_any_field_left_out_is_no_answer_though_null_is_one(
    rows: Rows, storage: Storage, field: str
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, offset(HAYES, 3000, 0), ref=TAP))
    left_out = {name: value for name, value in WHOLE.items() if name != field}

    def answered(said: dict[str, Any]) -> Any:
        script = Script([asks(NEAR), fake_response(text=json.dumps(said))])
        return investigate_tap(EmberClient.fake(storage, script), scene)

    made = answered(left_out)

    # A field that is not there is not a field that says null or nothing: the
    # answer is not the form, and counts as one Beacon could not use.
    assert made.run.outcome == Outcome(status="unconfirmed", reason="no_answer")
    assert made.usable is False
    # With every field there, `incident: null` included, it is an answer.
    whole = answered(WHOLE)
    assert whole.run.outcome == Outcome(status="unconfirmed", reason="nothing_near")
    assert whole.usable is True


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
        *(words.ground_text(ground, made.run) for ground in made.run.outcome.grounds),
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

    assert made.run.cost_nanousd == TAP_BUDGET_NANOUSD
    assert made.run.cost_usd == pytest.approx(TAP_BUDGET_USD)
    assert (made.run.cut_short, len(script.requests)) == ("budget", 1)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="cut_short")
    assert words.cut_short_text(made.run) == (
        "The investigation stopped: its next call to Muse Ember could have taken it past "
        "the most one tap may cost."
    )
    # The round that was paid for is kept, with what it cost. Nothing is unknown
    # about the run's cost: the call that was refused was not sent.
    assert [round_.ember.cost_nanousd for round_ in made.run.rounds] == [TAP_BUDGET_NANOUSD]
    assert (made.failure, made.run.unaccounted) == (None, 0)

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


# A live client against a stand-in for Meta's API, for what only a live client
# does: count the tokens, hold a call to what it may cost, and keep a ledger.


def live(storage: Storage, api: MetaApi, monkeypatch: pytest.MonkeyPatch) -> EmberClient:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    return api.client(storage, scope=SpendScope(SCOPE, 500_000_000))


def paid_requests(api: MetaApi) -> int:
    return api.paths().count("/v1/responses")


def test_a_call_that_could_take_a_tap_over_its_cap_is_refused_before_it_is_paid_for(
    rows: Rows, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    # The first request comes to $0.0095, which leaves half a tenth of a cent. A
    # request of 1,200 input tokens with 1,200 tokens of output allowed can cost
    # $0.00072: more than is left, though the tap is still under its cap.
    answers = [asks(NEAR, input_tokens=95_000), form()]
    api = MetaApi(create=lambda _request: httpx2_json(answers.pop(0)))
    ember = live(storage, api, monkeypatch)

    made = investigate_tap(ember, scene)

    assert made.run.cost_nanousd == 9_500_000 < TAP_BUDGET_NANOUSD
    # The second request had its tokens counted, which is free, and was not sent.
    assert (paid_requests(api), len(answers)) == (1, 1)
    counting = "/v1/responses/input_tokens"
    assert api.paths() == [counting, "/v1/responses", counting]
    assert (made.run.cut_short, made.failure, made.run.unaccounted) == ("budget", None, 0)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="cut_short")
    assert (made.run.model, made.run.stand_in) == ("live", True)
    # The ledger holds what was paid, under the investigator's scope, and no more.
    (call,) = ember.ledger_calls([made.run.rounds[0].ember.ledger])
    assert (call.cost_usd, call.status) == (pytest.approx(0.0095), "ok")
    assert ember.scope_spend(SCOPE).spent_nanousd == 9_500_000


def test_the_cap_holds_a_tools_own_call_and_the_last_answer_as_well(
    rows: Rows, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP, clip=True), lambda *_: CLIP)
    # $0.0095 again, and then the clip: its call can cost $0.00022, which fits.
    # After it $0.00028 is left, and the answer's request does not fit.
    answers = [
        asks(("listen_clip", {}), input_tokens=95_000),
        fake_response(text=json.dumps({"heard": ["siren"]}), input_tokens=2_200),
        form(),
    ]
    api = MetaApi(create=lambda _request: httpx2_json(answers.pop(0)))

    made = investigate_tap(live(storage, api, monkeypatch), scene)

    assert (paid_requests(api), len(answers)) == (2, 1)
    assert made.run.cost_nanousd == 9_500_000 + 220_000 <= TAP_BUDGET_NANOUSD
    assert (made.run.cut_short, made.run.final) == ("budget", None)
    assert [dict(call.returned) for call in made.run.rounds[0].calls] == [{"heard": ["siren"]}]


def test_a_paid_call_that_fails_leaves_a_run_whose_cost_is_said_not_to_be_all_there(
    rows: Rows, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    city(rows)
    scene = scene_of(rows, rows.tap(6, ref=TAP))
    api = MetaApi(create=lambda _request: httpx2_error(500))
    ember = live(storage, api, monkeypatch)

    made = investigate_tap(ember, scene)

    # The request was sent, and may have been paid for: the ledger kept back the
    # most it could cost, and the run has no accounting of it.
    assert paid_requests(api) == 1
    assert isinstance(made.failure, EmberCallError)
    assert (made.run.calls, made.run.cost_nanousd, made.run.unaccounted) == ([], 0, 1)
    assert made.run.outcome == Outcome(status="unconfirmed", reason="cut_short")
    spent = ember.scope_spend(SCOPE)
    assert spent.spent_nanousd == 1200 * 100 + 1200 * 500
    # A refusal before the paid request leaves nothing unaccounted.
    refused = investigate_tap(
        api.client(storage, scope=SpendScope(SCOPE, spent.spent_nanousd)), scene
    )
    assert paid_requests(api) == 1
    assert (refused.run.unaccounted, type(refused.failure).__name__) == (0, "BudgetExceededError")


def test_a_limit_only_refuses_and_is_held_beside_the_stop_and_the_scope(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MetaApi()
    ember = live(storage, api, monkeypatch)
    ask = {"task": "investigate_tap", "effort": "low", "max_output_tokens": 1000, "input": "x"}
    # 1,200 input tokens and 1,000 of output can cost 620,000 nano-dollars.
    with pytest.raises(CallLimitError, match="more than its caller allows"):
        ember.respond(sensitive=True, limit_nanousd=619_999, **ask)  # type: ignore[arg-type]
    assert paid_requests(api) == 0
    assert ember.respond(sensitive=True, limit_nanousd=620_000, **ask).source == "live"  # type: ignore[arg-type]
    # Nothing left is refused by any client, before anything is asked or counted.
    counted = len(api.paths())
    for nothing in (0, -5):
        with pytest.raises(CallLimitError):
            ember.respond(sensitive=False, limit_nanousd=nothing, **ask)  # type: ignore[arg-type]
        with pytest.raises(CallLimitError):
            EmberClient.fake(storage, Script([])).respond(
                sensitive=True,
                limit_nanousd=nothing,
                **ask,  # type: ignore[arg-type]
            )
    assert len(api.paths()) == counted
    # A refused call holds nothing in the ledger.
    spend = ember.scope_spend(SCOPE)
    assert (spend.calls, spend.unanswered) == (1, 0)


def httpx2_json(body: dict[str, Any]) -> Any:
    import httpx2

    return httpx2.Response(200, json=body)


def httpx2_error(status: int) -> Any:
    import httpx2

    return httpx2.Response(status, json={"error": {"message": "It failed."}})


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
            "Found 2 official alerts or notices, 1 of them an alert in force. 1 511 event "
            "in force lists a road as closed on a street of the incident's or near it.",
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
            "Found 3 radio calls, of 1 incident.",
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
    assert [words.ground_text(ground, run) for ground in run.outcome.grounds] == [
        "When Beacon looked, the incident was about 100 m from where the tap came from.",
        "The incident was open when the tap came, or Beacon had an update about it in the "
        "fifteen minutes before.",
        "In the clip sent with the tap, the investigator made out: a siren, a helicopter. "
        "At least one of these fits an incident of this kind.",
        "An aircraft that was over the incident was also within reach of where the tap came "
        "from, shortly before the tap.",
        "1 other tap came from nearby. Beacon cannot tell whether taps are from different people.",
    ]
    # With nothing kept of how far it was, no distance is said.
    bare = replace(run, rounds=())
    assert words.ground_text("near", bare) == (
        "When Beacon looked, the incident was within 500 m of where the tap came from."
    )
    # A stand-in is not called a model, and a model is not called a stand-in.
    assert words.investigator_text("fake") == (
        "A stand-in for Muse Ember made this investigation, by rule. No model investigated."
    )
    assert words.investigator_text("live") == (
        "Beacon's investigator is a model, Muse Ember. Its reading can be wrong."
    )
    assert words.investigator_text(None) is None
    assert "model" not in words.MATCHED + words.CRISIS
    assert words.stand_in_text("live", evidence=False) is None
    assert words.stand_in_text("live", evidence=True) == words.EVIDENCE_STAND_IN
    assert words.stand_in_text(None, evidence=True) == words.EVIDENCE_STAND_IN
    assert words.stand_in_text("fake", evidence=False) == words.investigator_text("fake")
    # No sentence tells a reader what to do.
    for text in (
        words.MATCHED,
        words.CRISIS,
        words.CRISIS_NOW,
        words.EVIDENCE_STAND_IN,
        words.SINCE,
        words.NEARBY,
        words.COST_INCOMPLETE,
        words.COST_LOST,
        words.investigator_text("fake") or "",
        words.investigator_text("live") or "",
    ):
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
        crisis_cited=tuple(Cited(kind, "medium", (7, 9)) for kind in signals),
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


def test_the_first_run_that_turns_crisis_mode_on_is_the_decision_kept_without_its_tap(
    tmp_path: Path,
) -> None:
    runs = runs_file(tmp_path)
    # A run that matched the incident and decided against crisis mode comes first.
    against = replace(crisis_run(TAP, "inc-1", 5, ()).outcome, crisis=False)
    runs.claim(TAP, at(5))
    runs.write(run_of(TAP, minute=5, outcome=against))
    assert runs.crisis("inc-1") is None
    for tap_id, minute, signals in (
        ("tap-AAAAAAAA0002", 6, ("alarm", "official_alert")),
        ("tap-AAAAAAAA0003", 9, ("closure",)),
    ):
        runs.claim(tap_id, at(minute))
        runs.write(crisis_run(tap_id, "inc-1", minute, signals))
    # And one after that decides against it again: nothing is turned off.
    runs.claim("tap-AAAAAAAA0004", at(10))
    runs.write(run_of("tap-AAAAAAAA0004", minute=10, outcome=against))

    decision = runs.crisis("inc-1")

    assert decision is not None
    # What it rested on, as it was then: each signal's kind, how far it was
    # trusted, and the rows under it.
    assert (decision.decided_at, decision.cited) == (
        at(6.1),
        (Cited("alarm", "medium", (7, 9)), Cited("official_alert", "medium", (7, 9))),
    )
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


def test_a_file_the_first_shape_wrote_keeps_its_claims_and_none_of_what_it_cannot_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "investigate" / "mock.sqlite3"
    runs_file(tmp_path).close()
    stamp = at(6).strftime("%Y-%m-%dT%H:%M:%S.%f")
    # A run and a decision as the first shape kept them: a decision's signals as
    # their kinds only, and of the incidents found only their ids.
    accounting = {
        "source": "fake",
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "ledger": ["generation", 1, "hash"],
    }
    first_shape = {
        "started_at": at(6).isoformat(),
        "finished_at": at(6.1).isoformat(),
        "outcome": {
            "status": "incident",
            "reason": None,
            "incident_id": "inc-1",
            "grounds": ["near"],
            "grounds_dropped": 0,
            "confidence": "low",
            "crisis": True,
            "crisis_signals": ["alarm"],
            "crisis_refused": False,
        },
        "rounds": [
            {
                "n": 1,
                "ember": accounting,
                "calls": [
                    {
                        "tool": "nearby_incidents",
                        "thinking": "no_lead",
                        "lead": None,
                        "why": "look_for_a_lead",
                        "args": {"radius_m": 1000, "minutes": 30},
                        "ok": True,
                        "returned": {"incidents": ["inc-1"], "more": False, "unlocated": 0},
                        "ember": None,
                    }
                ],
            }
        ],
        "final": accounting,
        "model": "fake",
        "stand_in": True,
        "cut_short": None,
        "as_of": at(6).isoformat(),
    }
    decision = {"signals": ["alarm"], "model": "fake", "stand_in": True}
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO run (tap_id, claimed_at, finished_at, run) VALUES (?, ?, ?, ?)",
            (TAP, stamp, stamp, json.dumps(first_shape)),
        )
        db.execute(
            "INSERT INTO crisis (incident_id, decided_at, decision) VALUES (?, ?, ?)",
            ("inc-1", stamp, json.dumps(decision)),
        )
        db.commit()
        db.execute("PRAGMA user_version = 1")

    again = Runs(path, "mock")

    # The tap was paid for and is not paid for again. Its run and the decision are
    # not read as if they were this shape's: they are gone.
    assert again.get(TAP) == (at(6), None)
    assert again.claim(TAP, at(7)) is False
    assert again.crisis("inc-1") is None
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    # And the file is this shape's from here on.
    again.claim("tap-AAAAAAAA0002", at(7))
    again.write(crisis_run("tap-AAAAAAAA0002", "inc-1", 7, ("alarm",)))
    assert Runs(path, "mock").crisis("inc-1") is not None


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


def test_runs_and_decisions_older_than_a_day_are_deleted_whether_or_not_a_tap_is_claimed(
    tmp_path: Path,
) -> None:
    runs = runs_file(tmp_path)
    day = KEPT.total_seconds() / 60

    def old_run(tap_id: str) -> None:
        runs.claim(tap_id, at(0))
        runs.write(crisis_run(tap_id, "inc-1", 0, ("alarm",)))

    # By a claim.
    old_run(TAP)
    runs.claim("tap-AAAAAAAA0002", at(day - 1))
    assert (runs.get(TAP) is not None, runs.crisis("inc-1") is not None) == (True, True)
    runs.claim("tap-AAAAAAAA0003", at(day + 1))
    assert (runs.get(TAP), runs.crisis("inc-1")) == (None, None)
    assert runs.get("tap-AAAAAAAA0002") is not None
    # By a tap that is passed over, when taps are only passed over.
    runs = Runs(tmp_path / "passed" / "mock.sqlite3", "mock")
    old_run(TAP)
    runs.skip("tap-AAAAAAAA0004", at(day + 1))
    assert (runs.get(TAP), runs.crisis("inc-1")) == (None, None)
    # And by the sweep, when no tap comes at all.
    runs = Runs(tmp_path / "swept" / "mock.sqlite3", "mock")
    old_run(TAP)
    assert runs.sweep(at(day - 1)) == 0
    assert runs.sweep(at(day + 1)) == 2
    assert (runs.get(TAP), runs.crisis("inc-1")) == (None, None)
    runs.close()
    with pytest.raises(RunsError, match="closed"):
        runs.sweep(at(day + 2))


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

    # It was made at half past, on the evidence as it stood when the tap came.
    assert (run.started_at, run.finished_at, run.as_of) == (at(6.5), at(6.6), at(6))
    assert run.last_id == scene.snapshot.last_id
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
