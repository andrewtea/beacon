"""The closed vocabulary a radio call is kept in: what fits it, and what is dropped."""

import pytest

from beacon.radio.vocabulary import block_of, clean_street, clean_unit, place_in_words


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("E36", "E36"),
        ("t6", "T6"),
        ("T06", "T6"),
        ("RS-1", "RS1"),
        ("M 52", "M52"),
        ("Engine 36", "E36"),
        ("truck 6", "T6"),
        ("Battalion 2", "B2"),
        ("Medic 52", "M52"),
        ("E999", "E999"),
    ],
)
def test_a_unit_is_kept_in_one_form(said: str, kept: str) -> None:
    assert clean_unit(said) == kept


@pytest.mark.parametrize(
    "said",
    ["", "Jane", "Jane Doe", "E0", "B000", "E1000", "ABCD1", "36", "Engine", "E36 Jane", 5, None],
)
def test_what_is_not_a_type_code_and_a_number_is_not_a_unit(said: object) -> None:
    assert clean_unit(said) is None


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("Hayes St", "Hayes St"),
        ("hayes street", "Hayes St"),
        ("HAYES   STREET.", "Hayes St"),
        ("19TH AVENUE", "19th Ave"),
        ("3rd St", "3rd St"),
        ("O'FARRELL STREET", "O'Farrell St"),
        ("mcallister st", "McAllister St"),
        ("john f kennedy drive", "John F Kennedy Dr"),
        ("Cesar Chavez St", "Cesar Chavez St"),
        ("DR. CARLTON B GOODLETT PL", "Dr Carlton B Goodlett Pl"),
        ("St Josephs Ave", "St Josephs Ave"),
        ("the embarcadero", "The Embarcadero"),
        ("broadway", "Broadway"),
        ("us-101", "US-101"),
        ("Geary Blvd", "Geary Blvd"),
    ],
)
def test_a_street_is_kept_in_one_form(said: str, kept: str) -> None:
    assert clean_street(said) == kept


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("432 Hayes St.", "Hayes St"),
        ("1458 hayes street", "Hayes St"),
        (" 12  19th Ave", "19th Ave"),
    ],
)
def test_a_house_number_in_front_of_a_street_is_dropped(said: str, kept: str) -> None:
    assert clean_street(said) == kept


@pytest.mark.parametrize(
    "said",
    [
        "",
        "St",
        "St St",
        # No street type, so it could as well be a person.
        "Hayes",
        "Jane Doe",
        # A description of a place, or of a way into a home, is not a street's name.
        "Mission St at 16th St",
        "Hayes and Gough St",
        "400 block of Hayes St",
        "Hayes St Apt 4",
        "Hayes St #4",
        "Apt 4 Hayes St",
        "Hayes St 1458",
        "Hayes St, the yellow house",
        "Hayes St\nJane Doe",
        "Jane Doe 415-555-0100 Hayes St",
        "One Two Three Four Five Six St",
        "A" * 60 + " St",
        "x" * 1000,
        432,
        None,
    ],
)
def test_what_is_not_shaped_like_one_streets_name_is_not_kept(said: object) -> None:
    assert clean_street(said) is None


def test_the_limit_of_checking_a_street_by_its_shape_is_known() -> None:
    # Words shaped like a street name are kept as one. Checking against a list of the
    # city's streets would close this, and is not done yet.
    assert clean_street("Jane Doe St") == "Jane Doe St"


@pytest.mark.parametrize(
    ("number", "block"), [(1458, 1400), (400, 400), (499, 400), (99, 0), (0, 0), (99_999, 99_900)]
)
def test_a_house_number_is_kept_only_as_its_hundred_block(number: int, block: int) -> None:
    assert block_of(number) == block


@pytest.mark.parametrize("number", [-1, 100_000, 14.58, "1458", True, None])
def test_what_is_not_a_house_number_has_no_block(number: object) -> None:
    assert block_of(number) is None


def test_a_place_is_put_in_words_no_finer_than_a_block() -> None:
    assert place_in_words("Hayes St", 400, "Gough St") == "400 block of Hayes St"
    assert place_in_words("Hayes St", None, "Gough St") == "Hayes St at Gough St"
    assert place_in_words("Hayes St", None, None) == "Hayes St"
    assert place_in_words("Hayes St", 0, None) == "0 block of Hayes St"
    assert place_in_words(None, 400, "Gough St") is None
