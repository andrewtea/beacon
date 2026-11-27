"""San Francisco's streets, blocks, and fire units, as the lists Beacon reads.

A radio row names a street or a unit only when it is on a list
(`beacon.radio.Known`), and an incident heard on the radio has a point only when its
block is on a list (`beacon.incident.Blocks`). Nothing in the repository is such a
list. This module makes them from two datasets the City and County of San Francisco
publishes on DataSF under the Open Data Commons Public Domain Dedication and License:

- **Streets and blocks** from "Streets - Active and Retired"
  (https://data.sf.gov/d/3psu-pn9h): one row for each stretch of a street between
  two crossings, with the house numbers on each side and the line it runs along.
  A block's place is the middle of the stretch its numbers are on.
- **Units** from the dispatched calls dataset (`beacon.scale.datasf`): the ids of
  the units that were sent to a call in the days *before* the day a run is scored
  on, so that the roster is not read off the answers.

What the lists are not: nobody has checked a block's place against the ground, a
stretch with no house numbers has no block, and a unit that was not sent anywhere in
those days is not on the roster. They are written under `data/`, which Git ignores,
in the forms `BEACON_RADIO_STREETS_FILE`, `BEACON_RADIO_UNITS_FILE`, and
`BEACON_BLOCKS_FILE` take, so the server can be pointed at them for real radio too.
"""

import itertools
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from beacon.errors import raise_bare
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.radio.vocabulary import block_of, clean_street, clean_unit
from beacon.scale import datasf

SOURCE = "datasf"
CENTERLINES = "3psu-pn9h"
CENTERLINES_URL = f"https://data.sf.gov/resource/{CENTERLINES}.json"
CENTERLINES_PAGE = f"https://data.sf.gov/d/{CENTERLINES}"
_COLUMNS = ("streetname", "lf_fadd", "lf_toadd", "rt_fadd", "rt_toadd", "line")
# The dataset has about 16,000 active stretches.
_PAGE_ROWS = 1000
_MAX_PAGES = 40
_MAX_PAGE_BYTES = 4_000_000
# The days before a run's day whose calls the roster of units is read from.
ROSTER_DAYS = 30
# No stretch between two crossings spans more hundred blocks than this.
_MAX_BLOCKS_A_STRETCH = 10

STREETS_FILE = "streets.txt"
UNITS_FILE = "units.txt"
BLOCKS_FILE = "blocks.csv"


@dataclass(frozen=True)
class Lists:
    """The lists as they are written: each street and unit in its stored form, and
    each block as `(street, hundred block, latitude, longitude)`."""

    streets: tuple[str, ...]
    units: tuple[str, ...]
    blocks: tuple[tuple[str, int, float, float], ...]

    def write(self, directory: Path) -> None:
        """Write the three files into `directory`, which is made when it is not
        there. Each is written whole or not at all, and the blocks last: a run takes
        the blocks' file being there to mean that all three are."""
        directory.mkdir(parents=True, exist_ok=True)
        credit = (
            "# From DataSF, City and County of San Francisco, under the Open Data "
            "Commons Public Domain\n# Dedication and License. Made by Beacon's scale "
            "run (beacon.scale.lists); not checked by hand.\n"
        )
        _write_whole(
            directory / STREETS_FILE, credit + "".join(f"{street}\n" for street in self.streets)
        )
        _write_whole(directory / UNITS_FILE, credit + "".join(f"{unit}\n" for unit in self.units))
        _write_whole(
            directory / BLOCKS_FILE,
            credit
            + "# street, hundred block, latitude, longitude\n"
            + "".join(
                f"{street},{block},{lat:.6f},{lon:.6f}\n" for street, block, lat, lon in self.blocks
            ),
        )


def _write_whole(path: Path, text: str) -> None:
    """Write a file to one beside it, which then takes its place."""
    beside = path.with_name(path.name + ".part")
    beside.write_text(text, encoding="utf-8")
    os.replace(beside, path)


def fetch_centerlines(http: SourceHttp) -> list[Mapping[str, Any]]:
    """DataSF's rows for the active stretches of San Francisco's streets, with the
    columns that are needed and no others.

    Raises SourceError when DataSF cannot be read, or lists more than is read."""
    rows: list[Mapping[str, Any]] = []
    for page in range(_MAX_PAGES):
        body = fetch(
            http,
            "GET",
            CENTERLINES_URL,
            source=SOURCE,
            max_bytes=_MAX_PAGE_BYTES,
            params={
                "$select": ",".join(_COLUMNS),
                "$where": "active = true",
                "$order": "cnn",
                "$limit": str(_PAGE_ROWS),
                "$offset": str(page * _PAGE_ROWS),
            },
        )
        listed = read_json(body, SOURCE)
        if not isinstance(listed, list):
            raise_bare(SourceError(SOURCE, "sent a reply that is not a list of rows"))
        rows += [row for row in listed if isinstance(row, dict)]
        if len(listed) < _PAGE_ROWS:
            return rows
    raise_bare(SourceError(SOURCE, "lists more streets than Beacon reads"))


