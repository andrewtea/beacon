"""DataSF's record of the fire department's dispatched calls: what a scale run is
scored against.

The dataset is "Fire Department and Emergency Medical Services Dispatched Calls for
Service" (https://data.sf.gov/d/nuek-vuh3), which the City and County of San
Francisco publishes under the Open Data Commons Public Domain Dedication and License
and updates daily. It has one row for each unit sent to each call, with the call's
type, its times, and a place no finer than an intersection.

**What Beacon keeps of it** is a `Record` for each call: the call's number, its
kind in Beacon's own vocabulary, when it was received and dispatched, when the
first unit was on scene and the last was available again, the streets of its
intersection, DataSF's point for it, and its units. More than half the calls are
medical, and for those the rules for EMS hold here as they do for radio: a medical
call is "medical" with DataSF's number for the call (which is public, and is how a
record is told from another), its crews, its intersection, and its times, and
nothing else.
So only the columns in `COLUMNS` are asked for at all: not the disposition, the
priority, or the call type group, which say how a patient was, and nothing that
DataSF does not publish is added. A record is kept in a run's own directory under
`data/`, which Git ignores: downloaded source data is not committed.

Times in the dataset have no zone: they are San Francisco's wall clock (`LOCAL`).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from beacon.errors import raise_bare
from beacon.evidence import Point
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.radio.vocabulary import MAX_ALARM, clean_street, clean_unit

SOURCE = "datasf"
DATASET = "nuek-vuh3"
URL = f"https://data.sf.gov/resource/{DATASET}.json"
PAGE_URL = f"https://data.sf.gov/d/{DATASET}"
LICENSE = "Open Data Commons Public Domain Dedication and License"
# What is said of the dataset wherever a number scored against it is shown.
CREDIT = (
    "Fire Department and Emergency Medical Services Dispatched Calls for Service, "
    f"City and County of San Francisco, DataSF ({LICENSE})"
)
LOCAL = ZoneInfo("America/Los_Angeles")

# The only columns that are asked for.
COLUMNS = (
    "call_number",
    "unit_id",
    "call_type",
    "received_dttm",
    "dispatch_dttm",
    "on_scene_dttm",
    "available_dttm",
    "address",
    "number_of_alarms",
    "case_location",
)
# A day is about 1,100 rows. More than this many pages is not a day's answer.
PAGE_ROWS = 1000
MAX_PAGES = 20
_MAX_PAGE_BYTES = 4_000_000

# The kind of a call in Beacon's vocabulary (`beacon.radio.vocabulary.CATEGORIES`),
# by the words of DataSF's call type, the first that fits.
_KINDS = (
    ("medical", "medical"),
    ("fire", "fire"),
    ("smoke", "fire"),
    ("explosion", "fire"),
    ("alarm", "alarm"),
    ("collision", "collision"),
    ("gas leak", "utility"),
    ("electrical", "utility"),
    ("hazmat", "hazmat"),
    ("fuel spill", "hazmat"),
    ("odor", "hazmat"),
    ("rescue", "rescue"),
    ("extrication", "rescue"),
    ("collapse", "rescue"),
    ("watercraft", "rescue"),
    ("assist", "service"),
    ("service", "service"),
)


@dataclass(frozen=True)
class Record:
    """One call DataSF lists, from the rows of its units."""

    number: str
    # One of Beacon's categories. `medical` for a medical incident, of which
    # nothing but its crews, its intersection, and its times is kept.
    kind: str
    received_at: datetime
    # When the first unit was dispatched, and when the first was on scene and the
    # last available again, where a row says so.
    dispatched_at: datetime
    on_scene_at: datetime | None
    cleared_at: datetime | None
    # The streets of the intersection DataSF gives, in Beacon's stored form, and
    # DataSF's point for the call. Neither is finer than an intersection.
    streets: tuple[str, ...]
    point: Point | None
    # The units that were sent, in the order they were dispatched, each in its
    # stored form. One whose id is not shaped as Beacon keeps a unit is left out.
    units: tuple[str, ...]
    # None for a medical call, whose alarm level is not kept.
    alarms: int | None

    @property
    def medical(self) -> bool:
        return self.kind == "medical"

    def as_data(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "received_at": self.received_at.isoformat(),
            "dispatched_at": self.dispatched_at.isoformat(),
            "on_scene_at": None if self.on_scene_at is None else self.on_scene_at.isoformat(),
            "cleared_at": None if self.cleared_at is None else self.cleared_at.isoformat(),
            "streets": list(self.streets),
            "point": None if self.point is None else [self.point.lat, self.point.lon],
            "units": list(self.units),
            "alarms": self.alarms,
        }


def fetch_day(http: SourceHttp, day: date) -> list[Mapping[str, Any]]:
    """DataSF's rows for the calls received on `day`, San Francisco's day, with
    the columns in `COLUMNS` and no others.

    Raises SourceError when DataSF cannot be read, or lists more than a day has."""
    rows: list[Mapping[str, Any]] = []
    for page in range(MAX_PAGES):
        body = fetch(
            http,
            "GET",
            URL,
            source=SOURCE,
            max_bytes=_MAX_PAGE_BYTES,
            params={
                "$select": ",".join(COLUMNS),
                "$where": f"call_date = '{day.isoformat()}T00:00:00'",
                "$order": "call_number, unit_id",
                "$limit": str(PAGE_ROWS),
                "$offset": str(page * PAGE_ROWS),
            },
        )
        listed = read_json(body, SOURCE)
        if not isinstance(listed, list):
            raise_bare(SourceError(SOURCE, "sent a reply that is not a list of rows"))
        rows += [row for row in listed if isinstance(row, dict)]
        if len(listed) < PAGE_ROWS:
            return rows
    raise_bare(SourceError(SOURCE, "lists more rows for one day than Beacon reads"))


def read_records(rows: Iterable[Mapping[str, Any]]) -> tuple[list[Record], int]:
    """The calls a day's rows are of, oldest first by when each was dispatched, and
    how many calls could not be read: one with no number, no time it was received
    or dispatched, or a type that is no text."""
    by_number: dict[str, list[Mapping[str, Any]]] = {}
    unreadable = 0
    for row in rows:
        number = row.get("call_number")
        if isinstance(number, str) and _digits(number, 12):
            by_number.setdefault(number, []).append(row)
        else:
            unreadable += 1
    records: list[Record] = []
    for number, of_call in by_number.items():
        record = _record(number, of_call)
        if record is None:
            unreadable += 1
        else:
            records.append(record)
    records.sort(key=lambda record: (record.dispatched_at, record.number))
    return records, unreadable


def records_from_data(listed: object) -> list[Record]:
    """The records a run kept (`Record.as_data`), read back.

    Raises ValueError when what was kept is not a list of them."""
    if not isinstance(listed, list):
        raise ValueError("The records a run kept are a list.")
    records = []
    for kept in listed:
        try:
            point = kept["point"]
            records.append(
                Record(
                    number=str(kept["number"]),
                    kind=str(kept["kind"]),
                    received_at=datetime.fromisoformat(kept["received_at"]),
                    dispatched_at=datetime.fromisoformat(kept["dispatched_at"]),
                    on_scene_at=_kept_time(kept["on_scene_at"]),
                    cleared_at=_kept_time(kept["cleared_at"]),
                    streets=tuple(str(street) for street in kept["streets"]),
                    point=None if point is None else Point(float(point[0]), float(point[1])),
                    units=tuple(str(unit) for unit in kept["units"]),
                    alarms=None if kept["alarms"] is None else int(kept["alarms"]),
                )
            )
        except (KeyError, TypeError, ValueError, IndexError):
            raise ValueError("The records a run kept are not all records.") from None
    return records


def kind_of(call_type: str) -> str:
    """Beacon's category for one of DataSF's call types."""
    words = call_type.lower()
    for word, kind in _KINDS:
        if word in words:
            return kind
    return "other"


def _record(number: str, rows: list[Mapping[str, Any]]) -> Record | None:
    call_type = rows[0].get("call_type")
    received = _earliest(rows, "received_dttm")
    dispatched = _earliest(rows, "dispatch_dttm")
    if not isinstance(call_type, str) or received is None or dispatched is None:
        return None
    kind = kind_of(call_type)
    in_order = sorted(rows, key=lambda row: str(row.get("dispatch_dttm") or "9"))
    units: list[str] = []
    for row in in_order:
        unit = clean_unit(row.get("unit_id"))
        if unit is not None and unit not in units:
            units.append(unit)
    available = [_time(row.get("available_dttm")) for row in rows]
    return Record(
        number=number,
        kind=kind,
        received_at=received,
        dispatched_at=dispatched,
        on_scene_at=_earliest(rows, "on_scene_dttm"),
        # Only when every unit is said to be available again.
        cleared_at=max(available) if all(available) else None,  # type: ignore[type-var]
        streets=_streets(rows[0].get("address")),
        point=_point(rows[0].get("case_location")),
        units=tuple(units),
        alarms=None if kind == "medical" else _alarms(rows),
    )


def _streets(address: object) -> tuple[str, ...]:
    """The streets of an intersection as DataSF writes one (`EDDY ST/TAYLOR ST`).
    Any other form of address, a call box's among them, gives none."""
    if not isinstance(address, str) or "/" not in address or len(address) > 120:
        return ()
    streets = [clean_street(part.strip()) for part in address.split("/")]
    if len(streets) != 2 or None in streets or streets[0] == streets[1]:
        return ()
    return tuple(street for street in streets if street is not None)


