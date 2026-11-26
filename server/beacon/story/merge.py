"""`merge_incident`: Ember tells what an incident's radio, closures, aircraft, and taps
add up to, in a few sentences, each of which names the lines it rests on.

The incident engine has already decided what belongs to the incident and written a
line for each thing a source said (`beacon.incident`). This call adds the one thing
plain code does not do well: saying in a few sentences what those lines come to. It
decides nothing. What Ember is given, and what is kept of its answer:

- **Ember is given only the lines of radio, 511 events, aircraft, and taps**, as the
  engine wrote them. It is given no alert and no notice, and may not write about
  one: what an authority said is shown by code, in the authority's words or as a
  labeled reading beside its link (`Incident.official`), and never in a model's
  retelling. A sentence that uses a word for an official act (`order`, `official`,
  `alert`, `warning`, `notice`) is left out.
- **Every sentence cites lines, and code gives it its sources and its confidence**:
  the sources of the lines it cites, and the least confidence among them. A
  sentence that cites nothing, or a line that is not there, is left out.
- **Every sentence passes the redaction pass in Beacon's own voice**
  (`beacon.redact.own`): ordinary words and listed names only, no word that tells a
  reader what to do, no number in front of a street. One that does not pass is
  left out whole.
- **A sentence about an evacuation, sheltering, or an all-clear rests on radio
  alone**, because Ember was given nothing else, so code puts beside it the words
  "That is radio traffic, not an official order."
- **A medical response has no story.** It is its crews and its block, which the
  engine already says, and `merge_incident` refuses it.

What this cannot do: nothing checks that a sentence is true to the lines it cites.
Its sources and its confidence say what it rests on, and the lines themselves are
shown beside it.

The call is `sensitive=True`. Its input is made of code's own words and 511's, and
its answer is unread text from a model until the pass above has read it, so the
ledger and the trace keep the call's accounting and none of its text. What is kept
of the answer is the story, once every sentence of it has passed.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from beacon.ember import EmberClient
from beacon.errors import raise_bare
from beacon.evidence import Confidence
from beacon.incident import Entry, Incident, Source, weakest
from beacon.radio.vocabulary import MEDICAL_EVENT
from beacon.redact import Names, own

TASK = "merge_incident"
MAX_OUTPUT_TOKENS = 1500

# The kinds of line Ember is given, and so the only ones a sentence can cite.
LINE_KINDS = ("radio", "closure", "aircraft", "taps")
# The newest lines are given, and no more than this many.
MAX_LINES = 60
MAX_CLAIMS = 8
MAX_CLAIM_CHARS = 280
MAX_HEADLINE_CHARS = 100
MAX_CITES = 6

NOT_AN_ORDER = "That is radio traffic, not an official order."

INSTRUCTIONS = """\
You write the short account of one incident for Beacon, an app that tells people in \
San Francisco what the sirens near them are about.

You are given a JSON document: the incident as Beacon's own code has put it together, \
and numbered lines, each one thing a source said (dispatch radio as it was logged, a \
511 traffic event, an aircraft's track, taps in the app). Write a headline and at most \
eight short sentences that say what the lines add up to: what kind of call it is, \
where, how far it has got, who is on it, and what else was seen.

Rules:
- Say only what the lines say. For each sentence, give the numbers of the lines it \
rests on. A sentence with no line is thrown away.
- Plain words. Name only streets, units, and agencies that appear in the lines, \
spelled as they are there. Never name or describe a person. Give a place no finer \
than a block.
- Never tell the reader what to do, and never write "you".
- Do not write about official alerts, orders, warnings, or notices, and do not use \
those words: Beacon shows what authorities said itself, beside its source.
- Radio traffic is what crews said, not what is confirmed. Write "crews report" or \
"the radio says" where it matters.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": ["string", "null"]},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "cites"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "claims"],
    "additionalProperties": False,
}

# A word for an official act. Ember is given no official source, so a sentence with
# one of these says more than its lines can.
_OFFICIAL = re.compile(
    r"\b(?:official\w*|authorit\w+|orders?|ordered|warnings?|alerts?|notices?|advisor\w+|"
    r"published|issued|declared|mandatory)\b",
    re.IGNORECASE,
)
# What crews can say on the radio that an authority can also order.
_ORDER_LIKE = re.compile(r"evacuat|shelter|all[\s-]+clear", re.IGNORECASE)


class StoryError(Exception):
    """Ember answered, and its answer is not the form it was asked for. The text is
    fixed and says nothing of the answer. The call may have been paid for."""


