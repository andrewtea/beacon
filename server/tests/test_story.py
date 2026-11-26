"""Stories: what is kept of Ember's account of an incident, and when one is paid for."""

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import beacon.main as main_module
import beacon.story.desk as desk_module
from beacon.ember import (
    BudgetExceededError,
    EmberCallError,
    EmberClient,
    EmberClosedError,
    Script,
    Storage,
    fake_response,
)
from beacon.incident import Blocks, Engine, Incident, open_engine
from beacon.ingest import Clock, build
from beacon.ingest.paid import HELD_INTERVAL_SECONDS, UNUSABLE_RUN_LIMIT
from beacon.ingest.runner import PollerHeldError
from beacon.main import create_app
from beacon.redact import Names
from beacon.story import (
    BUDGET_VARIABLE,
    NOT_AN_ORDER,
    Stories,
    StoriesError,
    StoryConfigError,
    StoryDesk,
    StoryError,
    basis_of,
    merge_incident,
    open_stories,
    stand_in_writer,
)
from tests.incident_rows import HAYES, NOON, Rows, at, mock_store

BLOCKS = Blocks.of([("Hayes St", 400, HAYES.lat, HAYES.lon)])
NAMES = Names.of(streets=("Hayes St", "Gough St"), units=("E36", "T6", "B2"))
START = NOON - timedelta(minutes=20)


@pytest.fixture
def rows(tmp_path: Path) -> Rows:
    return Rows(mock_store(tmp_path))


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


def fire(rows: Rows, minute: float = 6) -> Incident:
    """A working fire with a closure and an order, read at `minute`."""
    if not rows.store.search(kinds=["radio"]):
        rows.radio(0, "fire", "dispatched", units=("E36", "T6"), street="Hayes St", block=400)
        rows.radio(1, "fire", "on_scene", units=("E36",))
        rows.radio(
            2, "fire", "working", units=("E36", "B2"), details=("evacuation",), clarity="partial"
        )
        rows.traffic(3)
        rows.alert(4, "shelter")
        rows.notice(5, "evacuation_order", instruction="Leave now.", summary="Leave Hayes St.")
    (incident,) = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: at(minute)).incidents()
    return incident


def answer(*claims: tuple[Any, Any], headline: Any = None) -> dict[str, Any]:
    form = {"headline": headline, "claims": [{"text": t, "cites": c} for t, c in claims]}
    return fake_response(text=json.dumps(form), input_tokens=900, output_tokens=120)


def everything_on_disk(directory: Path) -> str:
    return "".join(
        path.read_bytes().decode("latin-1") for path in directory.rglob("*") if path.is_file()
    )


def test_a_sentence_gets_its_sources_and_its_confidence_from_the_lines_it_cites(
    rows: Rows, storage: Storage
) -> None:
    incident = fire(rows)
    script = Script(
        [
            answer(
                ("Crews are at work at the 400 block of Hayes St.", [1, 3]),
                ("E36 is on scene.", [2]),
                ("511 lists a closure on Hayes St.", [4]),
                headline="Fire at the 400 block of Hayes St",
            )
        ]
    )

    draft = merge_incident(EmberClient.fake(storage, script), incident, NAMES)

    working, on_scene, closure = draft.claims
    radio = [row.id for row in rows.store.search(kinds=["radio"])]
    (closed,) = rows.store.search(kinds=["traffic"])
    # The third call was only partly made out, so what rests on it is trusted less.
    assert (working.confidence, on_scene.confidence, closure.confidence) == (
        "low",
        "medium",
        "official",
    )
    assert [source.evidence_id for source in working.sources] == [radio[0], radio[2]]
    assert [source.evidence_id for source in on_scene.sources] == [radio[1]]
    # 511's credit goes with the sentence that rests on 511.
    assert [(s.evidence_id, s.credit is not None) for s in closure.sources] == [(closed.id, True)]
    assert all(source.stand_in for claim in draft.claims for source in claim.sources)
    assert (draft.headline, draft.left_out, draft.model) == (
        "Fire at the 400 block of Hayes St",
        0,
        "fake",
    )
    assert (draft.lines, draft.lines_left_out) == (4, 0)
    assert (draft.input_tokens, draft.output_tokens) == (900, 120)


