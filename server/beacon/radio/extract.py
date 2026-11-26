"""`extract_transmission`: one radio call, heard by Ember, as a `Transmission`.

Ember is given the audio and a form to fill in, and what comes back is checked field
by field against the closed vocabulary (`beacon.radio.vocabulary`): tags from fixed
lists, and units and streets that are on the caller's lists of them (`Known`). A
`Transmission` holds nothing else: no transcript, no free text, no house number. So
a name, a patient's condition, or an address that Ember heard or wrote has no field
to arrive in. `beacon.radio.vocabulary` says what little that leaves open.

Every call is made with `sensitive=True`, because any radio call can be EMS traffic
and nobody knows before it is heard. The Ember client then keeps the call's
accounting and none of its audio or its answer, and the call cannot be replayed: it
is paid for each time it is made.

A medical call is kept as "medical response" with the units and the block, and
nothing more: no status, no details, no alarm. That is so for a call Ember hears as
medical and for every call on a channel the caller says is an EMS one.
"""

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal, get_args

from beacon.ember import EmberClient
from beacon.errors import raise_bare
from beacon.evidence import ModelSource
from beacon.radio.audio import EmberFormat
from beacon.radio.vocabulary import (
    CATEGORIES,
    DETAILS,
    MAX_ALARM,
    MAX_UNITS,
    MEDICAL_EVENT,
    STATUSES,
    Category,
    Detail,
    Known,
    Status,
    block_of,
    place_in_words,
)

TASK = "extract_transmission"
# The form is about eighty tokens. The rest is room for the little reasoning that low
# effort does, which counts against the same limit.
MAX_OUTPUT_TOKENS = 512

# How well the call could be heard, in Ember's own judgment.
Clarity = Literal["clear", "partial", "unintelligible"]
CLARITIES: tuple[str, ...] = get_args(Clarity)

