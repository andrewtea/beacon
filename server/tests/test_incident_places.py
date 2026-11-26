"""The list of blocks that gives a radio call's place a point."""

import pytest

from beacon.evidence import Point
from beacon.incident import NO_BLOCKS, Blocks, read_blocks


def test_a_listed_block_has_a_point_and_an_unlisted_one_has_none() -> None:
    blocks = Blocks.of([("hayes street", 458, 37.7766, -122.4236)])

    assert blocks.point("Hayes St", 400) == Point(37.7766, -122.4236)
    assert blocks.point("Hayes St", 500) is None
    assert blocks.point("Hayes St", None) is None
    assert NO_BLOCKS.point("Hayes St", 400) is None


def test_a_list_file_is_read_line_by_line_and_what_is_no_block_is_counted() -> None:
    blocks, left_out = read_blocks(
        "# street, block, latitude, longitude\n"
        "Hayes St,400,37.7766,-122.4236\n"
        "\n"
        "Fell St, 400, 37.7757, -122.4234\n"
        "Hayes at Gough,400,37.77,-122.42\n"
        "Fell St,400,nan,-122.42\n"
        "Fell St,500,97.0,-122.42\n"
        "Fell St,500\n"
    )

    assert left_out == 4
    assert set(blocks.points) == {("Hayes St", 400), ("Fell St", 400)}


def test_a_refused_entry_is_named_by_its_place_and_not_by_what_it_holds() -> None:
    with pytest.raises(ValueError, match="Entry 2") as refused:
        Blocks.of([("Hayes St", 400, 37.7, -122.4), ("12 Secret Ln Apt 4", 1, 37.7, -122.4)])

    assert "Secret" not in str(refused.value)