def test_ember_is_given_the_lines_of_radio_closures_aircraft_and_taps_and_no_official_word(
    rows: Rows, storage: Storage
) -> None:
    incident = fire(rows)
    script = Script([answer()])

    merge_incident(EmberClient.fake(storage, script), incident, NAMES)

    (request,) = script.requests
    document = json.loads(request["input"])
    assert [line["kind"] for line in document["lines"]] == ["radio", "radio", "radio", "closure"]
    assert [line["n"] for line in document["lines"]] == [1, 2, 3, 4]
    sent = json.dumps(request)
    # Nothing of the alert or of the notice: what an authority said is shown by
    # code, and Ember is not asked to retell it.
    for word in ("Headline of shelter", "Instruction of shelter", "Leave now", "evacuation order"):
        assert word not in sent
    assert request["text"]["format"]["strict"] is True
    # The call is a sensitive one: the ledger keeps its accounting and no text.
    with sqlite3.connect(storage.ledger_db) as db:
        ((task, kept_request, kept_response),) = db.execute(
            "SELECT task, request, response FROM ember_calls"
        )
    assert task == "merge_incident"
    assert json.loads(kept_request)["withheld"] == json.loads(kept_response)["withheld"]
    assert json.loads(kept_request)["withheld"] == "sensitive"


