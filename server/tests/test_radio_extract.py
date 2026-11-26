"""`extract_transmission`: what is asked of Ember, and what is kept of its answer."""

import base64
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from beacon.ember import EmberClient, EmberError, ReplayMissError, Script, Storage, fake_response
from beacon.radio import ExtractionError, Transmission, extract_transmission, read_form
from beacon.radio.audio import pcm_to_wav
from beacon.radio.extract import INSTRUCTIONS, SCHEMA
from beacon.radio.vocabulary import CATEGORIES, DETAILS, NOTHING_KNOWN, STATUSES, Known

WAV = pcm_to_wav(b"\x01\x02" * 1600)
# The streets and the units these tests' calls may name.
KNOWN = Known.of(
    streets=["Hayes St", "Gough St", "19th Ave", "O'Farrell St", "Broadway"],
    units=["E36", "T6", "B2", "M52", "RS1"],
)

# What a call can hold that must not be kept, each in a form a model could write.
PRIVATE = ("Jane", "Doe", "555-0100", "chest pain", "unconscious", "year old", "overdose")
# The house number and the age, which are looked for only in what is kept of the
# call: a short number also turns up by chance in a file's hashes and times.
PRIVATE_NUMBERS = ("1458", "74")


def form(**changes: Any) -> dict[str, Any]:
    filled = {
        "clarity": "clear",
        "category": "fire",
        "status": "working",
        "details": ["working_fire"],
        "units": ["E36", "T6"],
        "alarm": 2,
        "street": "Hayes St",
        "block": 400,
        "cross_street": "Gough St",
    }
    return filled | changes


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


def hear(storage: Storage, answer: Any, **options: Any) -> tuple[Transmission, Script]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    script = Script([fake_response(text=text)])
    options = {"known": KNOWN} | options
    return extract_transmission(EmberClient.fake(storage, script), WAV, "wav", **options), script


