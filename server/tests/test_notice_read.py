"""`read_notice`: what is asked of Ember about a document, and what is kept of its answer."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from beacon.ember import EmberClient, EmberError, ReplayMissError, Script, Storage, fake_response
from beacon.notice import (
    HAZARDS,
    MAX_PDF_BYTES,
    NOTICE_TYPES,
    ORDERS,
    TEXT_TYPES,
    Notice,
    NoticeError,
    is_pdf,
    read_form,
    read_notice,
)
from beacon.notice.read import INSTRUCTIONS, SCHEMA
from beacon.scenario.pdf import text_pdf
from tests.test_radio_extract import everything_on_disk

# An invented document. Nothing in these tests was ever issued.
PDF = text_pdf(["EVACUATION ORDER", "Invented for a test.", "Leave Hayes St now."])

# What a document can hold that must not be kept of a press release.
PRIVATE = ("Jane", "Doe", "burns", "432", "67")


def form(**changes: Any) -> dict[str, Any]:
    filled = {
        "notice_type": "evacuation_order",
        "hazard": "fire",
        "issuer": "San Francisco Department of Emergency Management",
        "title": "Evacuation Order: 400 block of Hayes St",
        "issued_at": "2026-11-27T12:19:00-08:00",
        "effective_until": None,
        "areas": ["Both sides of Hayes St between Gough St and Octavia St"],
        "streets": ["Hayes St", "Gough St", "Octavia St"],
        "instruction": "Leave now, on foot, toward Franklin St.",
        "summary": "Everyone on the block is ordered to leave.",
    }
    return filled | changes


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


def read(storage: Storage, answer: Any, pdf: bytes = PDF) -> tuple[Notice, Script]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    script = Script([fake_response(text=text)])
    return read_notice(EmberClient.fake(storage, script), pdf), script


def _while_handling(error: Exception, call: Callable[[], object]) -> None:
    try:
        raise error
    except type(error):
        call()


def test_ember_is_handed_the_document_itself_and_a_form_at_low_effort(storage: Storage) -> None:
    notice, script = read(storage, form())

    (request,) = script.requests
    (message,) = request["input"]
    question, document = message["content"]
    assert question["type"] == "input_text"
    # The bytes, and never an address for Ember to fetch.
    assert document == {
        "type": "input_file",
        "filename": "notice.pdf",
        "file_data": "data:application/pdf;base64," + base64.b64encode(PDF).decode(),
    }
    assert request["instructions"] == INSTRUCTIONS
    assert request["reasoning"] == {"effort": "low"}
    assert request["max_output_tokens"] == 1500
    assert request["text"]["format"] == {
        "type": "json_schema",
        "name": "notice",
        "strict": True,
        "schema": SCHEMA,
    }
    assert notice == Notice(
        notice_type="evacuation_order",
        hazard="fire",
        issuer="San Francisco Department of Emergency Management",
        title="Evacuation Order: 400 block of Hayes St",
        issued_at=datetime(2026, 11, 27, 12, 19, tzinfo=timezone(timedelta(hours=-8))),
        effective_until=None,
        areas=("Both sides of Hayes St between Gough St and Octavia St",),
        streets=("Hayes St", "Gough St", "Octavia St"),
        instruction="Leave now, on foot, toward Franklin St.",
        summary="Everyone on the block is ordered to leave.",
        read_by="fake",
    )
    assert notice.is_order
    assert notice.as_data()["issued_at"] == "2026-11-27T12:19:00-08:00"
    assert json.loads(json.dumps(notice.as_data())) == notice.as_data()


def test_the_form_names_its_types_from_closed_lists_and_asks_for_every_field() -> None:
    properties = SCHEMA["properties"]

    assert properties["notice_type"]["enum"] == list(NOTICE_TYPES)
    assert properties["hazard"]["enum"] == list(HAZARDS)
    assert sorted(SCHEMA["required"]) == sorted(properties)
    assert SCHEMA["additionalProperties"] is False
    # What tells people to do something, and what is written to be repeated.
    assert ORDERS < TEXT_TYPES < set(NOTICE_TYPES)
    assert {"press_release", "other"} == set(NOTICE_TYPES) - TEXT_TYPES


def test_every_reading_is_a_sensitive_call_and_leaves_none_of_its_text_on_disk(
    storage: Storage, tmp_path: Path
) -> None:
    # A document at an address somebody listed can be anything, so neither it nor
    # what Ember made of it is in the ledger or the trace.
    notice, _script = read(
        storage,
        form(
            summary="Jane Doe, 67, of 432 Hayes St, was taken to a hospital with burns.",
            note="A field the form does not have, about Jane Doe.",
        ),
    )

    assert notice.summary is not None
    on_disk = everything_on_disk(tmp_path)
    for word in ("Jane", "Doe", "burns", "Evacuation Order", "Leave now", "Invented for a test"):
        assert word not in on_disk
    assert base64.b64encode(PDF).decode()[:60] not in on_disk
    assert "note" not in notice.as_data()


@pytest.mark.parametrize("notice_type", ["press_release", "other"])
def test_of_what_is_not_a_notice_to_the_public_no_text_is_kept(
    storage: Storage, notice_type: str
) -> None:
    # A press release is where a victim is named and a home described. A model
    # told not to repeat that can repeat it all the same.
    notice, _script = read(
        storage,
        form(
            notice_type=notice_type,
            issuer="San Francisco Fire Department",
            title="Three-alarm fire on Hayes St is out; Jane Doe hospitalized",
            areas=["432 Hayes St, the home of Jane Doe"],
            streets=["432 Hayes St", "Hayes at Gough", "Jane Doe", "Gough St"],
            instruction="Send flowers to Jane Doe.",
            summary="Jane Doe, 67, of 432 Hayes St, was taken to a hospital with burns.",
        ),
    )

    assert notice.as_data() == {
        "notice_type": notice_type,
        "hazard": "fire",
        "issuer": "San Francisco Fire Department",
        "title": None,
        "issued_at": "2026-11-27T12:19:00-08:00",
        "effective_until": None,
        "areas": [],
        # Names of streets in one shape, without numbers, and nothing else.
        "streets": ["Hayes St", "Gough St"],
        "instruction": None,
        "summary": None,
    }
    assert not notice.is_order
    kept = json.dumps(notice.as_data()) + repr(notice)
    for word in PRIVATE:
        assert word not in kept


def test_an_orders_own_words_are_kept_on_one_line_and_cut_to_a_length(storage: Storage) -> None:
    notice, _script = read(
        storage,
        form(
            notice_type="shelter_in_place",
            title="Shelter\nin place:\x00 smoke\t\tfrom a fire",
            areas=["  Hayes Valley  ", "Hayes Valley", "", 7, "x" * 500, *["A", "B"] * 20],
            instruction="Stay inside. " * 200,
            summary=None,
        ),
    )

    assert notice.title == "Shelter in place: smoke from a fire"
    assert notice.areas[:2] == ("Hayes Valley", "x" * 240)
    assert len(notice.areas) == 4
    assert notice.instruction is not None
    assert len(notice.instruction) <= 800
    assert notice.instruction.startswith("Stay inside. Stay inside.")
    assert notice.summary is None
    # An order's addresses stay in its own words: that is what an order is.
    order, _script = read(storage, form(areas=["400 through 450 Hayes St"]))
    assert order.areas == ("400 through 450 Hayes St",)


def test_a_field_that_does_not_fit_is_left_empty_or_taken_for_the_least() -> None:
    notice = read_form(
        {
            "notice_type": "evacuation",
            "hazard": ["fire"],
            "issuer": 12,
            "title": {"text": "An order"},
            # No zone, so no moment; and no date at all.
            "issued_at": "2026-11-27T12:19:00",
            "effective_until": "until further notice",
            "areas": "Hayes Valley",
            "streets": "Hayes St",
            "instruction": ["Leave"],
            "summary": 3,
        },
        read_by="live",
    )

    # What is not known to be a notice is `other`, of which no text is kept.
    assert notice == Notice(
        notice_type="other",
        hazard="other",
        issuer=None,
        title=None,
        issued_at=None,
        effective_until=None,
        areas=(),
        streets=(),
        instruction=None,
        summary=None,
        read_by="live",
    )


def test_a_time_is_kept_with_the_zone_the_notice_gave_it() -> None:
    notice = read_form(
        form(issued_at="2026-11-27T20:19:00Z", effective_until="2026-11-28T08:00:00-08:00"),
        read_by="fake",
    )

    assert notice.issued_at == datetime(2026, 11, 27, 20, 19, tzinfo=UTC)
    assert notice.effective_until == datetime(2026, 11, 28, 16, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "not_a_notice",
    [
        b"",
        b"<html>Jane Doe's page was not found</html>",
        b" %PDF-1.4 after a space",
        PDF + b"\x00" * (MAX_PDF_BYTES - len(PDF) + 1),
    ],
)
def test_what_is_not_a_pdf_of_a_notices_size_is_refused_and_ember_is_not_asked(
    storage: Storage, not_a_notice: bytes
) -> None:
    script = Script([fake_response(text=json.dumps(form()))])
    ember = EmberClient.fake(storage, script)

    with pytest.raises(NoticeError) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: read_notice(ember, not_a_notice))

    assert str(failure.value) == "A notice is a PDF file of at most five megabytes."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)
    assert script.requests == []
    assert is_pdf(PDF)
    assert not is_pdf(not_a_notice[:100]) or len(not_a_notice) > MAX_PDF_BYTES


@pytest.mark.parametrize(
    "answer",
    [
        "The document orders Jane Doe to leave 432 Hayes St.",
        "[]",
        '{"notice_type": "evacuation_order"}',
        json.dumps({name: None for name in SCHEMA["required"] if name != "summary"}),
        "[" * 100_000,
    ],
)
def test_an_answer_that_is_not_the_whole_form_is_an_error_that_repeats_none_of_it(
    storage: Storage, answer: str
) -> None:
    with pytest.raises(NoticeError) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: read(storage, answer))

    assert str(failure.value) == "Ember's answer about a notice was not a filled-in form."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)


def test_an_answer_cut_short_by_the_output_limit_is_not_read(storage: Storage) -> None:
    cut_short = fake_response(text=json.dumps(form()), status="incomplete")

    with pytest.raises(NoticeError):
        read_notice(EmberClient.fake(storage, Script([cut_short])), PDF)


def test_a_client_that_only_replays_cannot_read_a_notice(storage: Storage) -> None:
    # A sensitive call is never recorded, so there is never anything to replay.
    with pytest.raises(ReplayMissError):
        read_notice(EmberClient.replay_only(storage), PDF)


def test_a_failure_of_embers_is_one_of_the_clients_own_with_a_fixed_text(
    storage: Storage,
) -> None:
    def fails(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("could not read the order for Jane Doe at 432 Hayes")

    with pytest.raises(EmberError) as failure:
        read_notice(EmberClient.fake(storage, fails), PDF)

    for word in PRIVATE:
        assert word not in str(failure.value)
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)
