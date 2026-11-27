"""Stories: what is kept of Ember's account of an incident, and when one is paid for."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import beacon.main as main_module
import beacon.story.desk as desk_module
import beacon.story.merge as merge_module
import beacon.story.store as store_module
from beacon.ember import (
    BudgetExceededError,
    EmberCallError,
    EmberClient,
    EmberClosedError,
    Script,
    Storage,
    fake_response,
)
from beacon.evidence import Point
from beacon.incident import Blocks, Credit, Engine, Incident, Source, open_engine
from beacon.incident.build import CREDIT_511, CREDIT_ADSBLOL
from beacon.ingest import Clock, build
from beacon.ingest.paid import HELD_INTERVAL_SECONDS, UNUSABLE_RUN_LIMIT
from beacon.ingest.runner import PollerHeldError
from beacon.main import create_app
from beacon.redact import Names
from beacon.story import (
    BUDGET_VARIABLE,
    POINT_KINDS,
    Stories,
    StoriesError,
    StoryConfigError,
    StoryDesk,
    StoryError,
    basis_of,
    lines_of,
    merge_incident,
    open_stories,
    stand_in_writer,
)
from tests.incident_rows import HAYES, NOON, Rows, at, mock_store

EVACUATION_SAID = (
    "An evacuation was mentioned on the radio. That is radio traffic, not an official order."
)
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
    """A working fire with a closure, taps, an alert, and an order, read at `minute`."""
    if not rows.store.search(kinds=["radio"]):
        rows.radio(0, "fire", "dispatched", units=("E36", "T6"), street="Hayes St", block=400)
        rows.radio(1, "fire", "on_scene", units=("E36",), alarm=2)
        rows.radio(
            2, "fire", "working", units=("E36", "B2"), details=("evacuation",), clarity="partial"
        )
        rows.traffic(3)
        rows.tap(3.5)
        rows.alert(4, "shelter")
        rows.notice(5, "evacuation_order", instruction="Leave now.", summary="Leave Hayes St.")
    (incident,) = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: at(minute)).incidents()
    return incident


def answer(*points: tuple[Any, Any], **more: Any) -> dict[str, Any]:
    form = {"points": [{"kind": kind, "cites": cites} for kind, cites in points]} | more
    return fake_response(text=json.dumps(form), input_tokens=900, output_tokens=120)


def everything_on_disk(directory: Path) -> str:
    return "".join(
        path.read_bytes().decode("latin-1") for path in directory.rglob("*") if path.is_file()
    )


def test_code_writes_each_sentence_from_the_rows_its_point_cites(
    rows: Rows, storage: Storage
) -> None:
    incident = fire(rows)
    script = Script(
        [
            answer(
                ("status", [3]),
                ("alarm", [2]),
                ("units", [1, 3]),
                ("evacuation", [3]),
                ("closure", [4]),
                ("taps", [5]),
            )
        ]
    )

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    radio = [row.id for row in rows.store.search(kinds=["radio"])]
    (closed,) = rows.store.search(kinds=["traffic"])
    assert [(claim.kind, claim.text) for claim in draft.claims] == [
        ("status", "The radio said: crews at work."),
        ("alarm", "The radio said: alarm level 2."),
        ("units", "Units named on the radio: E36, T6, B2."),
        # What crews said about an evacuation is radio traffic, and is said so.
        (
            "evacuation",
            EVACUATION_SAID,
        ),
        # 511's own words, as the timeline has them.
        ("closure", "511 lists: Activity on Hayes St"),
        (
            "taps",
            '1 tap of "What\'s that?" came from nearby while this was going on. Beacon '
            "cannot tell whether they are from different people.",
        ),
    ]
    # The third call was only partly made out, so what rests on it is trusted less.
    assert [claim.confidence for claim in draft.claims] == [
        "low",
        "medium",
        "low",
        "low",
        "official",
        "low",
    ]
    status, alarm, units, _evacuation, closure, taps = draft.claims
    assert [source.evidence_id for source in status.sources] == [radio[2]]
    assert [source.evidence_id for source in alarm.sources] == [radio[1]]
    assert [source.evidence_id for source in units.sources] == [radio[0], radio[2]]
    # A sentence is as of its newest line.
    assert (status.at, alarm.at, units.at) == (at(2), at(1), at(2))
    # 511's credit goes with the sentence that rests on 511.
    assert [(s.evidence_id, s.credit is not None) for s in closure.sources] == [(closed.id, True)]
    assert [source.evidence_id for source in taps.sources] == [None]
    assert (draft.left_out, draft.model, draft.lines, draft.lines_left_out) == (0, "fake", 5, 0)
    assert (draft.input_tokens, draft.output_tokens) == (900, 120)


def test_ember_is_given_the_lines_of_radio_closures_aircraft_and_taps_and_no_official_word(
    rows: Rows, storage: Storage
) -> None:
    incident = fire(rows)
    script = Script([answer()])

    merge_incident(EmberClient.fake(storage, script), incident)

    (request,) = script.requests
    document = json.loads(request["input"])
    assert [line["kind"] for line in document["lines"]] == [
        "radio",
        "radio",
        "radio",
        "closure",
        "taps",
    ]
    assert [line["n"] for line in document["lines"]] == [1, 2, 3, 4, 5]
    sent = json.dumps(request)
    # Nothing of the alert or of the notice: what an authority said is shown by
    # code, and Ember is not asked about it.
    for word in ("Headline of shelter", "Instruction of shelter", "Leave now", "evacuation order"):
        assert word not in sent
    form = request["text"]["format"]
    assert form["strict"] is True
    # The form has no place for a sentence: a kind from a closed list, and numbers.
    point = form["schema"]["properties"]["points"]["items"]
    assert set(point["properties"]) == {"kind", "cites"}
    assert point["properties"]["kind"]["enum"] == list(POINT_KINDS)
    assert point["additionalProperties"] is False
    # The call is a sensitive one: the ledger keeps its accounting and no text.
    with sqlite3.connect(storage.ledger_db) as db:
        ((task, kept_request, kept_response),) = db.execute(
            "SELECT task, request, response FROM ember_calls"
        )
    assert task == "merge_incident"
    assert json.loads(kept_request)["withheld"] == json.loads(kept_response)["withheld"]
    assert json.loads(kept_request)["withheld"] == "sensitive"


@pytest.mark.parametrize(
    "point",
    [
        # A detail none of the cited calls gave, or only one of them did.
        ("all_clear", [3]),
        ("knocked_down", [1]),
        ("evacuation", [2, 3]),
        # Words about an evacuation resting on taps, or on a closure.
        ("evacuation", [5]),
        ("all_clear", [4]),
        # An alarm level from a call that gave none, and a status from two calls.
        ("alarm", [1]),
        ("status", [1, 2]),
        # A line of another kind than the point.
        ("closure", [1]),
        ("taps", [4]),
        ("aircraft", [5]),
        ("status", [4]),
        ("units", [1, 5]),
        # A kind that is not on the list: an instruction, an official act, a person.
        ("leave_now", [1]),
        ("official_order", [3]),
        ("Jane Doe was taken from 432 Hayes St.", [1]),
        (None, [1]),
        (["status"], [1]),
        # Resting on nothing, or on a line that is not there.
        ("status", []),
        ("status", [6]),
        ("status", [0]),
        ("status", [True]),
        ("status", "1"),
        ("units", [1, 1, 2, 2, 3]),
    ],
)
def test_a_point_its_lines_do_not_show_is_left_out_and_counted(
    rows: Rows, storage: Storage, point: tuple[Any, Any]
) -> None:
    incident = fire(rows)
    script = Script([answer(point, ("status", [2]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    assert [(claim.kind, claim.text) for claim in draft.claims] == [
        ("status", "The radio said: units on scene.")
    ]
    assert draft.left_out == 1


def test_no_word_ember_writes_is_read_kept_or_shown(
    rows: Rows, storage: Storage, tmp_path: Path
) -> None:
    incident = fire(rows)
    # A model can ignore the form: a headline, a sentence beside a point, a note.
    form = {
        "headline": "The area is safe. Jane Doe says go home.",
        "points": [
            {"kind": "status", "cites": [2], "text": "Shelter in place now."},
            {"kind": "evacuation", "cites": [3], "note": "Exit the building."},
        ],
        "summary": "Evacuation is over.",
    }
    ember = EmberClient.fake(storage, Script([fake_response(text=json.dumps(form))]))

    draft = merge_incident(ember, incident)

    assert [claim.text for claim in draft.claims] == [
        "The radio said: units on scene.",
        EVACUATION_SAID,
    ]
    kept = repr(draft) + everything_on_disk(tmp_path)
    for word in ("safe", "Jane", "go home", "Shelter in place now", "Exit", "is over"):
        assert word not in kept


def test_a_point_that_says_again_what_another_said_is_told_once(
    rows: Rows, storage: Storage
) -> None:
    incident = fire(rows)
    script = Script([answer(("status", [2]), ("status", [2]), ("units", [1]), ("units", [2]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    assert [claim.text for claim in draft.claims] == [
        "The radio said: units on scene.",
        "Units named on the radio: E36, T6.",
        "Units named on the radio: E36.",
    ]
    assert draft.left_out == 1


def test_an_alarm_level_is_told_only_from_calls_that_give_the_same_level(
    rows: Rows, storage: Storage
) -> None:
    fire(rows)
    # The second call gave level 2. Later calls give level 1, twice.
    rows.radio(5.5, "fire", "working", units=("E36",), alarm=1)
    rows.radio(5.7, "fire", "working", units=("B2",), alarm=1)
    incident = fire(rows)
    script = Script([answer(("alarm", [2, 6]), ("alarm", [6, 7]), ("alarm", [2]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    # Level 2 and level 1 together would be "level 2" as of the later call, which
    # no call said then: the point is left out.
    assert [(claim.text, claim.at) for claim in draft.claims] == [
        ("The radio said: alarm level 1.", at(5.7)),
        ("The radio said: alarm level 2.", at(1)),
    ]
    assert draft.left_out == 1


def test_a_version_of_a_closure_that_511_has_changed_is_not_told_as_what_511_says_now(
    rows: Rows, storage: Storage
) -> None:
    fire(rows)
    rows.traffic(5.5, created=3, headline="Hayes St closed, and Gough St with it")
    (closure,) = rows.store.search(kinds=["traffic"])[1:]
    rows.change(5.8, closure)
    incident = fire(rows)
    script = Script([answer(("closure", [4]), ("closure", [6]), ("closure", [7]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    first, second = rows.store.search(kinds=["traffic"])[:2]
    # A version that is not the one in force is told as having been listed, which
    # is all its row shows, and in no word of its own. What followed (a later
    # version, Beacon no longer seeing it listed) is told only by the row that
    # shows it, at that row's confidence, as what Beacon saw at that check.
    assert [(claim.text, claim.confidence) for claim in draft.claims] == [
        ("511 listed a traffic event.", "official"),
        ("511 listed a traffic event.", "official"),
        (
            "When Beacon checked, 511 was no longer listing a traffic event it had "
            "listed. That is what Beacon saw then, not an official reopening.",
            "medium",
        ),
    ]
    assert draft.claims[2].at == at(5.8)
    assert [[source.evidence_id for source in claim.sources] for claim in draft.claims][:2] == [
        [first.id],
        [second.id],
    ]
    shown = " ".join(claim.text for claim in draft.claims)
    assert "changed" not in shown
    assert "Gough St" not in shown


def test_a_detail_is_told_as_what_was_said_on_the_radio_and_names_nobody_who_said_it(
    rows: Rows, storage: Storage
) -> None:
    rows.radio(0, "fire", "dispatched", units=("E36",), street="Hayes St", block=400)
    # A dispatcher relays a request: the row keeps the request and not who made it.
    rows.radio(1, "fire", units=("E36",), details=("police_requested", "working_fire"))
    rows.radio(2, "fire", units=("E36",), details=("additional_alarm", "utilities_requested"))
    (incident,) = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: at(6)).incidents()
    kinds = ("police_requested", "working_fire", "additional_alarm", "utilities_requested")
    cites = ([2], [2], [3], [3])
    script = Script([answer(*zip(kinds, cites, strict=True))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    assert [claim.text for claim in draft.claims] == [
        "Radio traffic reported a request for police.",
        "Radio traffic reported a working fire declared.",
        "Radio traffic reported an additional alarm called.",
        "Radio traffic reported a request for utilities.",
    ]
    # No detail's sentence says who spoke or who acted.
    for text in merge_module.DETAIL_SAID.values():
        assert "rews" not in text
        assert "ispatch" not in text


def test_the_words_of_a_story_are_not_changed_without_the_files_number() -> None:
    worded = (
        merge_module.OLD_CLOSURE,
        sorted(merge_module.CHANGE_SAID.items()),
        sorted(merge_module.DETAIL_SAID.items()),
        sorted(merge_module.STATUS_WORDS.items()),
    )
    digest = hashlib.sha256(repr(worded).encode()).hexdigest()[:16]

    # A kept story is shown in the words it was kept in. When this fails because a
    # sentence changed, raise `_FORMAT` in story/store.py and add the old number to
    # `_EARLIER`, so that kept stories in the old words are dropped; then put the
    # new number and digest here.
    assert (store_module._FORMAT, digest) == (3, "df9585d83d76aaa6")
    assert store_module._FORMAT not in store_module._EARLIER


def test_an_event_that_left_511s_list_and_came_back_is_not_told_as_gone(
    rows: Rows, storage: Storage
) -> None:
    fire(rows)
    (closure,) = rows.store.search(kinds=["traffic"])
    rows.change(5.5, closure)
    rows.change(5.8, closure, "listed_again")
    incident = fire(rows)
    script = Script([answer(("closure", [6]), ("closure", [7]), ("closure", [4]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    gone, back, listed = draft.claims
    # The event is listed now. A point that cites the check at which it was missing
    # says what Beacon saw then, and nothing in the present tense.
    assert (gone.text, gone.at, gone.confidence) == (
        "When Beacon checked, 511 was no longer listing a traffic event it had listed. "
        "That is what Beacon saw then, not an official reopening.",
        at(5.5),
        "medium",
    )
    assert (back.text, back.at, back.confidence) == (
        "When Beacon checked, 511 was listing again a traffic event it had stopped listing.",
        at(5.8),
        "medium",
    )
    assert listed.text == "511 lists: Activity on Hayes St"
    for claim in (gone, back):
        assert "no longer lists" not in claim.text
        assert "lists this event" not in claim.text


def test_a_version_of_an_event_that_was_about_another_road_is_not_said_to_be_here(
    rows: Rows, storage: Storage
) -> None:
    rows.radio(0, "fire", "dispatched", units=("E36",), street="Hayes St", block=400)
    rows.radio(1, "fire", "on_scene", units=("E36",))
    # 511 first had the event on Ocean Ave, far away, and then on Hayes St. Both
    # versions are among the incident's lines, because the second is about it.
    elsewhere = rows.traffic(
        2, "511.org/9", road="Ocean Ave", point=Point(37.7236, -122.4544), headline="Ocean Ave"
    )
    rows.traffic(3, "511.org/9", created=2, headline="Activity on Hayes St")
    (incident,) = Engine(rows.store, BLOCKS, names=NAMES, clock=lambda: at(6)).incidents()
    lines, _ = lines_of(incident)
    (old,) = [n for n, line in enumerate(lines, 1) if line.details.get("state") == "replaced"]
    script = Script([answer(("closure", [old]))])

    draft = merge_incident(EmberClient.fake(storage, script), incident)

    (claim,) = draft.claims
    assert claim.text == "511 listed a traffic event."
    assert "here" not in claim.text
    assert "Ocean" not in claim.text
    assert [source.evidence_id for source in claim.sources] == [elsewhere.id]


def test_the_stand_in_cites_the_call_that_gave_the_status_and_not_the_newest_call(
    rows: Rows, storage: Storage
) -> None:
    fire(rows)
    # A later call that names a unit and says nothing of how far the call has got.
    rows.radio(5.5, "fire", units=("T6",))
    incident = fire(rows)

    draft = merge_incident(EmberClient.fake(storage, stand_in_writer), incident)

    radio = [row.id for row in rows.store.search(kinds=["radio"])]
    status = draft.claims[0]
    assert (status.kind, status.text) == ("status", "The radio said: crews at work.")
    assert [source.evidence_id for source in status.sources] == [radio[2]]
    assert status.at == at(2)
    # Every point the stand-in gave is one its lines show.
    assert draft.left_out == 0


def test_a_medical_response_has_no_story_and_ember_is_not_asked(
    rows: Rows, storage: Storage
) -> None:
    rows.radio(0, "medical", units=("M52",), street="Hayes St", block=400)
    (incident,) = Engine(rows.store, BLOCKS, clock=lambda: at(1)).incidents()
    script = Script([])

    with pytest.raises(ValueError, match="medical response"):
        merge_incident(EmberClient.fake(storage, script), incident)

    assert script.requests == []


@pytest.mark.parametrize("text", ["I cannot help with Jane Doe.", "[]", '{"points": "none"}'])
def test_an_answer_that_is_no_form_is_an_error_that_says_nothing_of_it(
    rows: Rows, storage: Storage, text: str
) -> None:
    incident = fire(rows)
    ember = EmberClient.fake(storage, Script([fake_response(text=text)]))

    with pytest.raises(StoryError) as raised:
        merge_incident(ember, incident)

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
    script = Script([answer(("status", [2]), ("all_clear", [1]))])
    draft = merge_incident(EmberClient.fake(storage, script), fire(rows))
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
    assert [claim.text for claim in written.claims] == ["The radio said: units on scene."]

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


OLD_STORIES = {
    # As the first shape kept a story: sentences a model wrote, with no kind and no
    # time, under a headline.
    0: {
        "written_at": "2026-11-27T20:00:30+00:00",
        "headline": "Fire on Hayes St",
        "claims": [{"text": "Exit the building.", "note": None, "confidence": "medium"}],
    },
    # As the second did: this shape's fields, in words that have since changed.
    2: {
        "written_at": "2026-11-27T20:00:30+00:00",
        "claims": [
            {
                "kind": "closure",
                "text": "511 no longer lists this event. That is what Beacon saw, not an "
                "official reopening.",
                "at": "2026-11-27T20:00:20+00:00",
                "confidence": "medium",
                "sources": [],
            }
        ],
        "left_out": 0,
        "lines": 4,
        "lines_left_out": 0,
        "model": "fake",
        "stand_in": True,
        "input_tokens": 1,
        "cached_tokens": 0,
        "output_tokens": 1,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 1,
    },
}
# A story of this shape from before Beacon asked adsb.lol: an aircraft's line that
# cites a row in OpenSky's format, under that feed's name and label.
BEFORE_ADSBLOL = OLD_STORIES[2] | {
    "claims": [
        {
            "kind": "aircraft",
            "text": "A helicopter (MOCKH1) circled nearby for about 6 min, as low as 300 m.",
            "at": "2026-11-27T20:00:20+00:00",
            "confidence": "high",
            "sources": [
                {
                    "kind": "aircraft",
                    "source": "opensky",
                    "label": "OpenSky Network aircraft positions",
                    "confidence": "high",
                    "at": "2026-11-27T20:00:20+00:00",
                    "evidence_id": 7,
                    "url": None,
                    "stand_in": True,
                    "model": None,
                    "credit": None,
                }
            ],
        }
    ]
}
SCHEMA = """
CREATE TABLE story (incident_id TEXT NOT NULL, basis TEXT NOT NULL, claimed_at TEXT NOT NULL,
    written_at TEXT, story TEXT, PRIMARY KEY (incident_id, basis));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('mode', 'mock');
