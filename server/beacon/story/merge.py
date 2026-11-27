"""`merge_incident`: Ember chooses what an incident's lines add up to, and code checks
each choice against its rows and writes the sentence.

The incident engine has already decided what belongs to the incident and written a
line for each thing a source said (`beacon.incident`). An incident that has gone on
for an hour has dozens. This call has Ember pick the few points that tell it, in the
order a reader should see them. It decides nothing, and **no word Ember writes is
shown**:

- **Ember answers with points, not prose.** A point is a kind from a closed list
  (`POINT_KINDS`: where the radio has got to, an alarm level, the units, a detail a
  call gave, a 511 event, an aircraft, taps) and the numbers of the lines that show
  it. No free text of Ember's is read, kept, or shown. A model that writes prose
  cannot be kept from telling a reader what to do by a list of words, so it is
  given no sentence to write.
- **Code checks every point against the lines it cites.** A point is kept only when
  every line it cites shows it: an `evacuation` point has to cite radio calls whose
  own rows hold that detail, an `alarm` point calls that gave an alarm level. A
  point that cites nothing, a line that is not there, or a line that does not show
  it is left out and counted (`Draft.left_out`).
- **Code writes the sentence**, from the cited rows' own fields, as what was said
  or seen then: a story's sentence is read apart from its line, so it is in the past
  tense and carries its time, and it names no speaker, because a row keeps what
  was said and not who said it. What was said about an evacuation, sheltering, or
  an all-clear is written with "That is radio traffic, not an official order.", as
  it is in the timeline.
  The version of a 511 event that is in force, an aircraft, and taps are told in
  the timeline's own line. A version that is no longer in force is told only as
  having been listed, and not as having been listed here: the row a point cites
  shows no more than that. A row that records 511's list changing is told as what
  Beacon saw at that check.
- **Code gives it its sources and its confidence**: the sources of the lines it
  cites, and the least confidence among them.
- **Ember is given only the lines of radio, 511 events, aircraft, and taps.** It is
  given no alert and no notice, and there is no kind of point about one: what an
  authority said is shown by code, in the authority's words or as a labeled reading
  beside its link (`Incident.official`).
- **A medical response has no story.** It is its crews and its block, which the
  engine already says, and `merge_incident` refuses it.

So a story is a choice among true lines, and what can be wrong with it is the choice:
a point that matters can be missing, and an old status can be picked over a new one,
which is why each sentence carries the time of its line.

The call is `sensitive=True`: its answer is not read beyond its form, and nothing of
the request or the answer is kept in the ledger or the trace besides the accounting.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from beacon.ember import EmberClient
from beacon.errors import raise_bare
from beacon.evidence import Confidence
from beacon.incident import Entry, Incident, Source, weakest
from beacon.incident.build import NOT_AN_ORDER, STATUS_WORDS
from beacon.radio.vocabulary import MEDICAL_EVENT

TASK = "merge_incident"
# Reasoning counts against the limit. Measured on the first real calls (2026-11-27,
# the invented fire's lines): at `low` effort Ember reasoned for 998 of 1,000
# tokens and wrote no form; at `minimal` it reasoned for 566 and chose eight
# points, each of which passed the check.
EFFORT = "minimal"
MAX_OUTPUT_TOKENS = 2000

# The kinds of line Ember is given, and so the only ones a point can cite.
LINE_KINDS = ("radio", "closure", "aircraft", "taps")
# The newest lines are given, and no more than this many.
MAX_LINES = 60
MAX_POINTS = 8
MAX_CITES = 4
# The most units one sentence names.
MAX_UNITS = 12
# What is said of a version of a 511 event that is not the one in force. Not that
# it was "here": every version of an event is among an incident's lines once one of
# them is about its place, and this one can be about another road.
OLD_CLOSURE = "511 listed a traffic event."
# What is said of a row that records Beacon seeing a 511 event leave 511's list or
# come back to it: what Beacon saw at that check, in the past tense. A later check
# can have seen otherwise, and a point that cites this row alone does not show it.
CHANGE_SAID = {
    "not_listed": (
        "When Beacon checked, 511 was no longer listing a traffic event it had listed. "
        "That is what Beacon saw then, not an official reopening."
    ),
    "listed_again": (
        "When Beacon checked, 511 was listing again a traffic event it had stopped listing."
    ),
}
# What is said of a detail a call gave: what was said on the radio then, in the past
# tense. A story's sentence is read apart from its line, and "rescue in progress"
# from a call of half an hour ago is not in progress now. No sentence says who said
# it or who did it: a row keeps the detail and not the speaker, and a dispatcher who
# relays a request for police is no crew that requested them.
DETAIL_SAID = {
    "smoke_showing": "Radio traffic reported smoke showing.",
    "fire_showing": "Radio traffic reported fire showing.",
    "working_fire": "Radio traffic reported a working fire declared.",
    "additional_alarm": "Radio traffic reported an additional alarm called.",
    "exposure_threatened": "Radio traffic reported neighbouring buildings threatened.",
    "rescue_in_progress": "Radio traffic reported a rescue in progress.",
    "evacuation": f"An evacuation was mentioned on the radio. {NOT_AN_ORDER}",
    "shelter_in_place": f"Sheltering in place was mentioned on the radio. {NOT_AN_ORDER}",
    "police_requested": "Radio traffic reported a request for police.",
    "utilities_requested": "Radio traffic reported a request for utilities.",
    "road_closed": "Radio traffic reported a road closed.",
    "aircraft_overhead": "Radio traffic mentioned an aircraft overhead.",
    "knocked_down": "Radio traffic reported the fire knocked down.",
    "overhaul": "Radio traffic reported overhaul under way.",
    "all_clear": f"All clear was said on the radio. {NOT_AN_ORDER}",
}

# What a point can be about. `status`, `alarm`, `units`, and each detail a call can
# give rest on radio lines; the last three on a line of their own kind.
POINT_KINDS: tuple[str, ...] = (
    "status",
    "alarm",
    "units",
    *DETAIL_SAID,
    "closure",
    "aircraft",
    "taps",
)

INSTRUCTIONS = """\
You choose what goes into the short account of one incident for Beacon, an app that \
tells people in San Francisco what the sirens near them are about.