def _point(location: object) -> Point | None:
    if not isinstance(location, dict):
        return None
    coordinates = location.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return None
    lon, lat = coordinates
    if not all(
        isinstance(value, int | float) and not isinstance(value, bool) for value in (lon, lat)
    ):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return Point(float(lat), float(lon))


def _alarms(rows: list[Mapping[str, Any]]) -> int | None:
    levels = []
    for row in rows:
        said = row.get("number_of_alarms")
        # Two ASCII digits at the most: `str.isdigit` alone also passes characters
        # `int` cannot read.
        if isinstance(said, str) and _digits(said, 2) and 1 <= int(said) <= MAX_ALARM:
            levels.append(int(said))
    return max(levels, default=None)


def _earliest(rows: list[Mapping[str, Any]], column: str) -> datetime | None:
    times = [time for time in (_time(row.get(column)) for row in rows) if time is not None]
    return min(times, default=None)


def _time(said: object) -> datetime | None:
    """A time as DataSF writes one, San Francisco's wall clock, in UTC."""
    if not isinstance(said, str) or len(said) > 40:
        return None
    try:
        local = datetime.fromisoformat(said)
        if local.tzinfo is None:
            local = local.replace(tzinfo=LOCAL)
        # A time at the edge of what a date can hold has no UTC to be put in.
        return local.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _digits(said: str, most: int) -> bool:
    """Whether a text is from one to `most` of the digits 0 to 9 and nothing else."""
    return 0 < len(said) <= most and said.isascii() and said.isdigit()


def _kept_time(said: object) -> datetime | None:
    return None if said is None else datetime.fromisoformat(str(said))
