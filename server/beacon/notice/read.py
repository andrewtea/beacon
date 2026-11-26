"""`read_notice`: an official notice, read by Ember, as a `Notice`.

A notice is a document an authority published: an evacuation order, a shelter-in-place
order, a road closure, an all-clear, a press release. Ember is given the PDF and a
form to fill in. Nothing in Beacon's own process parses the PDF.

Unlike a radio call, a notice is meant to be relayed in the authority's own words, so
the form has text fields: the notice's title, the areas it applies to, its instruction
to the public, and a short summary. They are kept as Ember gave them, cut to a length,
with control characters removed, and nothing else about them can be checked. What
follows from that is deliberate:

- **What is kept of a notice is only as right as Ember's reading of it.** A row made
  from one says which Ember read it and links the document, and whoever shows it
  links the document too.
- **Text is kept only of a notice that tells the public something** (`TEXT_TYPES`): an
  order, a warning, a closure, an advisory, an all-clear. Such a document is written
  to be repeated, and names places, not people. A press release is where a victim is
  named and a home is described, so of a press release, and of anything Ember does
  not take for a notice, Beacon keeps what it is, who issued it, its times, and the
  streets it names, and none of its text. The streets are names in the one shape
  `beacon.radio.vocabulary.clean_street` gives them, without numbers.
- **An order names places finer than a block**, because that is what an order is: a
  list of the addresses that must leave. Beacon's rule against house numbers is for
  radio traffic about people's emergencies, not for an authority's published order.
  They stay in `areas` and `instruction`, and never become a row's `place`.

Ember is told not to give a private person's name, age, or condition whatever the
document says. That is an instruction to a model, not a guarantee: the redaction pass
that follows ingest still reads a notice's text before it is shown.

The Ember call is made with `sensitive=True`. A document at an address somebody
listed can turn out to be anything, and the Ember ledger and trace are exported and
published. So they keep the call's accounting and none of its text, and the call is
paid for each time it is made.
"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from beacon.ember import EmberClient
from beacon.errors import raise_bare
from beacon.evidence import ModelSource
from beacon.radio.vocabulary import clean_street

TASK = "read_notice"
# The form with every text field full is about four hundred tokens. The rest is room
# for the reasoning low effort does, which counts against the same limit.
MAX_OUTPUT_TOKENS = 1500
# A notice is a page or a few. A longer document is not one, and is not sent.
MAX_PDF_BYTES = 5_000_000

NoticeType = Literal[
    "evacuation_order",
    "evacuation_warning",
    "shelter_in_place",
    "road_closure",
    "all_clear",
    "advisory",
    "press_release",
    "other",
]
Hazard = Literal[
    "fire",
    "smoke",
    "hazmat",
    "flood",
    "earthquake",
    "weather",
    "utility",
    "police_activity",
    "other",
    "none",
]
NOTICE_TYPES: tuple[str, ...] = get_args(NoticeType)
HAZARDS: tuple[str, ...] = get_args(Hazard)

# The types that tell people to do something, or that they may stop.
ORDERS = frozenset({"evacuation_order", "evacuation_warning", "shelter_in_place", "all_clear"})
# The types whose text is kept: the ones written to be repeated to the public.
TEXT_TYPES = ORDERS | {"road_closure", "advisory"}

MAX_AREAS = 12
MAX_STREETS = 30
_MAX_NAME = 160
_MAX_AREA = 240
_MAX_INSTRUCTION = 800
_MAX_SUMMARY = 500

INSTRUCTIONS = """\
You read official public-safety notices for a public incident feed in San Francisco. \
You are given one document as a PDF. Fill in the form about it, from what the document \
says and nothing else.