INSTRUCTIONS = """\
You log San Francisco fire and EMS dispatch radio for a public incident feed. You are \
given one radio call. Fill in the form about it, from what is said in the call and \
nothing else.

Rules:
- Never give a person's name, a patient's age, sex, condition, or complaint, a phone \
number, or a house number. The form has no place for them.
- `clarity`: `clear` when you could follow the call, `partial` when you caught some of \
it, `unintelligible` when it is noise, a tone, or too broken to follow.
- `category`: what the call is about. `medical` for any medical or EMS call. \
`administrative` for radio checks, status changes, and housekeeping. `other` when none fits.
- `status`: where the incident stands according to this call, or `unknown`.
- `details`: every tag the call supports, and no others. `additional_alarm` when a \
second or greater alarm is called.
- `units`: the units named, as written on the rig: E36, T6, B2, M52, RS1.
- `alarm`: the alarm level when one is said (2 for a second alarm), else null.
- `street`: the street the incident is on, as a name with its type (Hayes St, 19th \
Ave), else null. `cross_street`: the cross street, when one is given, else null.
- `block`: the hundred block of the address, when an address is given: 1458 Hayes is \
1400. Never the house number itself. Else null.
- When a field is not said in the call, use null, `unknown`, or an empty list. Do not guess.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clarity": {"type": "string", "enum": list(CLARITIES)},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "status": {"type": "string", "enum": list(STATUSES)},
        "details": {"type": "array", "items": {"type": "string", "enum": list(DETAILS)}},
        "units": {"type": "array", "items": {"type": "string"}},
        "alarm": {"type": ["integer", "null"]},
        "street": {"type": ["string", "null"]},
        "block": {"type": ["integer", "null"]},
        "cross_street": {"type": ["string", "null"]},
    },
    "required": [
        "clarity",
        "category",
        "status",
        "details",
        "units",
        "alarm",
        "street",
        "block",
        "cross_street",
    ],
    "additionalProperties": False,
}

_QUESTION = "Fill in the form for this radio call."


class ExtractionError(Exception):
    """Ember answered, and the answer is not a form. The text is fixed and says
    nothing of the call or the answer. The call was made, and may have been paid for."""


@dataclass(frozen=True)
class Transmission:
    """What is kept of one radio call. Every field is from a fixed list, a unit or
    a street on the lists the caller gave (`Known`), or a number."""

    clarity: Clarity
    category: Category
    status: Status
    details: tuple[Detail, ...]
    units: tuple[str, ...]
    alarm: int | None
    street: str | None
    # The hundred block: 400 for any address from 400 to 499.
    block: int | None
    cross_street: str | None
    # Which kind of Ember client heard the call: `live`, or `fake` for a stand-in.
    heard_by: ModelSource

    @property
    def event(self) -> str:
        """What the call is called where it is shown: `medical response` for a
        medical call, and the category for any other."""
        return MEDICAL_EVENT if self.category == "medical" else self.category

    @property
    def place(self) -> str | None:
        """The place in words, no finer than a block: `400 block of Hayes St`."""
        return place_in_words(self.street, self.block, self.cross_street)

    def as_data(self) -> dict[str, Any]:
        """The transmission as JSON, which is what an evidence row stores of it."""
        return {
            "event": self.event,
            "category": self.category,
            "clarity": self.clarity,
            "status": self.status,
            "details": list(self.details),
            "units": list(self.units),
            "alarm": self.alarm,
            "street": self.street,
            "block": self.block,
            "cross_street": self.cross_street,
        }


def extract_transmission(
    ember: EmberClient,
    audio: bytes,
    audio_format: EmberFormat,
    *,
    known: Known,
    ems_channel: bool = False,
) -> Transmission:
    """Have Ember hear one radio call. `audio` is a whole WAV or MP3 file. `known`
    lists the units and the streets that may be kept of it; one that is not listed
    is dropped. `ems_channel` says the call is from a channel that carries EMS
    traffic only, and it is then kept as a medical response whatever Ember made of it.

    Raises ExtractionError when the answer is not a form, and what
    `EmberClient.respond` raises: a `beacon.ember.EmberError`, whose text for a
    sensitive call is fixed. Do not make the call again after either: it may have
    been paid for."""
    answer = ember.respond(
        task=TASK,
        effort="low",
        max_output_tokens=MAX_OUTPUT_TOKENS,
        # Any call can be EMS traffic, and nobody knows before it is heard.
        sensitive=True,
        instructions=INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _QUESTION},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(audio).decode(),
                            "format": audio_format,
                        },
                    },
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "transmission",
                "strict": True,
                "schema": SCHEMA,
            }
        },
    )
    form = _form(answer.output_text) if answer.status == "completed" else None
    if form is None:
        raise_bare(ExtractionError("Ember's answer about a radio call was not a filled-in form."))
    return read_form(form, heard_by=answer.source, known=known, ems_channel=ems_channel)


def read_form(
    form: dict[str, Any], *, heard_by: ModelSource, known: Known, ems_channel: bool = False
) -> Transmission:
    """A `Transmission` from a filled-in form, keeping only what the vocabulary
    allows and `known` lists. A field that does not fit is left empty, not passed
    on: the form is a model's answer, and a model can put anything in it."""
    clarity = _one_of(form.get("clarity"), CLARITIES, "partial")
    category = _one_of(form.get("category"), CATEGORIES, "other")
    if ems_channel:
        category = "medical"
    if clarity == "unintelligible" or category == "unintelligible":
        # Nothing was heard, so nothing is said to have been.
        return Transmission(
            clarity="unintelligible",
            category="medical" if ems_channel else "unintelligible",
            status="unknown",
            details=(),
            units=(),
            alarm=None,
            street=None,
            block=None,
            cross_street=None,
            heard_by=heard_by,
        )
    medical = category == "medical"
    units = form.get("units")
    details = form.get("details")
    alarm = form.get("alarm")
    street = known.street(form.get("street"))
    return Transmission(
        clarity=clarity,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        # Of a medical call only the crews and the block are kept.
        status="unknown" if medical else _one_of(form.get("status"), STATUSES, "unknown"),  # type: ignore[arg-type]
        details=()
        if medical
        else tuple(
            dict.fromkeys(
                detail
                for detail in (details if isinstance(details, list) else [])
                if isinstance(detail, str) and detail in DETAILS
            )
        ),  # type: ignore[arg-type]
        units=tuple(
            dict.fromkeys(
                unit
                for value in (units if isinstance(units, list) else [])
                if (unit := known.unit(value)) is not None
            )
        )[:MAX_UNITS],
        alarm=alarm
        if not medical
        and isinstance(alarm, int)
        and not isinstance(alarm, bool)
        and 1 <= alarm <= MAX_ALARM
        else None,
        street=street,
        # A block or a cross street without the street says nothing.
        block=None if street is None else block_of(form.get("block")),
        cross_street=None if street is None else known.street(form.get("cross_street")),
        heard_by=heard_by,
    )


def _form(text: str) -> dict[str, Any] | None:
    try:
        form = json.loads(text)
    except ValueError:
        return None
    return form if isinstance(form, dict) else None


def _one_of(value: Any, allowed: tuple[str, ...], otherwise: str) -> str:
    return value if isinstance(value, str) and value in allowed else otherwise