@dataclass(frozen=True)
class Claim:
    """One sentence of a story, and what it rests on."""

    # Ember's words, after the redaction pass let the whole sentence through.
    text: str
    # The least confidence among the lines it cites, worked out by code.
    confidence: Confidence
    # The sources of the lines it cites.
    sources: tuple[Source, ...]
    # Words code puts beside it, or None: `NOT_AN_ORDER` for a sentence about an
    # evacuation, sheltering, or an all-clear.
    note: str | None = None


@dataclass(frozen=True)
class Draft:
    """What `merge_incident` made of one answer."""

    headline: str | None
    claims: tuple[Claim, ...]
    # How many sentences Ember wrote that were left out: one that did not pass the
    # redaction pass, cited nothing that is there, or spoke of an official act.
    left_out: int
    # How many lines Ember was given, and how many older ones it was not.
    lines: int
    lines_left_out: int
    # Which kind of Ember client wrote it: `live`, or `fake` for a stand-in.
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
    """Names what an incident's story would be written from: a digest of the lines
    Ember would be given. Two readings of an incident with the same basis would get
    the same request, and a story is the story of the basis it was written from."""
    lines, _ = lines_of(incident)
    return hashlib.sha256(_document(incident, lines).encode()).hexdigest()[:32]


def merge_incident(ember: EmberClient, incident: Incident, names: Names) -> Draft:
    """Have Ember write the story of an incident from its lines.

    Raises ValueError for a medical response and for an incident with no line to
    tell, before anything is asked. Raises StoryError when Ember's answer is not the
    form, and what `EmberClient.respond` raises."""
    if incident.category == MEDICAL_EVENT:
        raise ValueError("A medical response is its crews and its block, and has no story.")
    lines, lines_left_out = lines_of(incident)
    if not lines:
        raise ValueError("The incident has no line a story could be written from.")
    answer = ember.respond(
        task=TASK,
        effort="low",
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
    form = _form(answer.output_text)
    if form is None:
        raise_bare(StoryError("Ember's answer about an incident is not a filled-in form."))
    headline, written = form
    claims: list[Claim] = []
    for text, cites in written[:MAX_CLAIMS]:
        claim = _claim(text, cites, lines, names)
        if claim is not None:
            claims.append(claim)
    usage = answer.usage
    return Draft(
        headline=_headline(headline, names),
        claims=tuple(claims),
        left_out=len(written) - len(claims),
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


def _form(text: str) -> tuple[object, list[tuple[object, object]]] | None:
    """The headline and the sentences of an answer, each sentence as what it gave
    for its text and its lines, or None when the answer is not the form."""
    try:
        answer = json.loads(text)
    except ValueError:
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list):
        return None
    written = [
        (claim.get("text"), claim.get("cites")) if isinstance(claim, dict) else (None, None)
        for claim in answer["claims"]
    ]
    return answer.get("headline"), written


def _claim(text: object, cites: object, lines: Sequence[Entry], names: Names) -> Claim | None:
    """A sentence Ember wrote, when it may be shown, with what code adds to it."""
    if not isinstance(text, str) or len(text) > MAX_CLAIM_CHARS:
        return None
    shown = own(text, names)
    if shown is None or _OFFICIAL.search(shown):
        return None
    if not isinstance(cites, list) or not 1 <= len(cites) <= MAX_CITES:
        return None
    numbers = [n for n in cites if isinstance(n, int) and not isinstance(n, bool)]
    if len(numbers) != len(cites) or not all(1 <= n <= len(lines) for n in numbers):
        return None
    cited = [lines[n - 1] for n in sorted(set(numbers))]
    sources: dict[tuple[str, int | None], Source] = {}
    for entry in cited:
        for source in entry.sources:
            sources.setdefault((source.source, source.evidence_id), source)
    return Claim(
        text=shown,
        confidence=weakest([entry.confidence for entry in cited]),
        sources=tuple(sources.values()),
        note=NOT_AN_ORDER if _ORDER_LIKE.search(shown) else None,
    )


def _headline(text: object, names: Names) -> str | None:
    """Ember's headline when it may be shown, else None: whoever shows the story
    then uses the incident's own title. A headline has no room for a note, so one
    that speaks of an evacuation is left out as one that speaks of an order is."""
    if not isinstance(text, str) or len(text) > MAX_HEADLINE_CHARS:
        return None
    shown = own(text, names)
    if shown is None or _OFFICIAL.search(shown) or _ORDER_LIKE.search(shown):
        return None
    return shown