Rules:
- `notice_type`: what the document is. `evacuation_order` when people are told to leave, \
`evacuation_warning` when they are told to be ready to, `shelter_in_place` when they are \
told to stay inside, `all_clear` when an earlier order is lifted, `road_closure`, \
`advisory` for other official advice, `press_release` for a statement about an incident \
that orders nothing, and `other` for anything that is not a notice from an authority.
- `hazard`: what the notice is about, or `none`.
- `issuer`: the agency that issued it, as the document names it. `title`: its title.
- `issued_at` and `effective_until`: as ISO 8601 with a UTC offset, only when the \
document gives the date and the time. Else null.
- `areas`: each area the notice applies to, in the document's own words.
- `streets`: the streets it names, each as a name with its type (Hayes St).
- `instruction`: what the notice tells the public to do, in the document's own words. \
Null when it tells them nothing.
- `summary`: one or two plain sentences on what the notice says.
- Never give the name, the age, or the condition of a private person, even when the \
document does. An agency, an official's title, and a public information line are not private.
- When the document does not say something, use null or an empty list. Do not guess.
"""

_TEXT = {"type": ["string", "null"]}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notice_type": {"type": "string", "enum": list(NOTICE_TYPES)},
        "hazard": {"type": "string", "enum": list(HAZARDS)},
        "issuer": _TEXT,
        "title": _TEXT,
        "issued_at": _TEXT,
        "effective_until": _TEXT,
        "areas": {"type": "array", "items": {"type": "string"}},
        "streets": {"type": "array", "items": {"type": "string"}},
        "instruction": _TEXT,
        "summary": _TEXT,
    },
    "required": [
        "notice_type",
        "hazard",
        "issuer",
        "title",
        "issued_at",
        "effective_until",
        "areas",
        "streets",
        "instruction",
        "summary",
    ],
    "additionalProperties": False,
}

_QUESTION = "Fill in the form for this notice."


class NoticeError(Exception):
    """A notice could not be read: the document is not a PDF Beacon sends, or Ember's
    answer is not a form. The text is fixed and says nothing of the document or the
    answer. When Ember was asked, the call may have been paid for."""


@dataclass(frozen=True)
class Notice:
    """What is kept of one notice."""

    notice_type: NoticeType
    hazard: Hazard
    issuer: str | None
    title: str | None
    # When the notice says it was issued, and until when it applies.
    issued_at: datetime | None
    effective_until: datetime | None
    # In the notice's own words, as Ember gave them. Empty for a notice whose type
    # is not one of `TEXT_TYPES`, and so is `title`.
    areas: tuple[str, ...]
    # Street names, without numbers.
    streets: tuple[str, ...]
    instruction: str | None
    summary: str | None
    # Which kind of Ember client read it: `live`, or `fake` for a stand-in.
    read_by: ModelSource

    @property
    def is_order(self) -> bool:
        """Whether the notice tells people to do something, or that they may stop."""
        return self.notice_type in ORDERS

    def as_data(self) -> dict[str, Any]:
        """The notice as JSON, which is what an evidence row stores of it."""
        return {
            "notice_type": self.notice_type,
            "hazard": self.hazard,
            "issuer": self.issuer,
            "title": self.title,
            "issued_at": None if self.issued_at is None else self.issued_at.isoformat(),
            "effective_until": None
            if self.effective_until is None
            else self.effective_until.isoformat(),
            "areas": list(self.areas),
            "streets": list(self.streets),
            "instruction": self.instruction,
            "summary": self.summary,
        }


def is_pdf(document: bytes) -> bool:
    """Whether these bytes begin as a PDF file does."""
    return document[:5] == b"%PDF-"


def read_notice(ember: EmberClient, pdf: bytes) -> Notice:
    """Have Ember read one notice. `pdf` is a whole PDF file. A remote address is
    never handed to Ember: fetch the document and pass its bytes.

    Raises NoticeError when `pdf` is not a PDF of at most `MAX_PDF_BYTES`, in which
    case Ember is not asked, and when Ember's answer is not a form. Raises what
    `EmberClient.respond` raises: a `beacon.ember.EmberError`, whose text for a
    sensitive call is fixed. Do not make the call again after an error from Ember or
    about its answer: it may have been paid for."""
    if not is_pdf(pdf) or len(pdf) > MAX_PDF_BYTES:
        raise_bare(NoticeError("A notice is a PDF file of at most five megabytes."))
    answer = ember.respond(
        task=TASK,
        effort="low",
        max_output_tokens=MAX_OUTPUT_TOKENS,
        # See the module's docstring: the document can be anything.
        sensitive=True,
        instructions=INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _QUESTION},
                    {
                        "type": "input_file",
                        "filename": "notice.pdf",
                        "file_data": "data:application/pdf;base64,"
                        + base64.b64encode(pdf).decode(),
                    },
                ],
            }
        ],
        text={
            "format": {"type": "json_schema", "name": "notice", "strict": True, "schema": SCHEMA}
        },
    )
    form = _form(answer.output_text) if answer.status == "completed" else None
    if form is None:
        raise_bare(NoticeError("Ember's answer about a notice was not a filled-in form."))
    return read_form(form, read_by=answer.source)


def read_form(form: dict[str, Any], *, read_by: ModelSource) -> Notice:
    """A `Notice` from a filled-in form. A field of the wrong kind is left empty, a
    text is cut to its length, and a street that is not shaped like one is dropped.
    Of a notice whose type is not one of `TEXT_TYPES`, no text is kept at all."""
    notice_type = _one_of(form.get("notice_type"), NOTICE_TYPES, "other")
    worded = notice_type in TEXT_TYPES
    areas = form.get("areas")
    streets = form.get("streets")
    return Notice(
        notice_type=notice_type,  # type: ignore[arg-type]
        hazard=_one_of(form.get("hazard"), HAZARDS, "other"),  # type: ignore[arg-type]
        issuer=_text(form.get("issuer"), _MAX_NAME),
        title=_text(form.get("title"), _MAX_NAME) if worded else None,
        issued_at=_moment(form.get("issued_at")),
        effective_until=_moment(form.get("effective_until")),
        areas=tuple(
            dict.fromkeys(
                area
                for value in (areas if worded and isinstance(areas, list) else [])
                if (area := _text(value, _MAX_AREA)) is not None
            )
        )[:MAX_AREAS],
        streets=tuple(
            dict.fromkeys(
                street
                for value in (streets if isinstance(streets, list) else [])
                if (street := clean_street(value)) is not None
            )
        )[:MAX_STREETS],
        instruction=_text(form.get("instruction"), _MAX_INSTRUCTION) if worded else None,
        summary=_text(form.get("summary"), _MAX_SUMMARY) if worded else None,
        read_by=read_by,
    )


def _form(text: str) -> dict[str, Any] | None:
    """The form in Ember's answer, or None when the answer is not the whole form:
    every key of the schema, whatever its value."""
    try:
        form = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(form, dict) or not set(SCHEMA["required"]) <= form.keys():
        return None
    return form


def _one_of(value: Any, allowed: tuple[str, ...], otherwise: str) -> str:
    return value if isinstance(value, str) and value in allowed else otherwise


def _text(value: Any, limit: int) -> str | None:
    """A text on one line, without control characters, cut to `limit`."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return text[:limit].rstrip() or None


def _moment(value: Any) -> datetime | None:
    """A time with its zone, or None. A time with no zone is no moment."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None
