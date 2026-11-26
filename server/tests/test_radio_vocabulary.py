"""The closed vocabulary a radio call is kept in: what fits it, and what is dropped."""

import pytest

from beacon.radio.vocabulary import (
    NOTHING_KNOWN,
    Known,
    block_of,
    clean_street,
    clean_unit,
    place_in_words,
    read_names,
)


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


def test_shape_alone_keeps_what_is_no_street_and_no_unit() -> None:
    # Which is why a name is then looked up in `Known`.
    assert clean_street("Jane Doe St") == "Jane Doe St"
    assert clean_street("One Two Three Hayes St") == "One Two Three Hayes St"
    assert clean_unit("BP 180") == "BP180"
    assert clean_unit("Ann 72") == "ANN72"


KNOWN = Known.of(streets=["hayes street", "19TH AVE", "Broadway"], units=["Engine 36", "t06"])


def test_a_list_holds_each_name_in_its_stored_form() -> None:
    assert KNOWN.streets == {"Hayes St", "19th Ave", "Broadway"}
    assert KNOWN.units == {"E36", "T6"}


@pytest.mark.parametrize(
    ("said", "kept"),
    [("432 Hayes St.", "Hayes St"), ("19th avenue", "19th Ave"), ("BROADWAY", "Broadway")],
)
def test_a_listed_street_is_kept_in_its_stored_form(said: str, kept: str) -> None:
    assert KNOWN.street(said) == kept


@pytest.mark.parametrize(
    "said", ["Jane Doe St", "One Two Three Hayes St", "Fell St", "Hayes", "Hayes at Gough", 7, None]
)
def test_a_street_that_is_not_listed_is_not_kept(said: object) -> None:
    assert KNOWN.street(said) is None


def test_a_unit_is_kept_only_when_it_is_listed() -> None:
    assert (KNOWN.unit("truck 6"), KNOWN.unit("E036")) == ("T6", "E36")
    for said in ("BP 180", "Ann 72", "M72", "E37", "Jane", None):
        assert KNOWN.unit(said) is None


def test_with_no_list_nothing_is_kept() -> None:
    assert (NOTHING_KNOWN.street("Hayes St"), NOTHING_KNOWN.unit("E36")) == (None, None)


def test_a_list_made_in_code_refuses_a_name_beacon_does_not_keep_without_repeating_it() -> None:
    with pytest.raises(
        ValueError, match=r"^Name 2 in the list of streets is not a street Beacon keeps\.$"
    ):
        Known.of(streets=["Hayes St", "Jane Doe's house"])
    with pytest.raises(ValueError, match="Name 1 in the list of units"):
        Known.of(units=["Jane"])


def test_a_list_from_a_file_leaves_out_what_beacon_does_not_keep_and_counts_it() -> None:
    names = read_names(
        "# San Francisco street names\n\nHAYES ST\n  Gough St  \n"
        "AVENUE OF THE PALMS\n# and\nUNNAMED 042\n"
    )
    assert names == ["HAYES ST", "Gough St", "AVENUE OF THE PALMS", "UNNAMED 042"]

    known, left_out = Known.kept(streets=names, units=["E36", "the chief"])

    assert (known.streets, known.units, left_out) == ({"Hayes St", "Gough St"}, {"E36"}, 3)


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