@pytest.mark.parametrize(
    "claim",
    [
        # A person, named or described.
        ("Jane Doe was taken from the building.", [1]),
        ("One resident was injured.", [1]),
        # A house, and a street nobody listed.
        ("Crews are at work at 432 Hayes St.", [1]),
        ("Crews are at work on Laguna St.", [1]),
        # Telling the reader what to do.
        ("Avoid the area.", [1]),
        ("You should leave now.", [3]),
        # An official act: Ember was given none, and says nothing of one.
        ("An evacuation order is in effect.", [3]),
        ("Officials ordered an evacuation.", [3]),
        ("The city issued a warning.", [3]),
        ("An official alert is active.", [3]),
        # Resting on nothing, or on a line that is not there.
        ("Crews are at work.", []),
        ("Crews are at work.", [5]),
        ("Crews are at work.", [0]),
        ("Crews are at work.", [1, True]),
        ("Crews are at work.", "1"),
        ("Crews are at work.", [1, 1, 2, 2, 3, 3, 4]),
        # Not a sentence at all, and one too long to be one.
        (None, [1]),
        ("Crews are at work. " * 20, [1]),
    ],
)
def test_a_sentence_that_may_not_be_shown_is_left_out_whole_and_counted(
    rows: Rows, storage: Storage, tmp_path: Path, claim: tuple[Any, Any]
) -> None:
    incident = fire(rows)
    script = Script([answer(claim, ("E36 is on scene.", [2]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident, NAMES)

    assert [kept.text for kept in draft.claims] == ["E36 is on scene."]
    assert draft.left_out == 1


def test_what_crews_say_about_an_evacuation_is_marked_as_radio_traffic(
    rows: Rows, storage: Storage
) -> None:
    incident = fire(rows)
    script = Script(
        [
            answer(
                ("Crews mention an evacuation on the radio.", [3]),
                ("Crews report sheltering nearby.", [3]),
                ("The radio says all clear.", [3]),
                ("E36 is on scene.", [2]),
                # A headline has no room for the note, so it is not used.
                headline="Evacuation at Hayes St",
            )
        ]
    )

    draft = merge_incident(EmberClient.fake(storage, script), incident, NAMES)

    assert [claim.note for claim in draft.claims] == [
        NOT_AN_ORDER,
        NOT_AN_ORDER,
        NOT_AN_ORDER,
        None,
    ]
    assert NOT_AN_ORDER == "That is radio traffic, not an official order."
    assert draft.headline is None


def test_a_medical_response_has_no_story_and_ember_is_not_asked(
    rows: Rows, storage: Storage
) -> None:
    rows.radio(0, "medical", units=("M52",), street="Hayes St", block=400)
    (incident,) = Engine(rows.store, BLOCKS, clock=lambda: at(1)).incidents()
    script = Script([])

    with pytest.raises(ValueError, match="medical response"):
        merge_incident(EmberClient.fake(storage, script), incident, NAMES)

    assert script.requests == []


@pytest.mark.parametrize("text", ["I cannot help with Jane Doe.", "[]", '{"claims": "none"}'])
def test_an_answer_that_is_no_form_is_an_error_that_says_nothing_of_it(
    rows: Rows, storage: Storage, text: str
) -> None:
    incident = fire(rows)
    ember = EmberClient.fake(storage, Script([fake_response(text=text)]))

    with pytest.raises(StoryError) as raised:
        merge_incident(ember, incident, NAMES)

    assert str(raised.value) == "Ember's answer about an incident is not a filled-in form."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_a_basis_names_the_lines_ember_would_be_given_and_nothing_else(rows: Rows) -> None:
    before = basis_of(fire(rows))
    # Another alert and another notice change no line Ember is given.
    rows.alert(5.5, "second")
    rows.notice(5.6, "all_clear")
    assert basis_of(fire(rows)) == before
    # Time alone changes nothing either.
    assert basis_of(fire(rows, minute=9)) == before

    rows.radio(7, "fire", "under_control", units=("B2",))
    assert basis_of(fire(rows, minute=8)) != before


# The stories file.


def stories(tmp_path: Path, mode: str = "mock") -> Stories:
    return Stories(tmp_path / "story" / f"{mode}.sqlite3", mode)  # type: ignore[arg-type]


def draft_of(rows: Rows, storage: Storage, model: str = "fake") -> Any:
    script = Script([answer(("E36 is on scene.", [2]), ("Jane Doe was taken away.", [1]))])
    draft = merge_incident(EmberClient.fake(storage, script), fire(rows), NAMES)
    return draft if model == "fake" else draft.__class__(**{**draft.__dict__, "model": model})


def test_a_basis_is_claimed_once_and_an_incident_no_more_often_than_its_interval(
    tmp_path: Path,
) -> None:
    kept = stories(tmp_path)
    minute = timedelta(minutes=1)

    assert kept.claim("inc-a", "one", at(0), 2 * minute)
    # The same basis is nobody else's, in this process or another.
    assert not stories(tmp_path).claim("inc-a", "one", at(10), 2 * minute)
    # Another basis of the incident has to wait for the interval, and another
    # incident does not.
    assert not kept.claim("inc-a", "two", at(1.9), 2 * minute)
    assert kept.claim("inc-b", "one", at(1.9), 2 * minute)
    assert kept.claim("inc-a", "two", at(2), 2 * minute)

    # A claim that was given up, because nothing was paid, can be made again.
    kept.release("inc-a", "two")
    assert kept.claim("inc-a", "two", at(2.5), 2 * minute)


def test_a_story_is_written_once_under_its_claim_and_read_back_as_it_was(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    kept = stories(tmp_path)
    draft = draft_of(rows, storage)
    with pytest.raises(StoriesError, match="only under a claim"):
        kept.write("inc-a", "one", draft, stand_in=True, now=at(1))

    kept.claim("inc-a", "one", at(0), timedelta(0))
    written = kept.write("inc-a", "one", draft, stand_in=True, now=at(1), state={"k": 1})

    assert kept.latest("inc-a") == written
    assert (written.claims, written.left_out, written.model) == (draft.claims, 1, "fake")
    assert (written.stand_in, written.written_at, kept.state()) == (True, at(1), {"k": 1})
    assert kept.latest("inc-b") is None
    with pytest.raises(StoriesError):
        kept.write("inc-a", "one", draft, stand_in=True, now=at(2))
    # A story that was written is not given up, and its basis stays claimed.
    kept.release("inc-a", "one")
    assert kept.latest("inc-a") == written
    # The sentence that did not pass is nowhere on disk.
    assert "Jane" not in everything_on_disk(tmp_path)

    # The newest story of an incident is the one that is read.
    kept.claim("inc-a", "two", at(5), timedelta(0))
    newer = kept.write("inc-a", "two", draft, stand_in=True, now=at(6))
    assert kept.latest("inc-a") == newer


def test_the_live_file_holds_nothing_a_stand_in_wrote_and_a_file_keeps_its_mode(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    live = stories(tmp_path, "live")
    live.claim("inc-a", "one", at(0), timedelta(0))

    for draft, stand_in in (
        (draft_of(rows, storage), False),
        (draft_of(rows, storage, "live"), True),
    ):
        with pytest.raises(StoriesError, match="nothing a stand-in wrote"):
            live.write("inc-a", "one", draft, stand_in=stand_in, now=at(1))
    kept = live.write("inc-a", "one", draft_of(rows, storage, "live"), stand_in=False, now=at(1))
    assert (kept.model, kept.stand_in) == ("live", False)

    with pytest.raises(StoriesError, match="begun as the live one"):
        Stories(tmp_path / "story" / "live.sqlite3", "mock")


def test_a_closed_file_takes_nothing_more_and_is_still_read(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    kept = stories(tmp_path)
    kept.claim("inc-a", "one", at(0), timedelta(0))
    written = kept.write("inc-a", "one", draft_of(rows, storage), stand_in=True, now=at(1))
    kept.claim("inc-a", "two", at(2), timedelta(0))

    kept.close()

    for write in (
        lambda: kept.claim("inc-a", "three", at(9), timedelta(0)),
        lambda: kept.release("inc-a", "two"),
        lambda: kept.save_state({}),
        lambda: kept.write("inc-a", "two", draft_of(rows, storage), stand_in=True, now=at(3)),
    ):
        with pytest.raises(StoriesError, match="closed"):
            write()
    assert kept.latest("inc-a") == written


def test_rows_older_than_a_day_are_deleted_when_a_new_one_is_claimed(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    kept = stories(tmp_path)
    kept.claim("inc-a", "one", at(0), timedelta(0))
    kept.write("inc-a", "one", draft_of(rows, storage), stand_in=True, now=at(1))

    kept.claim("inc-b", "one", at(23 * 60), timedelta(0))
    assert kept.latest("inc-a") is not None
    kept.claim("inc-c", "one", at(24 * 60 + 1), timedelta(0))
    assert kept.latest("inc-a") is None


# The desk.


class Writer:
    """A stand-in for Ember that answers each request with the next thing it was
    given: a response, or an error to raise in place of asking."""

    def __init__(self) -> None:
        self.asked = 0
        self.next: list[Any] = []

    def __call__(self, body: dict[str, Any]) -> Any:
        self.asked += 1
        return stand_in_writer(body)


@pytest.fixture
def writer() -> Writer:
    return Writer()


@pytest.fixture
def wall() -> list[datetime]:
    return [NOON]


@pytest.fixture(autouse=True)
def refusals(writer: Writer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Has `merge_incident` fail with the errors `writer.next` holds, in order,
    before Ember is asked: a fake client has no spending stop and no ledger."""
    merge = desk_module.merge_incident

    def merging(ember: EmberClient, incident: Incident, names: Names) -> Any:
        if writer.next:
            failure = writer.next.pop(0)
            if failure is not None:
                raise failure
        return merge(ember, incident, names)

    monkeypatch.setattr(desk_module, "merge_incident", merging)


def desk_of(tmp_path: Path, storage: Storage, writer: Any, wall: list[datetime]) -> StoryDesk:
    return StoryDesk(
        EmberClient.fake(storage, writer), stories(tmp_path), NAMES, wall_clock=lambda: wall[0]
    )


def test_a_story_is_paid_for_once_for_the_same_lines_and_again_when_they_change(
    rows: Rows, storage: Storage, tmp_path: Path, writer: Writer, wall: list[datetime]
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    incident = fire(rows)
    assert desk.latest(incident) is None

    story = desk.refresh(incident, at(6))

    assert story is not None
    assert writer.asked == 1
    assert (story.model, story.stand_in, story.basis) == ("fake", True, basis_of(incident))
    assert desk.latest(incident) == (story, True)
    assert [claim.note for claim in story.claims if "evacuation" in claim.text] == [NOT_AN_ORDER]
    # The same lines, later and from another desk on the same file: nothing is asked.
    assert desk.refresh(fire(rows, minute=20), at(20)) is None
    other = desk_of(tmp_path, storage, writer, wall)
    assert other.refresh(incident, at(21)) is None
    assert writer.asked == 1

    # A new line: the story is no longer current, and a new one waits for the interval.
    rows.radio(7, "fire", "under_control", units=("B2",))
    changed = fire(rows, minute=7.5)
    assert desk.latest(changed) == (story, False)
    assert desk.refresh(changed, at(7.5)) is None
    assert writer.asked == 1
    newer = desk.refresh(changed, at(8))
    assert newer is not None
    assert writer.asked == 2
    assert desk.latest(changed) == (newer, True)


def test_no_story_is_asked_for_a_medical_response_a_closed_incident_or_a_short_one(
    rows: Rows, storage: Storage, tmp_path: Path, writer: Writer, wall: list[datetime]
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    for minute in (0, 1, 2):
        rows.radio(minute, "medical", units=("M52",), street="Gough St", block=300)
    rows.radio(0, "alarm", "dispatched", units=("T6",), street="Hayes St", block=400)
    rows.radio(1, "alarm", "on_scene", units=("T6",))
    engine = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: at(3))

    assert desk.refresh_all(engine.snapshot()) == 0
    assert writer.asked == 0

    # A third line, and the alarm has something to tell. Cleared, it has not.
    rows.radio(2, "alarm", "working", units=("T6",))
    assert desk.refresh_all(Engine(rows.store, BLOCKS, clock=lambda: at(3)).snapshot()) == 1
    rows.radio(4, "alarm", "cleared", units=("T6",))
    assert desk.refresh_all(Engine(rows.store, BLOCKS, clock=lambda: at(9)).snapshot()) == 0
    assert writer.asked == 1


def test_a_refusal_that_sent_nothing_gives_the_claim_up_and_holds_the_desk(
    rows: Rows, storage: Storage, tmp_path: Path, writer: Writer, wall: list[datetime]
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    incident = fire(rows)
    writer.next = [BudgetExceededError("The spending stop was reached.")]

    with pytest.raises(BudgetExceededError):
        desk.refresh(incident, at(6))

    # Held, by the real clock, and across a restart.
    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS - 1)
    with pytest.raises(PollerHeldError, match="leaving Ember alone"):
        desk_of(tmp_path, storage, writer, wall).refresh(incident, at(6))
    assert writer.asked == 0
    # Nothing was paid, so the same lines are asked about once the hold is over.
    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS + 1)
    assert desk.refresh(incident, at(20)) is not None
    assert writer.asked == 1


def test_a_failure_that_may_have_been_paid_for_is_not_asked_about_again(
    rows: Rows, storage: Storage, tmp_path: Path, writer: Writer, wall: list[datetime]
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    incident = fire(rows)
    writer.next = [EmberCallError("The API failed.")]

    with pytest.raises(EmberCallError):
        desk.refresh(incident, at(6))

    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS + 1)
    assert desk.refresh(incident, at(20)) is None
    assert (writer.asked, desk.latest(incident)) == (0, None)
    # New lines are a new basis, and are asked about.
    rows.radio(21, "fire", "under_control", units=("B2",))
    assert desk.refresh(fire(rows, minute=22), at(22)) is not None


def test_answers_that_are_no_form_are_paid_for_once_each_and_hold_the_desk(
    rows: Rows, storage: Storage, tmp_path: Path, wall: list[datetime]
) -> None:
    asked: list[int] = []

    def no_form(_body: dict[str, Any]) -> Any:
        asked.append(1)
        return fake_response(text="Jane Doe lives at 432 Hayes St.")

    desk = desk_of(tmp_path, storage, no_form, wall)
    fire(rows)
    held = None
    for round_ in range(UNUSABLE_RUN_LIMIT):
        # New lines each time, three minutes apart: each answer is paid for.
        minute = 10 + 3 * round_
        rows.radio(minute, "fire", "working", units=("E36",))
        incident = fire(rows, minute=minute + 0.5)
        try:
            assert desk.refresh(incident, at(minute + 0.5)) is None
        except PollerHeldError as error:
            held = error
        assert desk.latest(incident) is None
    # The lines of the last answer are not asked about again, hold or no hold.
    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS + 1)
    assert desk.refresh(incident, at(30)) is None

    assert len(asked) == UNUSABLE_RUN_LIMIT
    assert held is not None
    assert "not filled-in forms" in str(held)
    assert "Jane" not in everything_on_disk(tmp_path)


def test_a_closed_desk_asks_nothing_and_leaves_the_lines_for_the_next_run(
    rows: Rows, storage: Storage, tmp_path: Path, writer: Writer, wall: list[datetime]
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    incident = fire(rows)

    desk.close()
    with pytest.raises(EmberClosedError):
        desk.refresh(incident, at(6))
    assert desk.stop(0.1) is True
    with pytest.raises(StoriesError):
        desk.refresh(incident, at(7))

    # Nothing was paid and nothing is held: the next process writes the story.
    again = desk_of(tmp_path, storage, writer, wall)
    assert again.refresh(incident, at(6.1)) is not None
    assert writer.asked == 1


def test_a_pass_writes_the_newest_few_and_ends_quietly_when_ember_fails(
    rows: Rows,
    storage: Storage,
    tmp_path: Path,
    writer: Writer,
    wall: list[datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    streets = ("Hayes St", "Gough St", "Fell St", "Irving St", "Geary Blvd")
    for place, street in enumerate(streets):
        for minute in (0, 1, 2):
            rows.radio(
                place + minute / 10,
                "alarm",
                "working",
                units=(f"E{place + 1}",),
                street=street,
                block=100,
            )
    engine = Engine(rows.store, BLOCKS, clock=lambda: at(6))

    assert desk.refresh_all(engine.snapshot()) == desk_module.MAX_PER_PASS
    assert writer.asked == desk_module.MAX_PER_PASS

    writer.next = [EmberCallError("The API failed with Jane Doe's name.")]
    assert desk.refresh_all(engine.snapshot()) == 0
    assert "A story could not be written (EmberCallError)." in caplog.text
    assert "Jane" not in caplog.text
    # Held now: the next pass asks nothing and says nothing.
    caplog.clear()
    assert desk.refresh_all(engine.snapshot()) == 0
    assert (writer.asked, caplog.text) == (desk_module.MAX_PER_PASS, "")


def test_a_live_desk_pays_only_under_its_own_scope(storage: Storage, tmp_path: Path) -> None:
    class Live:
        mode = "live"
        scope = None

    with pytest.raises(ValueError, match="story:merge"):
        StoryDesk(Live(), stories(tmp_path), NAMES)  # type: ignore[arg-type]


# The desk the environment asks for, and the server.


def test_the_mock_scenarios_fire_gets_a_stand_ins_story_that_says_so(tmp_path: Path) -> None:
    moment = [NOON]
    env = {"BEACON_DATA_DIR": str(tmp_path)}
    ingest = build(env, Clock(lambda: moment[0]))
    engine = open_engine(env, ingest.store, clock=lambda: moment[0])
    desk = open_stories(env, engine)
    assert desk is not None
    assert desk.stories.mode == "mock"
    app = create_app()
    app.state.incidents, app.state.stories = engine, desk
    client = TestClient(app)

    moment[0] = START + timedelta(minutes=20)
    ingest.catch_up()
    assert desk.refresh_all(engine.snapshot()) == 1

    (fire_id,) = [i.id for i in engine.incidents() if i.category == "fire"]
    story = client.get(f"/api/incidents/{fire_id}").json()["incident"]["story"]
    assert (story["model"], story["stand_in"], story["current"]) == ("fake", True, True)
    assert [claim["text"] for claim in story["claims"]] == [
        "The radio says crews are at work at the 400 block of Hayes St.",
        "Crews report alarm level 3.",
        "Crews mention an evacuation on the radio.",
        "511 lists a traffic event at this place.",
        "An aircraft was seen nearby.",
    ]
    evacuation, closure = story["claims"][2], story["claims"][3]
    assert evacuation["note"] == "That is radio traffic, not an official order."
    assert (evacuation["confidence"], closure["confidence"]) == ("medium", "official")
    assert closure["sources"][0]["credit"]["text"] == "data provided by 511.org"
    for claim in story["claims"]:
        assert claim["sources"]
        assert all(source["stand_in"] for source in claim["sources"])
    # A medical response has no story, and the list carries none.
    (medical_id,) = [i.id for i in engine.incidents() if i.place == "400 block of Fell St"]
    assert client.get(f"/api/incidents/{medical_id}").json()["incident"]["story"] is None
    assert "story" not in client.get("/api/incidents").json()["incidents"][0]

    # A minute on the radio has said more: the story is behind until the next pass.
    moment[0] = START + timedelta(minutes=27)
    ingest.catch_up()
    assert client.get(f"/api/incidents/{fire_id}").json()["incident"]["story"]["current"] is False
    assert desk.refresh_all(engine.snapshot()) == 1
    assert client.get(f"/api/incidents/{fire_id}").json()["incident"]["story"]["current"] is True


def test_on_the_live_store_stories_are_written_only_under_a_budget_and_by_a_live_ember(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from beacon.evidence import EvidenceStore
    from beacon.incident import NO_BLOCKS

    engine = Engine(EvidenceStore(tmp_path / "evidence" / "live.sqlite3", "live"), NO_BLOCKS)
    env = {"BEACON_DATA_DIR": str(tmp_path)}

    assert open_stories(env, engine) is None
    assert "No budget for stories (BEACON_STORY_BUDGET_USD)" in caplog.text
    with pytest.raises(StoryConfigError, match="in dollars"):
        open_stories(env | {BUDGET_VARIABLE: "a lot"}, engine)
    # A budget, and no live Ember to spend it: refused, and no file is left.
    with pytest.raises(StoryConfigError, match="live Ember"):
        open_stories(env | {BUDGET_VARIABLE: "0.25"}, engine)
    assert not (tmp_path / "story").exists()


def test_the_server_writes_the_stories_of_its_incidents_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "STORY_SECONDS", 0.05)
    with TestClient(create_app()) as client:
        desk = client.app.state.stories  # type: ignore[attr-defined]
        story = None
        for _ in range(200):
            fires = [
                incident
                for incident in client.get("/api/incidents").json()["incidents"]
                if incident["category"] == "fire"
            ]
            if fires:
                story = client.get(f"/api/incidents/{fires[0]['id']}").json()["incident"]["story"]
            if story is not None:
                break
            time.sleep(0.05)
        assert story is not None
        assert (story["model"], story["stand_in"]) == ("fake", True)

    # The server has shut down: the desk asks for nothing more and writes nothing.
    with pytest.raises(StoriesError, match="closed"):
        desk.stories.save_state({})
