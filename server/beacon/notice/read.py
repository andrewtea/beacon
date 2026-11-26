"""`read_notice`: an official notice, read by Ember, as a `Notice`.

A notice is a document an authority published: an evacuation order, a shelter-in-place
order, a road closure, an all-clear, a press release. Ember is given the PDF and a
form to fill in. Nothing in Beacon's own process parses the PDF.

Unlike a radio call, a notice is about what an authority told the public, so the form
has text fields: the notice's title, the areas it applies to, its instruction to the
public, and a short summary. They are kept as Ember gave them, with control
characters removed, and nothing else about them can be checked. What follows from
that is deliberate:

- **What is kept is Ember's reading of a notice, and not the notice.** The document
  is the authority speaking; the reading is a model's, and can be wrong or leave
  something out. A row made from one says which Ember read it and links the
  document, and whoever shows it says it is a reading and sends people to the
  document for what to do.
- **A text is kept whole or not at all.** An instruction with its end cut off can
  say the opposite of the instruction: "residents may return" without "except to
  these addresses". So a text that is longer than Beacon keeps, or a list of areas
  with more entries than it keeps, is left out, and the notice says which of its
  fields are not all there (`incomplete`).
- **Text is kept only of a notice that tells the public something** (`TEXT_TYPES`): an
  order, a warning, a closure, an advisory, an all-clear. Such a document is written
  to be repeated, and names places, not people. A press release is where a victim is
  named and a home is described, so of a press release, and of anything Ember does
  not take for a notice, Beacon keeps what it is, what hazard it is about, and its
  times, and nothing a model could write a name or an address into: no text, no
  issuer, and no streets.
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
# The form with every text field as full as Beacon keeps it is about fifteen hundred
# tokens. The rest is room for the reasoning low effort does, which counts against
# the same limit.
MAX_OUTPUT_TOKENS = 2500
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
# The longest each text may be. A longer one is left out whole, not cut.
_MAX_NAME = 200
_MAX_AREA = 300
_MAX_INSTRUCTION = 1500
_MAX_SUMMARY = 600

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
- `instruction`: everything the notice tells the public to do, in the document's own \
words, with every exception and condition it gives. Null when it tells them nothing.
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
    # The notice's words, as Ember gave them. All of these, and `issuer` and
    # `title`, are empty for a notice whose type is not one of `TEXT_TYPES`.
    areas: tuple[str, ...]
    # Street names, without numbers.
    streets: tuple[str, ...]
    instruction: str | None
    summary: str | None
    # The fields of which this does not hold all that Ember gave, because it was
    # more than Beacon keeps: a text or the areas, left out whole, or the streets,
    # of which the first are kept. Whoever shows the notice says it is not all here.
    incomplete: tuple[str, ...]
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
            "incomplete": list(self.incomplete),
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
    """A `Notice` from a filled-in form. A field of the wrong kind is left empty, and
    a street that is not shaped like one is dropped. A text that is longer than
    Beacon keeps is left out whole, and so are the areas when there are more of them
    than it keeps or one of them is too long: the notice names those fields in
    `incomplete`. Of a notice whose type is not one of `TEXT_TYPES`, nothing a model
    could have written freely is kept at all."""
    notice_type = _one_of(form.get("notice_type"), NOTICE_TYPES, "other")
    hazard = _one_of(form.get("hazard"), HAZARDS, "other")
    issued_at = _moment(form.get("issued_at"))
    effective_until = _moment(form.get("effective_until"))
    if notice_type not in TEXT_TYPES:
        return Notice(
            notice_type=notice_type,  # type: ignore[arg-type]
            hazard=hazard,  # type: ignore[arg-type]
            issuer=None,
            title=None,
            issued_at=issued_at,
            effective_until=effective_until,
            areas=(),
            streets=(),
            instruction=None,
            summary=None,
            incomplete=(),
            read_by=read_by,
        )
    incomplete: list[str] = []

    def whole(name: str, limit: int) -> str | None:
        text = _text(form.get(name))
        if text is not None and len(text) > limit:
            incomplete.append(name)
            return None
        return text

    issuer, title = whole("issuer", _MAX_NAME), whole("title", _MAX_NAME)
    given = form.get("areas")
    areas = tuple(
        dict.fromkeys(
            area
            for value in (given if isinstance(given, list) else [])
            if (area := _text(value)) is not None
        )
    )
    if len(areas) > MAX_AREAS or any(len(area) > _MAX_AREA for area in areas):
        incomplete.append("areas")
        areas = ()
    given = form.get("streets")
    streets = tuple(
        dict.fromkeys(
            street
            for value in (given if isinstance(given, list) else [])
            if (street := clean_street(value)) is not None
        )
    )
    if len(streets) > MAX_STREETS:
        # An index of names and no instruction, so the first of them are of use.
        incomplete.append("streets")
        streets = streets[:MAX_STREETS]
    instruction, summary = whole("instruction", _MAX_INSTRUCTION), whole("summary", _MAX_SUMMARY)
    return Notice(
        notice_type=notice_type,  # type: ignore[arg-type]
        hazard=hazard,  # type: ignore[arg-type]
        issuer=issuer,
        title=title,
        issued_at=issued_at,
        effective_until=effective_until,
        areas=areas,
        streets=streets,
        instruction=instruction,
        summary=summary,
        incomplete=tuple(incomplete),
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


def _text(value: Any) -> str | None:
    """A text on one line, without control characters, or None for what is no text
    or is empty."""
    if not isinstance(value, str):
        return None
    return " ".join("".join(char if char.isprintable() else " " for char in value).split()) or None


def _moment(value: Any) -> datetime | None:
    """A time with its zone, or None. A time with no zone is no moment."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None
