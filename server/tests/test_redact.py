"""The redaction pass: which texts a person is shown, and which are left out whole."""

import pytest

from beacon.redact import MAX_TEXT, NO_NAMES, ORDER_WORDS, PROPER, WORDS, Names, own, reading
from beacon.scenario import INVENTED
from beacon.scenario.notices import NOTICES

DESCRIBES_A_PERSON = (
    "man woman child boy girl old age aged year years mr mrs ms dr injured injury "
    "hurt burns died dead death hospital patient victim body name named wife husband "
    "son daughter mother father family he she his her him"
)

NAMES = Names.of(
    streets=("Hayes St", "Gough St", "Octavia St", "Franklin St", "Van Ness Ave"),
    units=("E36", "T6"),
    places=("Civic Center Plaza",),
)


@pytest.mark.parametrize(
    "text",
    [
        "Leave now, on foot, toward Franklin St. Do not use Hayes St. A reception "
        "point is open at Civic Center Plaza.",
        "Residents may return, except to 400 through 450 Hayes St, which stay closed "
        "until the Fire Department has inspected them.",
        "Both sides of Hayes St between Gough St and Octavia St",
        f"{INVENTED} The evacuation order for Hayes St between Gough St and Octavia St is lifted.",
        "Hayes St. is closed between Gough St. and Van Ness Ave until 6:30 pm on the 3rd.",
        "A shelter-in-place order is in effect. Call 415-555-0100 for information.",
    ],
)
def test_a_notice_in_plain_words_about_listed_places_passes_as_a_reading(text: str) -> None:
    assert reading(text, NAMES) == text


@pytest.mark.parametrize(
    "text",
    [
        # A person, by a name that is no word.
        "Jane Doe, 67, of 432 Hayes St, was taken to a hospital with burns.",
        "Residents of Hayes St were told by Jane to leave.",
        # A name that is also a word, in the middle of a sentence and in capitals.
        "The building of resident Will was closed.",
        "The order was issued by MAY.",
        # Who somebody is, and what happened to them.
        "A 67-year-old was taken from the building.",
        "One resident was injured.",
        # A street nobody listed, and a part of a listed name.
        "Leave toward Laguna St.",
        "The fire on Hayes is out.",
        # An address written out of a listed name's reach, and other things no
        # plain notice holds.
        "Write to jane@example.org for information.",
        "See https://example.org/order for the map.",
        "The unit 3B is closed.",
        "Résidents may return.",
        "",
        "   ",
    ],
)
def test_a_text_that_names_or_describes_a_person_or_an_unlisted_place_is_left_out(
    text: str,
) -> None:
    assert reading(text, NAMES) is None
    assert own(text, NAMES) is None


def test_a_text_is_kept_whole_or_left_out_and_never_cut() -> None:
    kept = "The fire on the 400 block of Hayes St is out."
    assert reading(f"{kept} Jane Doe was taken away.", NAMES) is None
    # White space is made single spaces, and nothing else is changed.
    assert reading(f"  {kept}\n", NAMES) == kept
    assert reading("x" * (MAX_TEXT + 1), NAMES) is None
    assert reading(None, NAMES) is None
    assert reading(["The fire is out."], NAMES) is None


@pytest.mark.parametrize(
    "text",
    [
        "Leave now.",
        "You must go toward Franklin St.",
        "Residents should stay indoors.",
        "Stay away from Hayes St.",
        "People may return to the area.",
        "Avoid the area.",
        "Please wait for the all-clear.",
        "Do not enter the building.",
    ],
)
def test_beacons_own_voice_never_tells_a_reader_what_to_do(text: str) -> None:
    assert reading(text, NAMES) == text
    assert own(text, NAMES) is None


def test_beacons_own_voice_names_a_block_and_never_a_house() -> None:
    assert own("Crews are at work at the 400 block of Hayes St.", NAMES) is not None
    assert own("E36 and T6 are on scene at Hayes St at Gough St.", NAMES) is not None
    assert own("An authority published a shelter-in-place order.", NAMES) is not None
    assert own("Crews are at work at 432 Hayes St.", NAMES) is None
    assert own("Crews are at work at 432, Hayes St.", NAMES) is not None  # no house: a count
    assert own("For information, the number is 4155550100.", NAMES) is None
    # An order names the addresses it is for, and its reading may.
    assert reading("400 through 450 Hayes St stay closed.", NAMES) is not None


def test_a_listed_name_passes_only_as_a_whole_phrase() -> None:
    assert own("Fire on Hayes St.", NAMES) is not None
    assert own("Fire on hayes st.", NAMES) is not None
    assert own("Fire on Hayes Street.", NAMES) is None
    assert own("Fire on Hayes Sts.", NAMES) is None
    # With no list, only the public bodies Beacon knows itself are names.
    assert own("The Fire Department is on scene.", NO_NAMES) is not None
    assert own("Fire on Hayes St.", NO_NAMES) is None
    assert own("E36 is on scene.", NO_NAMES) is None


def test_an_issuer_is_shown_only_when_it_is_a_listed_public_body_and_nothing_else() -> None:
    assert NAMES.public_body(" San Francisco  Fire Department ") == "San Francisco Fire Department"
    assert NAMES.public_body("Civic Center Plaza") == "Civic Center Plaza"
    assert NAMES.public_body("San Francisco Fire Department, Chief Jane Doe") is None
    assert NAMES.public_body("Jane Doe") is None
    # A street and a unit are names, and are nobody who issues a notice.
    assert NAMES.public_body("Hayes St") is None
    assert NAMES.public_body("E36") is None
    assert NAMES.public_body(None) is None


def test_no_word_of_an_instruction_is_among_beacons_own_and_none_describes_a_person() -> None:
    assert not WORDS & ORDER_WORDS
    for word in ("leave", "stay", "go", "must", "should", "evacuate", "you", "your", "avoid"):
        assert word in ORDER_WORDS
    for word in DESCRIBES_A_PERSON.split():
        assert word not in WORDS | ORDER_WORDS | PROPER, word


def test_the_scenarios_public_notices_pass_and_its_press_release_does_not() -> None:
    order, clear, release = (notice.read for notice in NOTICES)
    names = Names.of(
        streets=("Hayes St", "Gough St", "Octavia St", "Franklin St"),
        places=("Civic Center Plaza",),
    )
    for form in (order, clear):
        assert reading(form["instruction"], names) is not None
        assert reading(form["summary"], names) is not None
        assert all(reading(area, names) is not None for area in form["areas"])
    # What a model might write of a press release, against its instructions.
    assert reading(release["summary"], names) is None
    assert reading(release["title"], names) is None
    assert all(reading(area, names) is None for area in release["areas"])