def fetch_roster(http: SourceHttp, before: date) -> list[str]:
    """The ids of the units DataSF lists as sent to a call in the `ROSTER_DAYS`
    days before `before`, as DataSF writes them.

    Raises SourceError when DataSF cannot be read."""
    first = before - timedelta(days=ROSTER_DAYS)
    body = fetch(
        http,
        "GET",
        datasf.URL,
        source=SOURCE,
        max_bytes=_MAX_PAGE_BYTES,
        params={
            "$select": "unit_id",
            "$where": (
                f"call_date >= '{first.isoformat()}T00:00:00' "
                f"AND call_date < '{before.isoformat()}T00:00:00'"
            ),
            "$group": "unit_id",
            "$limit": "5000",
        },
    )
    listed = read_json(body, SOURCE)
    if not isinstance(listed, list):
        raise_bare(SourceError(SOURCE, "sent a reply that is not a list of rows"))
    return [row["unit_id"] for row in listed if isinstance(row, dict) and "unit_id" in row]


def lists_of(centerlines: Iterable[Mapping[str, Any]], roster: Iterable[object]) -> Lists:
    """The lists, from DataSF's rows for the streets and the ids of its units. A
    stretch with no name Beacon keeps, no house numbers, or no line is left out, and
    so is a unit whose id is not shaped as Beacon keeps one."""
    places: dict[tuple[str, int], list[tuple[float, float]]] = {}
    streets: set[str] = set()
    for row in centerlines:
        street = clean_street(row.get("streetname"))
        if street is None:
            continue
        streets.add(street)
        middle = _middle(row.get("line"))
        if middle is None:
            continue
        for block in _blocks(row):
            places.setdefault((street, block), []).append(middle)
    blocks = []
    for (street, block), middles in sorted(places.items()):
        # A block that is drawn as several stretches is placed among them.
        lat = sum(lat for lat, _ in middles) / len(middles)
        lon = sum(lon for _, lon in middles) / len(middles)
        blocks.append((street, block, lat, lon))
    units = {unit for unit in (clean_unit(named) for named in roster) if unit is not None}
    return Lists(streets=tuple(sorted(streets)), units=tuple(sorted(units)), blocks=tuple(blocks))


def _blocks(row: Mapping[str, Any]) -> list[int]:
    """The hundred blocks a stretch's house numbers are on."""
    numbers = [_number(row.get(column)) for column in _COLUMNS[1:5]]
    on_it = [number for number in numbers if number is not None and number > 0]
    if not on_it:
        return []
    first, last = block_of(min(on_it)), block_of(max(on_it))
    if first is None or last is None:
        return []
    blocks = list(range(first, last + 1, 100))
    return blocks if len(blocks) <= _MAX_BLOCKS_A_STRETCH else []


def _number(said: object) -> int | None:
    if isinstance(said, bool):
        return None
    try:
        # An integer of hundreds of digits is a number JSON can hold and a float cannot.
        number = float(said)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if math.isfinite(number) and 0 <= number < 100_000 else None


def _middle(line: object) -> tuple[float, float] | None:
    """The point half way along a line as GeoJSON draws one, as `(lat, lon)`."""
    if not isinstance(line, dict) or line.get("type") != "LineString":
        return None
    drawn = line.get("coordinates")
    if not isinstance(drawn, list) or not 2 <= len(drawn) <= 5000:
        return None
    points: list[tuple[float, float]] = []
    for position in drawn:
        if not isinstance(position, list) or len(position) < 2:
            return None
        lon, lat = position[0], position[1]
        if not all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in (lon, lat)
        ):
            return None
        # The bounds, which no infinity, no NaN, and no integer too large for a
        # float is within.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        points.append((float(lat), float(lon)))
    # On a plane, which a city block is near enough to.
    width = math.cos(math.radians(points[0][0]))
    stretches = [
        (a, b, math.hypot(b[0] - a[0], (b[1] - a[1]) * width))
        for a, b in itertools.pairwise(points)
    ]
    half = sum(length for _, _, length in stretches) / 2
    for a, b, length in stretches:
        if half <= length and length > 0:
            along = half / length
            return (a[0] + (b[0] - a[0]) * along, a[1] + (b[1] - a[1]) * along)
        half -= length
    return points[0]
