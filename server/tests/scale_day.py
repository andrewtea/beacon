"""An invented day in DataSF's shape, and a stand-in for DataSF that serves it.

Nothing here is DataSF's data: the calls, their numbers, and their times are made
up, on a few blocks whose places are made up too.
"""

from typing import Any

import httpx2

from beacon.ingest.http import SourceHttp

# A day that is over, whenever the tests are run.
DAY = "2026-03-03"

# Where the invented blocks are: a street, the hundred block, and the line it runs
# along, as (longitude, latitude) pairs.
_STRETCHES = (
    ("HAYES ST", 400, ((-122.4230, 37.7769), (-122.4245, 37.7767))),
    ("HAYES ST", 500, ((-122.4245, 37.7767), (-122.4261, 37.7765))),
    ("GOUGH ST", 300, ((-122.4229, 37.7760), (-122.4230, 37.7769))),
    ("FELL ST", 300, ((-122.4213, 37.7761), (-122.4229, 37.7759))),
    ("OCTAVIA ST", 300, ((-122.4245, 37.7758), (-122.4246, 37.7767))),
    ("GEARY BLVD", 5200, ((-122.4750, 37.7805), (-122.4761, 37.7804))),
    ("17TH AVE", 400, ((-122.4755, 37.7795), (-122.4756, 37.7804))),
)
# The corners the invented calls are at.
HAYES_GOUGH = ("HAYES ST/GOUGH ST", -122.4230, 37.7769)
FELL_GOUGH = ("FELL ST/GOUGH ST", -122.4229, 37.7759)
HAYES_OCTAVIA = ("HAYES ST/OCTAVIA ST", -122.4245, 37.7767)
GEARY_17TH = ("GEARY BLVD/17TH AVE", -122.4756, 37.7804)

ROSTER = ("E36", "T06", "B02", "E05", "M552", "E31", "T12", "SCRT1", "EMS6A")
# DataSF's unit type for the units of each type code of the invented day.
UNIT_TYPES = {"E": "ENGINE", "T": "TRUCK", "B": "CHIEF", "M": "MEDIC", "SCRT": "CP", "EMS": "CP"}


def centerlines() -> list[dict[str, Any]]:
    return [
        {
            "streetname": street,
            "lf_fadd": f"{block + 1}.0",
            "lf_toadd": f"{block + 99}.0",
            "rt_fadd": f"{block}.0",
            "rt_toadd": f"{block + 98}.0",
            "line": {"type": "LineString", "coordinates": [list(end) for end in line]},
        }
        for street, block, line in _STRETCHES
    ]


def unit_row(
    number: str,
    unit: str,
    call_type: str,
    dispatched: str,
    corner: tuple[str, float, float] | None,
    *,
    on_scene: str | None = None,
    available: str | None = None,
    alarms: str = "1",
    **more: Any,
) -> dict[str, Any]:
    """One row of DataSF's: one unit sent to one call, at times of the invented
    day's wall clock (`08:00:00`)."""
    row: dict[str, Any] = {
        "call_number": number,
        "unit_id": unit,
        "call_type": call_type,
        "received_dttm": f"{DAY}T{dispatched}.000",
        "dispatch_dttm": f"{DAY}T{dispatched}.000",
        "number_of_alarms": alarms,
        # What DataSF also publishes, and Beacon never asks for or keeps.
        "call_final_disposition": "Code 3 Transport",
        "call_type_group": "Potentially Life-Threatening",
        "priority": "3",
    }
    if corner is not None:
        address, lon, lat = corner
        row["address"] = address
        row["case_location"] = {"type": "Point", "coordinates": [lon, lat]}
    if on_scene is not None:
        row["on_scene_dttm"] = f"{DAY}T{on_scene}.000"
    if available is not None:
        row["available_dttm"] = f"{DAY}T{available}.000"
    return row | more


def call(
    number: str,
    call_type: str,
    dispatched: str,
    corner: tuple[str, float, float] | None,
    units: tuple[str, ...],
    **more: Any,
) -> list[dict[str, Any]]:
    """The rows of one call: one for each unit."""
    return [unit_row(number, unit, call_type, dispatched, corner, **more) for unit in units]


class DataSf:
    """Stands where data.sf.gov is: answers the four questions a scale run asks,
    with the invented day, and keeps what was asked."""

    def __init__(self, rows: list[dict[str, Any]], *, roster: tuple[str, ...] = ROSTER) -> None:
        self.rows = rows
        self.roster = roster
        self.unit_types = dict(UNIT_TYPES)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.host != "data.sf.gov":
            return httpx2.Response(404)
        asked = request.url.params
        if request.url.path.endswith("/3psu-pn9h.json"):
            listed: list[dict[str, Any]] = centerlines()
        elif "$group" in asked and "unit_type" in asked["$select"]:
            # The units of the day's rows, each with its type.
            units = sorted({row["unit_id"] for row in self.rows})
            return httpx2.Response(
                200,
                json=[
                    {"unit_id": unit, "unit_type": self.unit_types[unit.rstrip("0123456789A")]}
                    for unit in units
                    if unit.rstrip("0123456789A") in self.unit_types
                ],
            )
        elif "$group" in asked:
            return httpx2.Response(200, json=[{"unit_id": unit} for unit in self.roster])
        else:
            # Only the columns that were asked for, as DataSF answers.
            columns = asked["$select"].split(",")
            listed = [{key: row[key] for key in columns if key in row} for row in self.rows]
        offset, limit = int(asked.get("$offset", "0")), int(asked["$limit"])
        return httpx2.Response(200, json=listed[offset : offset + limit])

    def http(self) -> SourceHttp:
        return SourceHttp(httpx2.MockTransport(self))