def everything_on_disk(root: Path) -> str:
    """Every file under a directory, and every text column of every SQLite table."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        found.append(path.read_bytes().decode("latin-1"))
        if path.suffix == ".sqlite3":
            with sqlite3.connect(path) as db:
                tables = [name for (name,) in db.execute("SELECT name FROM sqlite_master")]
                for table in tables:
                    try:
                        found.append(json.dumps(db.execute(f"SELECT * FROM {table}").fetchall()))
                    except sqlite3.Error:
                        continue
    return "\n".join(found)


def test_ember_is_handed_the_audio_and_a_form_at_low_effort(storage: Storage) -> None:
    heard, script = hear(storage, form())

    (request,) = script.requests
    (message,) = request["input"]
    question, audio = message["content"]
    assert question["type"] == "input_text"
    assert audio == {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(WAV).decode(), "format": "wav"},
    }
    assert request["instructions"] == INSTRUCTIONS
    assert request["reasoning"] == {"effort": "low"}
    assert request["max_output_tokens"] == 512
    assert request["text"]["format"] == {
        "type": "json_schema",
        "name": "transmission",
        "strict": True,
        "schema": SCHEMA,
    }
    assert heard == Transmission(
        clarity="clear",
        category="fire",
        status="working",
        details=("working_fire",),
        units=("E36", "T6"),
        alarm=2,
        street="Hayes St",
        block=400,
        cross_street="Gough St",
        heard_by="fake",
    )
    assert (heard.event, heard.place) == ("fire", "400 block of Hayes St")


def test_the_form_offers_only_the_closed_vocabulary_and_no_place_for_free_text() -> None:
    properties = SCHEMA["properties"]

    assert properties["category"]["enum"] == list(CATEGORIES)
    assert properties["status"]["enum"] == list(STATUSES)
    assert properties["details"]["items"]["enum"] == list(DETAILS)
    assert sorted(SCHEMA["required"]) == sorted(properties)
    assert SCHEMA["additionalProperties"] is False
    # Nothing in it asks for what was said: no transcript, summary, or note.
    assert not {"transcript", "text", "summary", "notes", "speaker"} & set(properties)


def test_every_call_is_a_sensitive_one_and_leaves_none_of_its_text_on_disk(
    storage: Storage, tmp_path: Path
) -> None:
    # A model can ignore the form's rules. This one writes what it heard wherever
    # the form takes a string, and adds a field the form does not have.
    heard, _script = hear(
        storage,
        form(
            category="medical",
            units=["M52", "Jane Doe", "E36 for the unconscious 74 year old"],
            street="1458 Hayes St",
            block=1458,
            cross_street="Jane Doe, chest pain, 415-555-0100",
            details=["rescue_in_progress", "overdose"],
            transcript="Medic 52, 1458 Hayes Street, Jane Doe, 74, unconscious",
        ),
    )

    assert heard.as_data() == {
        "event": "medical response",
        "category": "medical",
        "clarity": "clear",
        # Of a medical call the crews and the block are kept, and nothing else.
        "status": "unknown",
        "details": [],
        "units": ["M52"],
        "alarm": None,
        "street": "Hayes St",
        "block": 1400,
        "cross_street": None,
    }
    assert heard.place == "1400 block of Hayes St"
    kept = json.dumps(heard.as_data()) + repr(heard)
    on_disk = everything_on_disk(tmp_path)
    for word in PRIVATE:
        assert word not in kept
        assert word not in on_disk
    for number in PRIVATE_NUMBERS:
        assert number not in kept
    # The audio is not on disk either, whole or as it was sent.
    assert base64.b64encode(WAV).decode()[:200] not in on_disk
    with sqlite3.connect(storage.ledger_db) as db:
        ((task, source, request, response),) = db.execute(
            "SELECT task, source, request, response FROM ember_calls"
        )
    assert (task, source) == ("extract_transmission", "fake")
    assert json.loads(request)["withheld"] == json.loads(response)["withheld"] == "sensitive"
    # Never recorded, so never replayed.
    assert not storage.replay_dir.exists() or not any(storage.replay_dir.iterdir())


@pytest.mark.parametrize(
    ("changes", "kept"),
    [
        # A category, status, or clarity the vocabulary does not have.
        ({"category": "shooting"}, {"category": "other", "event": "other"}),
        ({"status": "Jane Doe is on scene"}, {"status": "unknown"}),
        ({"clarity": 7}, {"clarity": "partial"}),
        # Details are kept once each, and only the ones on the list.
        (
            {"details": ["evacuation", "evacuation", "patient is 74", 3, None, "road_closed"]},
            {"details": ["evacuation", "road_closed"]},
        ),
        ({"details": "evacuation"}, {"details": []}),
        # Units in their one form, once each.
        (
            {"units": ["Engine 36", "e36", "T06", "B000", {"unit": "M52"}, "Jane"]},
            {"units": ["E36", "T6"]},
        ),
        ({"units": None}, {"units": []}),
        ({"alarm": 9}, {"alarm": None}),
        ({"alarm": True}, {"alarm": None}),
        ({"alarm": "2"}, {"alarm": None}),
        # A house number is kept as its block, wherever it was put.
        ({"block": 458}, {"block": 400}),
        ({"street": "458 hayes street", "block": None}, {"street": "Hayes St", "block": None}),
        ({"block": "458"}, {"block": None}),
        # Without a street, a block and a cross street say nothing.
        (
            {"street": "the yellow house on Hayes"},
            {"street": None, "block": None, "cross_street": None},
        ),
        ({"cross_street": "Gough at Oak"}, {"cross_street": None}),
    ],
)
def test_a_field_that_does_not_fit_the_vocabulary_is_left_empty(
    changes: dict[str, Any], kept: dict[str, Any]
) -> None:
    heard = read_form(form(**changes), heard_by="fake", known=KNOWN)

    assert {name: heard.as_data()[name] for name in kept} == kept


@pytest.mark.parametrize(
    ("changes", "kept"),
    [
        # Shaped like units, and a reading, a name with an age, and an age with a sex.
        ({"units": ["BP 180", "Ann 72", "M72", "E36"]}, {"units": ["E36"]}),
        # Shaped like streets: a house number spelled out, and a person's name.
        (
            {"street": "One Two Three Hayes St"},
            {"street": None, "block": None, "cross_street": None},
        ),
        ({"street": "Jane Doe St"}, {"street": None, "block": None, "cross_street": None}),
        ({"cross_street": "Jane Doe St"}, {"street": "Hayes St", "cross_street": None}),
        # A real street of the city's that nobody listed is not kept either.
        ({"street": "Fell St"}, {"street": None}),
    ],
)
def test_a_unit_or_a_street_that_is_not_on_the_lists_is_not_kept(
    changes: dict[str, Any], kept: dict[str, Any]
) -> None:
    heard = read_form(form(**changes), heard_by="live", known=KNOWN)

    assert {name: heard.as_data()[name] for name in kept} == kept


def test_with_no_lists_no_unit_and_no_street_is_kept() -> None:
    heard = read_form(form(), heard_by="live", known=NOTHING_KNOWN)

    assert (heard.units, heard.street, heard.block, heard.cross_street, heard.place) == (
        (),
        None,
        None,
        None,
        None,
    )
    # What is from the fixed lists is kept as before.
    assert (heard.category, heard.status, heard.details, heard.alarm) == (
        "fire",
        "working",
        ("working_fire",),
        2,
    )


def test_a_form_with_fields_missing_is_read_as_far_as_it_goes() -> None:
    assert read_form({}, heard_by="live", known=KNOWN).as_data() == {
        "event": "other",
        "category": "other",
        "clarity": "partial",
        "status": "unknown",
        "details": [],
        "units": [],
        "alarm": None,
        "street": None,
        "block": None,
        "cross_street": None,
    }


@pytest.mark.parametrize(
    "changes", [{"clarity": "unintelligible"}, {"category": "unintelligible", "clarity": "clear"}]
)
def test_of_a_call_nobody_could_make_out_nothing_is_said_to_have_been_heard(
    changes: dict[str, Any],
) -> None:
    heard = read_form(form(**changes), heard_by="fake", known=KNOWN)

    assert heard == Transmission(
        clarity="unintelligible",
        category="unintelligible",
        status="unknown",
        details=(),
        units=(),
        alarm=None,
        street=None,
        block=None,
        cross_street=None,
        heard_by="fake",
    )
    assert heard.place is None


def test_every_call_on_an_ems_channel_is_a_medical_response_whatever_ember_made_of_it(
    storage: Storage,
) -> None:
    heard, _script = hear(storage, form(category="fire"), ems_channel=True)

    assert (heard.event, heard.category) == ("medical response", "medical")
    assert (heard.status, heard.details, heard.alarm) == ("unknown", (), None)
    assert (heard.units, heard.place) == (("E36", "T6"), "400 block of Hayes St")

    unheard = read_form(
        form(clarity="unintelligible"), heard_by="fake", known=KNOWN, ems_channel=True
    )
    assert (unheard.event, unheard.units, unheard.place) == ("medical response", (), None)


def _while_handling(error: Exception, call: Callable[[], object]) -> None:
    try:
        raise error
    except type(error):
        call()


@pytest.mark.parametrize(
    "answer",
    [
        "Medic 52 to 1458 Hayes Street for Jane Doe",
        '["Jane Doe"]',
        '"Jane Doe"',
        "",
        "{",
        # An object that is not the form: it says nothing about the call, and is
        # not kept as a call in which nothing was heard.
        "{}",
        '{"clarity": "clear", "category": "fire"}',
        '{"transcript": "Jane Doe"}',
    ],
)
def test_an_answer_that_is_not_a_form_is_an_error_that_repeats_none_of_it(
    storage: Storage, answer: str
) -> None:
    with pytest.raises(ExtractionError) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: hear(storage, answer))

    assert str(failure.value) == "Ember's answer about a radio call was not a filled-in form."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)


def test_an_answer_cut_short_by_the_output_limit_is_not_read(storage: Storage) -> None:
    cut_short = fake_response(text=json.dumps(form()), status="incomplete")
    ember = EmberClient.fake(storage, Script([cut_short]))

    with pytest.raises(ExtractionError):
        extract_transmission(ember, WAV, "wav", known=KNOWN)


def test_a_client_that_only_replays_cannot_hear_a_call(storage: Storage) -> None:
    # A sensitive call is never recorded, so there is never anything to replay.
    with pytest.raises(ReplayMissError):
        extract_transmission(EmberClient.replay_only(storage), WAV, "wav", known=KNOWN)


def test_a_failure_of_embers_is_one_of_the_clients_own_with_a_fixed_text(
    storage: Storage,
) -> None:
    def fails(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("could not hear Jane Doe at 1458 Hayes")

    with pytest.raises(EmberError) as failure:
        extract_transmission(EmberClient.fake(storage, fails), WAV, "wav", known=KNOWN)

    for word in PRIVATE:
        assert word not in str(failure.value)
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)
