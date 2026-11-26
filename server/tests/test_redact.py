"""The redaction pass: which readings a person is shown, and which are left out whole."""

import pytest

from beacon.redact import ACRONYMS, DAYS, MAX_TEXT, MONTHS, NO_NAMES, WORDS, Names, reading
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
        # A date, and a public body by its acronym or with a possessive.
        "Issued Friday, November 27, 2026. Hayes St is closed until April 3 at 6 pm.",
        "BART and Muni are stopped near Hayes St. SFFD's crews are on scene.",
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
        # A name that is also a month, a day, or a public body's acronym.
        "The resident April was at the fire.",
        "Residents were told by June to leave.",
        "The building of resident Friday was closed.",
        "The resident Bart was at the fire.",
        "The resident Dem was at the fire.",
        # Who somebody is, and what happened to them.
        "A 67-year-old was taken from the building.",
        "One resident was injured.",
        # A street nobody listed, and a part of a listed name.
        "Leave toward Laguna St.",
        "The fire on Hayes is out.",
        # A name in another script, beside a listed name or alone.
        "The Fire Department 張三 is on scene.",
        "Hayes St張三 is closed.",
        # Other things no plain notice holds.
        "Write to jane@example.org for information.",
        "See https://example.org/order for the map.",
        "The unit 3B is closed.",
        "Résidents may return.",
        "The road ٣ is closed.",
        "",
        "   ",
    ],
)
def test_a_text_that_names_or_describes_a_person_or_an_unlisted_place_is_left_out(
    text: str,
) -> None:
    assert reading(text, NAMES) is None


def test_a_text_is_kept_whole_or_left_out_and_never_cut() -> None:
    kept = "The fire on the 400 block of Hayes St is out."
    assert reading(f"{kept} Jane Doe was taken away.", NAMES) is None
    # White space is made single spaces, and nothing else is changed.
    assert reading(f"  {kept}\n", NAMES) == kept
    assert reading("x" * (MAX_TEXT + 1), NAMES) is None
    assert reading(None, NAMES) is None
    assert reading(["The fire is out."], NAMES) is None


def test_a_month_passes_only_beside_a_number_and_a_day_only_before_a_date() -> None:
    assert reading("Closed until April 3.", NAMES) is not None
    assert reading("Closed from 3 May to 5 May.", NAMES) is not None
    assert reading("Closed until Friday, April 3.", NAMES) is not None
    assert reading("Closed until Friday April 3.", NAMES) is not None
    # With no number beside it, a month is somebody's name, and so is a day.
    assert reading("Closed until April.", NAMES) is None
    assert reading("Closed until Friday.", NAMES) is None
    assert reading("Closed until Friday, at 6 pm.", NAMES) is None
    assert reading("Closed until APRIL 3.", NAMES) is None
    assert reading("The building of April's family, 3 doors down.", NAMES) is None


def test_a_listed_name_passes_only_as_a_whole_phrase() -> None:
    assert reading("Fire on Hayes St.", NAMES) is not None
    assert reading("Fire on hayes st.", NAMES) is not None
    assert reading("Fire on Hayes Street.", NAMES) is None
    assert reading("Fire on Hayes Sts.", NAMES) is None
    # With no list, only the public bodies Beacon knows itself are names.
    assert reading("The Fire Department is on scene.", NO_NAMES) is not None
    assert reading("Fire on Hayes St.", NO_NAMES) is None
    assert reading("E36 is on scene.", NO_NAMES) is None


@pytest.mark.parametrize(
    "name",
    [
        # A name in another script with a word in this one among it: listing the
        # word that can be read would list `Lee`.
        "\u674e Lee \u516c\u5712",
        "Lee (the resident)",
        "Lee & Sons Plaza",
        "Lee, Jane",
        "Lee\u200b",
        "\u00c9cole Lee",
    ],
)
def test_a_listed_name_that_is_not_written_plainly_is_left_out_whole(name: str) -> None:
    for names, left_out in (
        Names.kept(places=[name]),
        Names.kept(streets=[name]),
        Names.kept(units=[name]),
    ):
        assert left_out == 1
        assert names == NO_NAMES
        assert names.public_body("Lee") is None
        assert reading("The crew of Lee was here.", names) is None
    assert Names.of(places=[name, "Civic Center Plaza"]) == Names.of(places=["Civic Center Plaza"])


def test_a_listed_name_written_plainly_is_kept_with_its_stops_and_hyphens() -> None:
    names, left_out = Names.kept(
        streets=["Hayes  St.", "O'Farrell St"],
        places=["St. Mary's Cathedral", "Pier 39", "Embarcadero-Ferry Plaza"],
    )

    assert left_out == 0
    assert reading("Go to St. Mary's Cathedral or Pier 39, by O'Farrell St.", names) is not None
    assert names.public_body("Embarcadero-Ferry Plaza") == "Embarcadero-Ferry Plaza"
    assert names.public_body("Ferry Plaza") is None


@pytest.mark.parametrize(
    "issuer",
    [
        "San Francisco Fire Department, Chief Jane Doe",
        "Jane Doe",
        # The private name is in another script, or behind punctuation: the name
        # of the body is all there may be.
        "Fire Department 張三",
        "Fire Department (Jane Doe)",
        "Fire Department!",
        "Fire Department\u200b",
        "Fire Department - 432",
        # A street, a unit, and Beacon itself are names, and issue no notice.
        "Hayes St",
        "E36",
        "Beacon",
        "",
        None,
    ],
)
def test_an_issuer_with_anything_beside_a_listed_bodys_name_is_left_out_whole(
    issuer: object,
) -> None:
    assert NAMES.public_body(issuer) is None


def test_an_issuer_that_is_a_listed_public_body_and_nothing_else_is_shown() -> None:
    assert NAMES.public_body(" San Francisco  Fire Department ") == "San Francisco Fire Department"
    assert NAMES.public_body("Civic Center Plaza") == "Civic Center Plaza"
    assert NAMES.public_body("fire department") == "fire department"


def test_no_word_on_the_lists_describes_a_person() -> None:
    lowered = {acronym.lower() for acronym in ACRONYMS}
    for word in DESCRIBES_A_PERSON.split():
        assert word not in WORDS | lowered | MONTHS | DAYS, word
    # An acronym passes in capitals only, and a month is no ordinary word, except
    # `may`, which a notice cannot be written without.
    assert not lowered & WORDS
    months_that_are_words = MONTHS & WORDS
    assert months_that_are_words == {"may"}
    assert not DAYS & WORDS


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
