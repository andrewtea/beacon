import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from beacon.scale import results
from beacon.scale.run import Layout, ScaleError

router = APIRouter()

# The setting that names the scale run whose results this server shows: a run under
# `data/scale/`, made by `scripts/scale_run.py`. With none, the server shows none.
RUN_VARIABLE = "BEACON_SCALE_RUN"

# What the page is, said before any run's own words. A scale run is no part of what
# this server shows of incidents, and the page says so.
ABOUT = (
    "A scale run puts one day of fire dispatch calls through the code this server "
    "runs and compares what Beacon made of them with DataSF's public record of that "
    "day. It is made by a command, apart from this server: nothing on this page is "
    "about the incidents this server shows, or about any tap it was sent."
)
NO_RUN = "This server shows no scale run's results."
NO_RESULTS = "The scale run this server names has no results yet."
UNREADABLE = "The scale run's results on this server cannot be read."
COMPLETE = (
    "The run is complete, which means nothing is left for its command to do. It does "
    "not mean every call has a row or every tap an answer: the numbers below say "
    "what came of each."
)
UNFINISHED = "The run is not finished. The numbers below are of what was done so far."


class Credit(BaseModel):
    text: str
    url: str


class ScaleNumber(BaseModel):
    key: str
    # What is counted, in a few words.
    label: str
    # The value as it is shown, with its unit: "468 calls", "85.2%", or "none" for
    # a number the run does not have, which `text` then explains.
    value_text: str
    # What was counted and what the count does not show. Shown beside the value,
    # every time: no number of a run is shown bare.
    text: str


class ScaleSection(BaseModel):
    # A heading that names what its numbers are about, one of `results.SECTIONS`.
    title: str
    numbers: list[ScaleNumber]


class ScaleRun(BaseModel):
    name: str
    # The day of DataSF's record the run is scored against, as YYYY-MM-DD.
    day: str
    made_at: datetime
    # Whether a stand-in had a part in the numbers: generated audio, a stand-in for
    # Ember, or both. Only recorded radio heard by Muse Ember is neither.
    stand_in: bool
    complete: bool
    # What the run is in a few words: "Stand-in, generated calls". For a run a
    # stand-in had a part in it is shown with every value, so that a number read
    # without the rest of the page still says whose it is.
    kind_text: str
    # What the run is, shown before any of its numbers: what its audio was, who
    # heard it, and what its numbers do and do not measure.
    basis_text: str
    # For a run a stand-in had a part in: the sentence that says its numbers are no
    # measurement of Beacon on real radio. Shown with every group of numbers, so
    # that none is read without it. Null only when `stand_in` is false.
    reminder_text: str | None
    # What `complete` means here, in a sentence.
    state_text: str
    # What else holds for every number: shown with them.
    caveats: list[str]
    # Whose record the run is scored against, with its page. Shown with the numbers.
    scored_against: Credit
    sections: list[ScaleSection]


class Scale(BaseModel):
    # What a scale run is and that it is apart from what this server shows.
    about_text: str
    # The run this server shows, or null when it shows none.
    run: ScaleRun | None
    # Why there is no run, when there is none.
    none_text: str | None


def open_published(env: Mapping[str, str]) -> Layout | None:
    """The run whose results this server shows, by `BEACON_SCALE_RUN`, or None.

    Raises ScaleError when the setting is no run's name."""
    name = env.get(RUN_VARIABLE)
    return Layout.under(env, name) if name else None


@router.get("/scale")
def scale(request: Request) -> Scale:
    layout = getattr(request.app.state, "scale", None)
    if layout is None:
        return Scale(about_text=ABOUT, run=None, none_text=NO_RUN)
    try:
        made = results.read(layout)
        if made is None:
            return Scale(about_text=ABOUT, run=None, none_text=NO_RESULTS)
        return Scale(about_text=ABOUT, run=_run(made), none_text=None)
    except (ScaleError, ValidationError, KeyError, TypeError, ValueError, AttributeError):
        pass
    # Outside the handler, so that nothing of the file is chained under the error.
    raise HTTPException(503, UNREADABLE)


def _run(made: dict[str, Any]) -> ScaleRun:
    # Every number under its heading, in the order the run wrote them; a number of
    # a heading this Beacon does not know is shown under no heading rather than left out.
    titles = [*results.SECTIONS, ""]
    by_title: dict[str, list[ScaleNumber]] = {title: [] for title in titles}
    for number in made["numbers"]:
        title = number.get("section", "")
        if not isinstance(title, str):
            raise TypeError
        by_title[title if title in by_title else ""].append(_number(number))
    # What the result says of itself has to be what a run of its kind is: one that
    # does not is not shown under another kind's words.
    kind_text, reminder_text = results.kind(made)
    complete = made["complete"]
    if not isinstance(complete, bool):
        # Not a word that would be read as true or false: the sentence about the
        # run's state is chosen by it.
        raise TypeError
    if not all(_words(caveat) for caveat in made["caveats"]):
        raise TypeError
    return ScaleRun(
        name=made["name"],
        day=made["day"],
        made_at=made["made_at"],
        stand_in=made["stand_in"],
        complete=complete,
        kind_text=kind_text,
        basis_text=made["basis_text"],
        reminder_text=reminder_text,
        state_text=COMPLETE if complete else UNFINISHED,
        caveats=made["caveats"],
        scored_against=Credit(**made["scored_against"]),
        sections=[
            ScaleSection(title=title, numbers=by_title[title])
            for title in titles
            if by_title[title]
        ],
    )


def _words(said: object) -> bool:
    return isinstance(said, str) and bool(said.strip())


def _number(number: Mapping[str, Any]) -> ScaleNumber:
    """A number as it is shown, when it is one: a label, a unit, and a sentence that
    are words, and a value that is a number or none. No number is shown bare, so one
    whose sentence is missing or empty is not shown: the results it is in are refused.

    Raises TypeError for anything else."""
    value = number["value"]
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value)
    ):
        raise TypeError
    if not all(_words(number[field]) for field in ("key", "label", "unit", "text")):
        raise TypeError
    return ScaleNumber(
        key=number["key"],
        label=number["label"],
        value_text=results.shown(number),
        text=number["text"],
    )
