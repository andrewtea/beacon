"""Where a block is: the list that gives a radio call's place a point on the map.

A radio row has a place in words (`400 block of Hayes St`) and no point, because
nothing in ingest knows where a block is. Everything else Beacon matches by distance:
a tap, an alert's area, a closure, an aircraft overhead. `Blocks` is the list that
joins the two: a street and a hundred block, and the middle of that block.

The list is somebody's to supply, as the lists of streets and units are for radio
(`beacon.radio.Known`). The mock scenario has its own (`beacon.scenario.BLOCK_POINTS`).
For real radio an operator names a file (`BEACON_BLOCKS_FILE`), one block to a line:

    # street, hundred block, latitude, longitude
    Hayes St,400,37.7766,-122.4236

Without a list an incident still gets a point when 511 lists a closure on its street,
and has none otherwise: it is on the timeline, and no tap is matched to it by
distance.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from beacon.evidence import Point
from beacon.radio.vocabulary import block_of, clean_street


@dataclass(frozen=True)
class Blocks:
    """The middle of each block somebody listed. Build one with `Blocks.of`."""

    points: Mapping[tuple[str, int], Point] = field(default_factory=dict)

    @classmethod
    def of(cls, entries: Iterable[tuple[str, int, float, float]]) -> "Blocks":
        """The list, from `(street, block, lat, lon)` entries, each street put in
        its stored form and each block made the hundred block.

        Raises ValueError when an entry is not a street Beacon keeps, a block, or a
        position, and says which place in the list it has, not what it is."""
        blocks, left_out = cls.kept(entries)
        if left_out:
            raise ValueError(f"Entry {left_out[0]} in the list of blocks is not a block's place.")
        return blocks

    @classmethod
    def kept(cls, entries: Iterable[object]) -> "tuple[Blocks, list[int]]":
        """The list as `of` makes it, with the entries it would refuse left out,
        and the places those had in the list, counted from one."""
        points: dict[tuple[str, int], Point] = {}
        left_out: list[int] = []
        for place, entry in enumerate(entries, start=1):
            listed = _entry(entry)
            if listed is None:
                left_out.append(place)
            else:
                points.setdefault(listed[0], listed[1])
        return cls(points), left_out

    def point(self, street: str | None, block: int | None) -> Point | None:
        """The middle of a listed block, or None."""
        if street is None or block is None:
            return None
        return self.points.get((street, block))


# With this, no radio call's place has a point.
NO_BLOCKS = Blocks()


def read_blocks(text: str) -> tuple[Blocks, int]:
    """The blocks in a list file, and how many of its lines are not one. A line is
    `street,block,latitude,longitude`; blank lines and lines that begin with `#`
    are left out and not counted."""
    lines = (line.strip() for line in text.splitlines())
    entries = [tuple(line.split(",")) for line in lines if line and not line.startswith("#")]
    blocks, left_out = Blocks.kept(entries)
    return blocks, len(left_out)


def _entry(entry: object) -> tuple[tuple[str, int], Point] | None:
    if not isinstance(entry, tuple | list) or len(entry) != 4:
        return None
    street = clean_street(entry[0])
    number, lat, lon = (_number(value) for value in entry[1:])
    if street is None or number is None or lat is None or lon is None:
        return None
    block = block_of(int(number)) if number == int(number) else None
    if block is None:
        return None
    try:
        return (street, block), Point(lat, lon)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    """A finite number, given as one or as its text, or None."""
    number: float | None = None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str) and len(value) <= 30:
        try:
            number = float(value.strip())
        except ValueError:
            return None
    return number if number is not None and math.isfinite(number) else None