You are given a JSON document: the incident as Beacon's own code has put it together, \
and numbered lines, each one thing a source said (dispatch radio as it was logged, a \
511 traffic event, an aircraft's track, taps in the app).

Choose at most eight points that tell what the lines add up to, in the order a reader \
should see them: how far the call has got, how large it is, who is on it, and what \
else was seen. For each point give its kind and the numbers of the lines that show it.

- status: one radio line, the one that says how far the call has got now.
- alarm: the radio line that gives the alarm level as it is now, and others only \
when they give the same level.
- units: radio lines that name the units on the call.
- a detail (smoke_showing, evacuation, knocked_down, and the others): the radio lines \
that say it.
- closure, aircraft, taps: one line of that kind.

Beacon checks each point against the lines it cites and writes the sentence itself. \
A point whose lines do not show it is thrown away. Write nothing else.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(POINT_KINDS)},
                    "cites": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["kind", "cites"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["points"],
    "additionalProperties": False,
}


class StoryError(Exception):
    """Ember answered, and its answer is not the form it was asked for. The text is
    fixed and says nothing of the answer. The call may have been paid for."""


@dataclass(frozen=True)
class Claim:
    """One sentence of a story, and what it rests on."""

    # What it is about, from `POINT_KINDS`. Ember chose it; code checked it.
    kind: str
    # Written by code from the rows of the lines it cites. None of it is Ember's.
    text: str
    # When the newest of those lines is from.
    at: datetime
    # The least confidence among the lines it cites, worked out by code.
    confidence: Confidence
    # The sources of the lines it cites.
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Draft:
    """What `merge_incident` made of one answer."""

    claims: tuple[Claim, ...]
    # How many points Ember gave that were left out: one that cited nothing that is
    # there, one its lines do not show, and one that says again what another said.
    left_out: int
    # How many lines Ember was given, and how many older ones it was not.
    lines: int
    lines_left_out: int
    # Which kind of Ember client chose the points: `live`, or `fake` for a stand-in.
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int


def lines_of(incident: Incident) -> tuple[list[Entry], int]:
    """The lines of an incident that Ember is given, oldest first, and how many
    older ones are left out because there are more than `MAX_LINES`."""
    lines = [entry for entry in incident.timeline if entry.kind in LINE_KINDS]
    return lines[-MAX_LINES:], max(0, len(lines) - MAX_LINES)


def basis_of(incident: Incident) -> str:
    """Names what an incident's story would be made from: a digest of the lines
    Ember would be given. Two readings of an incident with the same basis would get
    the same request, and a story is the story of the basis it was made from."""
    lines, _ = lines_of(incident)
    return hashlib.sha256(_document(incident, lines).encode()).hexdigest()[:32]


def merge_incident(ember: EmberClient, incident: Incident) -> Draft:
    """Have Ember choose the points of an incident's story from its lines.

    Raises ValueError for a medical response and for an incident with no line to
    tell, before anything is asked. Raises StoryError when Ember's answer is not the
    form, and what `EmberClient.respond` raises."""
    if incident.category == MEDICAL_EVENT:
        raise ValueError("A medical response is its crews and its block, and has no story.")
    lines, lines_left_out = lines_of(incident)
    if not lines:
        raise ValueError("The incident has no line a story could be made from.")
    answer = ember.respond(
        task=TASK,
        effort=EFFORT,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        sensitive=True,
        instructions=INSTRUCTIONS,
        input=_document(incident, lines),
        text={
            "format": {
                "type": "json_schema",
                "name": "incident_story",
                "strict": True,
                "schema": SCHEMA,
            }
        },
    )
    # An answer that was cut short is no form, whatever of it reads as one.
    points = _points(answer.output_text) if answer.status == "completed" else None
    if points is None:
        raise_bare(StoryError("Ember's answer about an incident is not a filled-in form."))
    claims: dict[tuple[str, tuple[int, ...]], Claim] = {}
    for kind, cites in points[:MAX_POINTS]:
        cited = _cited(cites, lines)
        claim = None if cited is None else point_claim(kind, [lines[n - 1] for n in cited])
        if claim is not None:
            # A point that says again what another said is counted as left out.
            claims.setdefault((claim.kind, tuple(cited or ())), claim)
    usage = answer.usage
    return Draft(
        claims=tuple(claims.values()),
        left_out=len(points) - len(claims),
        lines=len(lines),
        lines_left_out=lines_left_out,
        model=answer.source,
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cost_usd=answer.cost_usd,
        latency_ms=answer.latency_ms,
    )