INSERT INTO story VALUES ('inc-a', 'one', '2026-11-27T20:00:00.000000',
    '2026-11-27T20:00:30.000000', :story);
INSERT INTO story VALUES ('inc-a', 'two', '2026-11-27T20:01:00.000000', NULL, NULL);
"""


def old_file(tmp_path: Path, shape: int, journal: str = "wal") -> Path:
    """A mock stories file that says it is of `shape`, as another Beacon left it:
    one story, and one claim with no story."""
    path = tmp_path / "story" / "mock.sqlite3"
    path.parent.mkdir(parents=True)
    story = json.dumps(
        BEFORE_ADSBLOL if shape == store_module._FORMAT else OLD_STORIES.get(shape, OLD_STORIES[2])
    )
    db = sqlite3.connect(path, isolation_level=None)
    db.execute(f"PRAGMA journal_mode = {journal}")
    db.executescript(SCHEMA.replace(":story", "'" + story.replace("'", "''") + "'"))
    db.execute(f"PRAGMA user_version = {shape}")
    db.close()
    return path


def file_rows(path: Path) -> list[Any]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return [
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("PRAGMA journal_mode").fetchone()[0],
            *db.execute("SELECT incident_id, basis, written_at, story FROM story"),
            *db.execute("SELECT key, value FROM meta"),
        ]


@pytest.mark.parametrize("shape", sorted(OLD_STORIES))
def test_a_file_of_an_earlier_shape_keeps_its_claims_and_none_of_its_stories(
    rows: Rows, storage: Storage, tmp_path: Path, shape: int
) -> None:
    path = old_file(tmp_path, shape)

    opened = stories(tmp_path)

    # The old story is not read, by the route or by the desk, and is not there: a
    # story is shown in the words it was kept in, and those are not this Beacon's.
    assert opened.latest("inc-a") is None
    assert file_rows(path)[:4] == [
        store_module._FORMAT,
        "wal",
        ("inc-a", "one", None, None),
        ("inc-a", "two", None, None),
    ]
    # Its lines were paid for once, and are not paid for again.
    assert not opened.claim("inc-a", "one", at(5), timedelta(0))
    # A new basis gets a story, which is read back, and opening the file again
    # drops nothing of this shape.
    assert opened.claim("inc-a", "three", at(5), timedelta(0))
    written = opened.write("inc-a", "three", draft_of(rows, storage), stand_in=True, now=at(6))
    assert stories(tmp_path).latest("inc-a") == written


def test_a_story_from_before_the_aircraft_feed_changed_is_kept_as_it_was_written(
    tmp_path: Path,
) -> None:
    path = old_file(tmp_path, store_module._FORMAT)
    before = file_rows(path)

    opened = stories(tmp_path)

    # The story was paid for and its basis is claimed, so it could not be made
    # again. It cites a row in OpenSky's format and says so, as it did: nothing of
    # it is put under adsb.lol's name, and it has no notice of adsb.lol's.
    story = opened.latest("inc-a")
    assert story is not None
    (claim,) = story.claims
    (source,) = claim.sources
    assert (source.source, source.label) == ("opensky", "OpenSky Network aircraft positions")
    assert (source.stand_in, source.credit) == (True, None)
    assert file_rows(path) == before


def test_a_credit_in_pieces_is_kept_with_its_pieces_and_one_link_as_it_always_was() -> None:
    def source(credit: Credit) -> Source:
        return Source(
            kind="aircraft",
            source="adsblol",
            label="adsb.lol aircraft positions",
            confidence="high",
            at=at(1),
            evidence_id=7,
            url=None,
            stand_in=False,
            credit=credit,
        )

    kept = store_module._source_json(source(CREDIT_ADSBLOL))
    assert store_module._source_from(json.loads(json.dumps(kept))).credit == CREDIT_ADSBLOL
    # 511's is one link, and is kept in the two fields a file of this shape always
    # had, so an earlier Beacon reads it and this one reads an earlier Beacon's.
    kept = store_module._source_json(source(CREDIT_511))
    assert kept["credit"] == {"text": "data provided by 511.org", "url": "https://511.org"}
    assert store_module._source_from(kept).credit == CREDIT_511


@pytest.mark.parametrize("journal", ["delete", "wal"])
@pytest.mark.parametrize(
    ("shape", "mode", "why"),
    [
        # A shape a newer Beacon wrote, whichever mode it is opened as.
        (store_module._FORMAT + 1, "mock", "a shape this Beacon does not know"),
        (store_module._FORMAT + 1, "live", "a shape this Beacon does not know"),
        (1, "mock", "a shape this Beacon does not know"),
        # The other mode's file, in an earlier shape and in this one.
        (0, "live", "begun as the mock one"),
        (2, "live", "begun as the mock one"),
        (store_module._FORMAT, "live", "begun as the mock one"),
    ],
)
def test_a_file_that_is_refused_is_left_as_it_was_to_the_byte(
    tmp_path: Path, shape: int, mode: str, why: str, journal: str
) -> None:
    path = old_file(tmp_path, shape, journal)
    before, rows_before = path.read_bytes(), file_rows(path)

    with pytest.raises(StoriesError, match=why):
        Stories(path, mode)  # type: ignore[arg-type]

    # No table, no row, no number, and no journal mode of it was written: its story
    # is there, and it is the file the Beacon that wrote it left.
    assert path.read_bytes() == before
    assert file_rows(path) == rows_before
    assert (rows_before[0], rows_before[1], rows_before[2][3] is not None) == (shape, journal, True)


def test_the_advice_for_a_file_in_an_unknown_shape_is_never_to_begin_another(
    tmp_path: Path,
) -> None:
    path = old_file(tmp_path, store_module._FORMAT + 1)

    with pytest.raises(StoriesError) as refused:
        Stories(path, "mock")

    # The file is the only record of which lines were paid for.
    said = str(refused.value)
    assert "Do not delete the file or move it away" in said
    assert "paid for again" in said
    assert "to begin a new one" not in said


def test_a_file_another_process_makes_a_newer_shape_while_it_is_opened_is_refused(
    tmp_path: Path,
) -> None:
    path = old_file(tmp_path, 0)
    # Another, newer Beacon is in the middle of making the file its own shape.
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute(f"PRAGMA user_version = {store_module._FORMAT + 1}")
    came_of_it: list[Any] = []

    def opening() -> None:
        try:
            came_of_it.append(Stories(path, "mock"))
        except StoriesError as error:
            came_of_it.append(str(error))

    thread = threading.Thread(target=opening)
    thread.start()
    time.sleep(0.3)
    other.execute("COMMIT")
    other.close()
    thread.join(10)

    # The shape is looked at under the same lock the file is changed under, so the
    # file is seen as the other process left it, and refused.
    assert len(came_of_it) == 1
    assert isinstance(came_of_it[0], str)
    assert "a shape this Beacon does not know" in came_of_it[0]
    assert file_rows(path)[0] == store_module._FORMAT + 1
    assert file_rows(path)[2][3] is not None


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

    def merging(ember: EmberClient, incident: Incident) -> Any:
        if writer.next:
            failure = writer.next.pop(0)
            if failure is not None:
                raise failure
        return merge(ember, incident)

    monkeypatch.setattr(desk_module, "merge_incident", merging)


def desk_of(tmp_path: Path, storage: Storage, writer: Any, wall: list[datetime]) -> StoryDesk:
    return StoryDesk(
        EmberClient.fake(storage, writer), stories(tmp_path), wall_clock=lambda: wall[0]
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
    assert [claim.kind for claim in story.claims] == [
        "status",
        "alarm",
        "evacuation",
        "closure",
        "taps",
    ]
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
    with pytest.raises(EmberClosedError):
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
    assert "A story could not be made (EmberCallError)." in caplog.text
    assert "Jane" not in caplog.text
    # Held now: the next pass asks nothing and says nothing.
    caplog.clear()
    assert desk.refresh_all(engine.snapshot()) == 0
    assert (writer.asked, caplog.text) == (desk_module.MAX_PER_PASS, "")


def test_a_pass_asks_ember_about_its_few_incidents_whatever_came_of_the_answers(
    rows: Rows, storage: Storage, tmp_path: Path, wall: list[datetime]
) -> None:
    asked: list[int] = []

    def every_other_is_no_form(body: dict[str, Any]) -> Any:
        asked.append(1)
        if len(asked) % 2:
            return fake_response(text="Not a form.")
        return stand_in_writer(body)

    desk = desk_of(tmp_path, storage, every_other_is_no_form, wall)
    for place, street in enumerate(("Hayes St", "Gough St", "Fell St", "Irving St", "Geary Blvd")):
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

    # Three answers were paid for and one of them made a story. The two that came
    # to nothing count toward the pass's few all the same.
    assert desk.refresh_all(engine.snapshot()) == 1
    assert len(asked) == desk_module.MAX_PER_PASS


def test_a_shutdown_waits_from_before_the_claim_until_the_story_is_stored(
    rows: Rows,
    storage: Storage,
    tmp_path: Path,
    writer: Writer,
    wall: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    incident = fire(rows)
    claiming, go = threading.Event(), threading.Event()
    claim = desk.stories.claim

    def slow_claim(*given: Any) -> bool:
        claiming.set()
        assert go.wait(5)
        return claim(*given)

    monkeypatch.setattr(desk.stories, "claim", slow_claim)
    made: list[Any] = []
    thread = threading.Thread(target=lambda: made.append(desk.refresh(incident, at(6))))
    thread.start()
    try:
        assert claiming.wait(5)
        # The claim is about to be made. A shutdown that closed the file now would
        # leave it behind, for lines nobody paid for: `drain` says work is out.
        assert desk.drain(0.05) is False
    finally:
        go.set()
        thread.join(5)

    assert desk.drain(1) is True
    assert made[0] is not None
    assert desk.latest(incident) == (made[0], True)


def test_a_refresh_that_comes_after_the_desk_is_closed_claims_nothing(
    rows: Rows,
    storage: Storage,
    tmp_path: Path,
    writer: Writer,
    wall: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_of(tmp_path, storage, writer, wall)
    incident = fire(rows)
    claimed: list[Any] = []
    claim = desk.stories.claim

    def seen(*given: Any) -> bool:
        claimed.append(given)
        return claim(*given)

    monkeypatch.setattr(desk.stories, "claim", seen)
    released = threading.Event()
    release = desk.stories.release

    def too_late(*given: Any) -> None:
        # Between a shutdown's `drain` and this, the stories file is closed, and a
        # claim can no longer be given up.
        released.set()
        desk.stories.close()
        release(*given)

    monkeypatch.setattr(desk.stories, "release", too_late)

    # The desk is closed and has drained; the file is still open. A pass that was
    # under way gets to its next incident only now.
    desk.close()
    assert desk.drain(0.1) is True
    with pytest.raises(EmberClosedError, match="story desk is closed"):
        desk.refresh(incident, at(6))

    # It made no claim, so there was none to be stranded by the file closing.
    assert (claimed, released.is_set(), writer.asked) == ([], False, 0)
    again = desk_of(tmp_path, storage, writer, wall)
    assert again.refresh(incident, at(6.1)) is not None


def test_a_live_desk_pays_only_under_its_own_scope(storage: Storage, tmp_path: Path) -> None:
    class Live:
        mode = "live"
        scope = None

    with pytest.raises(ValueError, match="story:merge"):
        StoryDesk(Live(), stories(tmp_path))  # type: ignore[arg-type]


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
    closure_headline = ingest.store.search(kinds=["traffic"])[-1].data["headline"]
    story = client.get(f"/api/incidents/{fire_id}").json()["incident"]["story"]
    assert (story["model"], story["stand_in"], story["current"]) == ("fake", True, True)
    assert [(claim["kind"], claim["text"]) for claim in story["claims"]] == [
        ("status", "The radio said: crews at work."),
        ("alarm", "The radio said: alarm level 3."),
        (
            "evacuation",
            EVACUATION_SAID,
        ),
        ("closure", f"511 updated: {closure_headline}"),
        ("aircraft", "A helicopter (MOCKH1) circled nearby for about 13 min, as low as 320 m."),
    ]
    assert "headline" not in story
    evacuation, closure = story["claims"][2], story["claims"][3]
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


def test_shutting_down_admits_no_paid_work_before_it_waits_for_anything(
    rows: Rows, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = fire(rows)
    app = create_app()
    seen: list[str] = []

    async def sweeping(_taps: Any, stop: asyncio.Event) -> None:
        # The first thing a shutdown waits for, and the wait can be a long one.
        await stop.wait()
        try:
            app.state.stories.refresh(incident, at(6))
            seen.append("a story was paid for")
        except EmberClosedError:
            seen.append("refused")

    monkeypatch.setattr(main_module, "_sweep_tap_media", sweeping)
    monkeypatch.setattr(main_module, "STORY_SECONDS", 3600)
    with TestClient(app):
        pass

    assert seen == ["refused"]


def test_the_pollers_are_closed_to_paid_work_when_the_story_desk_could_not_be(
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed: list[str] = []

    class Desk:
        def close(self) -> None:
            raise RuntimeError("Jane Doe")

    class Pollers:
        def close_paid(self) -> None:
            closed.append("pollers")

    main_module._close_paid(Desk(), None, Pollers())  # type: ignore[arg-type]
    main_module._close_paid(None, None, None)

    assert closed == ["pollers"]
    assert "Paid work could not be closed (RuntimeError)." in caplog.text
    assert "Jane" not in caplog.text


def test_a_pass_that_does_not_end_does_not_keep_the_server_from_stopping(
    storage: Storage,
    tmp_path: Path,
    writer: Writer,
    wall: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(main_module, "STOP_SECONDS", 0.05)
    desk = desk_of(tmp_path, storage, writer, wall)
    stuck = threading.Event()

    async def stopping() -> float:
        telling = asyncio.create_task(asyncio.to_thread(stuck.wait, 20))
        await asyncio.sleep(0.05)
        began = time.monotonic()
        try:
            await main_module._stop_stories(desk, telling)
            return time.monotonic() - began
        finally:
            stuck.set()

    with caplog.at_level(logging.WARNING):
        took = asyncio.run(stopping())

    assert took < 5
    assert "had not ended when the server shut down" in caplog.text
    # Whatever the pass does next, it writes nothing: the file is closed.
    with pytest.raises(StoriesError, match="closed"):
        desk.stories.save_state({})
