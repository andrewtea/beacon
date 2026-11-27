"""`python -m beacon.moments`: when each thing in the invented story happens.

    python -m beacon.moments 2026-12-01T18:10:00+00:00

For somebody who shows the invented story to others (`BEACON_MOCK_STORY_MINUTE`,
`scripts/film`): given the moment the story's first minute is counted from
(`story_began_at` of `GET /api/sources`), it prints what changes in the story with
the time each change comes at, and which of them are past. What is there from
before the story to its end is not a moment of it, and is named in one line
(`THROUGHOUT`). It reads no store and asks no server; everything it prints is the
invented scenario's own, made up for Beacon.

The radio calls and the notices are read from the scenario (`CALLS`, `NOTICES`),
so a change to either is a change here. What the other sources do is written down
here from `beacon/scenario/fire.py`, where the minutes are conditions in code: a
change to one there is a change to `_OTHERS` here.
"""

import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

# First, for an older circular import between the pollers and the scenario.
from beacon import ingest as _ingest  # noqa: F401
from beacon.scenario import DURATION
from beacon.scenario import notices as scenario_notices
from beacon.scenario import radio as scenario_radio

# What the other invented sources do, by the minute of the story.
_OTHERS: tuple[tuple[float, str], ...] = (
    (5.0, "Aircraft: a helicopter comes in. It circles the fire from minute 7."),
    (9.0, "511: Hayes St is listed as closed between Gough St and Octavia St."),
    (16.0, "NWS: a shelter-in-place warning for smoke is issued for Hayes Valley."),
    (20.0, "511: the closure is widened to Fell St."),
    (25.0, "Aircraft: a second aircraft passes along the northern waterfront until minute 29."),
    (40.0, "NWS: the warning is replaced by one for a smaller area."),
    (48.0, "Aircraft: the helicopter leaves, and is gone by minute 50."),
    (70.0, "NWS: the warning is no longer listed."),
    (80.0, "511: the closure is no longer listed."),
)

# What the invented sources say from before the story to its end, which no line
# above is about.
THROUGHOUT = (
    "There throughout, and in no line above: a wind advisory, road work elsewhere "
    "in the city, an airliner that crosses the city every ten minutes, and what "
    "each source carries for Beacon to leave out (a test alert, an event in "
    "Oakland, pages of the notices feed that are no notices)."
)


def moments() -> list[tuple[float, str]]:
    """What changes in the invented story, as the minute each change comes at and
    a line about it, in the story's order. Not what is there throughout
    (`THROUGHOUT`)."""
    radio = [
        (
            call.minute,
            "Radio: nothing is said in this call." if call.said is None else f"Radio: {call.said}",
        )
        for call in scenario_radio.CALLS
    ]
    notices = [
        (notice.minute, f"Notice published as a PDF: {notice.title}.")
        for notice in scenario_notices.NOTICES
    ]
    end = (DURATION.total_seconds() / 60, "The story ends. Nothing in it changes after this.")
    return sorted([*radio, *notices, *_OTHERS, end], key=lambda moment: moment[0])


def lines(began_at: datetime, now: datetime) -> list[str]:
    """The story's moments with the time each comes at, for a story whose first
    minute is counted from `began_at`, and where the story is at `now`."""
    minute = (now - began_at).total_seconds() / 60
    last = DURATION.total_seconds() / 60
    if minute < 0:
        where = "The invented story has not begun."
    elif minute > last:
        where = "The invented story is over."
    else:
        where = f"The invented story is in minute {int(minute)} of {int(last)}."
    told = [where, ""]
    for at, what in moments():
        when = (began_at + timedelta(minutes=at)).astimezone(UTC).strftime("%H:%M:%S")
        told.append(f"{'past' if at <= minute else '    '}  min {at:>4.1f}  {when} UTC  {what}")
    return [*told, "", THROUGHOUT]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    began_at: datetime | None = None
    if len(arguments) == 1:
        try:
            began_at = datetime.fromisoformat(arguments[0])
        except ValueError:
            began_at = None
    if began_at is None or began_at.tzinfo is None:
        print(
            "Give the moment the story began, with its offset, as `story_began_at` of "
            "GET /api/sources has it: python -m beacon.moments 2026-12-01T18:10:00+00:00",
            file=sys.stderr,
        )
        return 2
    print("\n".join(lines(began_at, datetime.now(UTC))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
