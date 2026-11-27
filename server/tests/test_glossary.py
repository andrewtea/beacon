"""`learn_glossary`: a table of counts in, a role from a closed list for each code out."""

import json
from pathlib import Path

import pytest

from beacon.ember import EmberClient, Script, Storage, fake_response
from beacon.glossary import (
    BUILT_IN,
    ROLE_WORDS,
    ROLES,
    Pattern,
    learn_glossary,
    stand_in_learner,
)
from beacon.glossary.learn import read_form, schema


def pattern(code: str, incidents: int = 20, **more: object) -> Pattern:
    given = {
        "units": 4,
        "by_category": (("alarm", incidents),),
        "alone": 2,
        "mean_units": 3.0,
    } | more
    return Pattern(code=code, incidents=incidents, **given)  # type: ignore[arg-type]


def fake(tmp_path: Path, *answers: object) -> tuple[EmberClient, Script]:
    script = Script([fake_response(text=json.dumps(answer)) for answer in answers])
    return EmberClient.fake(Storage.under(tmp_path), script), script


def test_ember_is_sent_counts_and_tags_and_answers_with_a_role_from_the_list(
    tmp_path: Path,
) -> None:
    ember, script = fake(
        tmp_path,
        {"entries": [{"code": "RC", "role": "rescue_captain"}, {"code": "E", "role": "engine"}]},
    )
    asked = [
        pattern("E", 300, by_category=(("medical response", 200), ("alarm", 100))),
        pattern("RC", 30, alone=0, mean_units=4.2),
    ]

    glossary = learn_glossary(ember, asked)

    # In the order the codes were asked about, whatever order the answer had.
    assert [(entry.code, entry.role) for entry in glossary.entries] == [
        ("E", "engine"),
        ("RC", "rescue_captain"),
    ]
    assert (glossary.whole, glossary.left_out, glossary.asked) == (True, 0, ("E", "RC"))
    # A stand-in answered, and the glossary says so; it has no cost to report.
    assert (glossary.model, glossary.stand_in, glossary.cost_nanousd) == ("fake", True, None)
    (request,) = script.requests
    # What was sent is the table and nothing else: numbers, and tags from a closed list.
    assert json.loads(request["input"]) == {
        "codes": [
            {
                "code": "E",
                "units": 4,
                "incidents": 300,
                "by_category": {"medical response": 200, "alarm": 100},
                "alone": 2,
                "mean_units": 3.0,
            },
            {
                "code": "RC",
                "units": 4,
                "incidents": 30,
                "by_category": {"alarm": 30},
                "alone": 0,
                "mean_units": 4.2,
            },
        ]
    }
    # The form has a code that was asked about and a role from the list, and no
    # field a sentence could be written in.
    form = request["text"]["format"]["schema"]
    assert form == schema(["E", "RC"])
    entry = form["properties"]["entries"]["items"]
    assert set(entry["properties"]) == {"code", "role"}
    assert entry["properties"]["code"]["enum"] == ["E", "RC"]
    assert entry["properties"]["role"]["enum"] == [*ROLES, "unknown"]
    assert entry["additionalProperties"] is False


def test_an_entry_that_is_not_what_was_asked_for_is_left_out_and_counted_never_mended() -> None:
    answer = {
        "entries": [
            {"code": "E", "role": "engine"},
            # A code nobody asked about, a role that is on no list, a second entry
            # for a code, an entry with a note, and one that is no entry.
            {"code": "ZZ", "role": "truck"},
            {"code": "T", "role": "ladder company"},
            {"code": "E", "role": "truck"},
            {"code": "B", "role": "chief", "note": "Battalion chiefs lead."},
            "RC is a rescue captain",
            {"code": "RC", "role": "unknown"},
        ]
    }

    glossary = read_form(json.dumps(answer), ["E", "T", "B", "RC"], model="live")

    assert [(entry.code, entry.role) for entry in glossary.entries] == [("E", "engine")]
    assert (glossary.whole, glossary.left_out) == (True, 5)
    # A code answered "unknown" has no entry, and neither has one not answered.
    assert (glossary.role_of("RC"), glossary.role_of("T"), glossary.role_of("B")) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "text",
    [
        "The codes are: E engine, T truck.",
        json.dumps(["E"]),
        json.dumps({"entries": "E"}),
        json.dumps({"entries": [], "summary": "Engines go to everything."}),
    ],
)
def test_an_answer_that_is_not_the_whole_form_is_no_glossary(text: str) -> None:
    glossary = read_form(text, ["E"], model="live")

    assert (glossary.whole, glossary.entries, glossary.left_out) == (False, (), 0)


def test_codes_that_are_not_ones_to_ask_about_are_refused_before_ember_is_asked(
    tmp_path: Path,
) -> None:
    ember, script = fake(tmp_path)

    for asked in (
        [],
        [pattern("E"), pattern("E")],
        [pattern("E36")],
        [pattern("e")],
        [pattern("SCRT")],
        [pattern(chr(65 + n // 26) + chr(65 + n % 26)) for n in range(41)],
    ):
        with pytest.raises(ValueError, match=r"A glossary is learned for|A code is one to"):
            learn_glossary(ember, asked)
    assert script.requests == []


def test_the_stand_in_guesses_by_a_rule_of_thumb_and_gives_no_kind_for_a_code_seldom_named(
    tmp_path: Path,
) -> None:
    ember = EmberClient.fake(Storage.under(tmp_path), stand_in_learner)
    asked = [
        pattern("M", 300, by_category=(("medical response", 295), ("fire", 5))),
        pattern("E", 300, by_category=(("medical response", 200), ("alarm", 100))),
        pattern("B", 100, alone=1, mean_units=3.5),
        pattern("T", 100, alone=30, mean_units=3.0),
        # A private ambulance, which the rule takes for a medic unit: it is wrong
        # for whole kinds of unit, and the comparison with DataSF then shows it.
        pattern("AM", 90, by_category=(("medical response", 90),)),
        pattern("HT", 1),
    ]

    glossary = learn_glossary(ember, asked)

    assert {entry.code: entry.role for entry in glossary.entries} == {
        "M": "medic",
        "E": "engine",
        "B": "chief",
        "T": "truck",
        "AM": "medic",
    }
    assert (glossary.role_of("HT"), glossary.stand_in) == (None, True)


def test_every_role_has_its_words_and_the_codes_a_person_wrote_down_have_roles() -> None:
    assert set(ROLE_WORDS) == set(ROLES)
    assert set(BUILT_IN.values()) <= set(ROLES)
    assert set(BUILT_IN) == {"E", "T", "B", "D", "M"}