def point_claim(kind: object, cited: Sequence[Entry]) -> Claim | None:
    """The sentence for a point of `kind` that cites these lines, or None when the
    lines do not show it. Every line has to: a point is no truer for a line beside
    it that says something else."""
    if not isinstance(kind, str) or not cited:
        return None
    text = _sentence(kind, cited)
    if text is None:
        return None
    sources: dict[tuple[str, int | None], Source] = {}
    for entry in cited:
        for source in entry.sources:
            sources.setdefault((source.source, source.evidence_id), source)
    return Claim(
        kind=kind,
        text=text,
        at=max(entry.at for entry in cited),
        confidence=weakest([entry.confidence for entry in cited]),
        sources=tuple(sources.values()),
    )


def _sentence(kind: str, cited: Sequence[Entry]) -> str | None:
    """The words for a point, from the fields the cited lines were written from."""
    details = [entry.details for entry in cited]
    if kind in ("closure", "aircraft", "taps"):
        if len(cited) != 1 or cited[0].kind != kind:
            return None
        said = details[0]
        if kind == "closure" and "change" in said:
            # What Beacon saw at one check, said as that. None for a change this
            # does not know.
            return CHANGE_SAID.get(said["change"])
        if kind == "closure" and said.get("state") != "in_force":
            # A version of a 511 event that is not the one in force: its words are
            # not what 511 says now. The row shows that 511 listed an event, and
            # that is all the sentence says. What came after it (a later version,
            # or Beacon no longer seeing it listed) is another row's to show, which
            # this point does not cite.
            return OLD_CLOSURE
        # The engine's own line, as the timeline has it: for the version of a 511
        # event that is in force that is 511's words, said as 511's, and an
        # aircraft and taps are told there in the past tense already.
        return cited[0].text
    if any(entry.kind != "radio" for entry in cited):
        return None
    if kind == "status":
        status = details[0].get("status")
        if len(cited) != 1 or status not in STATUS_WORDS or status == "unknown":
            return None
        return f"The radio said: {STATUS_WORDS[status]}."
    if kind == "alarm":
        levels = [said.get("alarm") for said in details]
        if not all(isinstance(level, int) and not isinstance(level, bool) for level in levels):
            return None
        # Every cited call has to give the same level: the sentence has the time
        # of the newest of them, and an older, higher level is not the level then.
        if len(set(levels)) != 1:
            return None
        return f"The radio said: alarm level {levels[0]}."
    if kind == "units":
        named: dict[str, None] = {}
        for said in details:
            units = [unit for unit in said.get("units") or [] if isinstance(unit, str)]
            if not units:
                return None
            named.update(dict.fromkeys(units))
        shown = list(named)[:MAX_UNITS]
        more = ", and others" if len(named) > MAX_UNITS else ""
        return f"Units named on the radio: {', '.join(shown)}{more}."
    if kind in DETAIL_SAID:
        if not all(kind in (said.get("details") or []) for said in details):
            return None
        return DETAIL_SAID[kind]
    return None


def _document(incident: Incident, lines: Sequence[Entry]) -> str:
    """The request's input: the incident and its lines as JSON, the same text for
    the same lines."""
    return json.dumps(
        {
            "incident": {
                "title": incident.title,
                "category": incident.category,
                "status": incident.status,
                "place": incident.place,
                "alarm": incident.alarm,
                "units": list(incident.units),
            },
            "lines": [
                {
                    "n": place,
                    "at": entry.at.isoformat(),
                    "kind": entry.kind,
                    "confidence": entry.confidence,
                    "text": entry.text,
                }
                for place, entry in enumerate(lines, start=1)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _points(text: str) -> list[tuple[object, object]] | None:
    """The points of an answer, each as what it gave for its kind and its lines, or
    None when the answer is not the form. Nothing else of the answer is read."""
    try:
        answer = json.loads(text)
    except ValueError:
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("points"), list):
        return None
    return [
        (point.get("kind"), point.get("cites")) if isinstance(point, dict) else (None, None)
        for point in answer["points"]
    ]


def _cited(cites: object, lines: Sequence[Entry]) -> list[int] | None:
    """The numbers of the lines a point cites, in order and once each, or None when
    it cites none, too many, or one that is not there."""
    if not isinstance(cites, list) or not 1 <= len(cites) <= MAX_CITES:
        return None
    numbers = [n for n in cites if isinstance(n, int) and not isinstance(n, bool)]
    if len(numbers) != len(cites) or not all(1 <= n <= len(lines) for n in numbers):
        return None
    return sorted(set(numbers))
