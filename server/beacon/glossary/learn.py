"""`learn_glossary`: the Ember call that proposes what each unit type code means.

A unit on the radio is a type code and a number: `E36`, `T6`, `RC2`. Beacon keeps
the id and knows what five of the codes mean, because a person wrote them down
(`beacon.radio.vocabulary`). The glossary is Beacon's attempt to learn the rest
from what it heard.

**It is learned from counts, not from words.** The build plan words this call as
"a day of transcripts in, proposed code meanings out". Beacon has no transcript and
must never have one: nothing a radio call said is stored as text. What it has is a
day's incidents, each with a category from a closed list and the units that were
named. So what Ember is given is, for each code, a `Pattern`: how many units have
it, how many incidents named one, how those incidents divide by category, how often
a unit of the code was the only one named, and how many units such an incident
names. That is a table of numbers and tags. It names no person, no place, and no
call, so this call is `sensitive=False`: its request and its answer are kept in the
ledger, and an answer that was recorded is replayed and not paid for again.

**Ember chooses from a closed list, and code checks the choice.** The form has one
entry for a code, and a role from `ROLES` or `unknown`; it has no field for a
sentence, a reason, or a confidence. An entry is kept only when its code is one
Ember was asked about, its role is on the list, and it is the first for its code;
anything else is left out and counted (`Glossary.left_out`), never mended. A form
that is not whole is no glossary.

**What a glossary is and is not.** It is a model's proposal, from one day's
pattern, of what kind of unit a code names. Nothing here checks that a proposal is
right: `beacon.scale.glossary` compares one with DataSF's own unit types, which
Ember is never shown. A model can also know a fire department's codes from
elsewhere, so a right answer is not proof that the counts showed it. A meaning is
shown as the glossary's proposal with who proposed it, and never as what a unit is.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from beacon.ember import EmberClient
from beacon.ember.client import Source

TASK = "learn_glossary"
# What a code can be said to name. A new kind of unit worth telling apart is a new
# role here, with its words below.
ROLES = (
    "engine",
    "truck",
    "medic",
    "chief",
    "rescue_squad",
    "rescue_captain",
    "ambulance",
    "community_paramedic",
    "support",
    "other",
)
# What Beacon knows without a glossary: the codes of the units a dispatcher names in
# full, which a person wrote down (`beacon.radio.vocabulary`). A battalion and a
# division are a chief officer's.
BUILT_IN = {"E": "engine", "T": "truck", "B": "chief", "D": "chief", "M": "medic"}
# The answer for a code whose pattern does not show what it names.
UNKNOWN = "unknown"
ROLE_WORDS = {
    "engine": "Engine",
    "truck": "Truck",
    "medic": "Medic unit",
    "chief": "Chief officer",
    "rescue_squad": "Rescue squad",
    "rescue_captain": "Rescue captain",
    "ambulance": "Ambulance that is not a medic unit",
    "community_paramedic": "Community paramedic or crisis response team",
    "support": "Support unit",
    "other": "Another kind of unit",
}
# The most codes one call asks about. A day in San Francisco has about twenty.
MAX_CODES = 40
_CODE = re.compile(r"[A-Z]{1,3}")
_MAX_OUTPUT_TOKENS = 2048

INSTRUCTIONS = (
    "You are given a table about one day of a city fire department's dispatch radio. "
    "Each row is about one unit type code: the letters in front of a unit's number, "
    "as in E36. For each code the table gives how many different units have it, how "
    "many incidents named a unit with it, how those incidents divide by category, in "
    "how many of them units of this code were the only units named, and the mean "
    "number of units an incident that names it has.\n"
    "For each code, say what kind of unit it names, choosing one role from the list "
    "in the form. Answer with the role `unknown` when the table does not show what a "
    "code names. Give one entry for each code in the table and none for any other."
)


@dataclass(frozen=True)
class Pattern:
    """What a day's incidents show of one unit type code. Every field is a count or
    a tag from a closed list."""

    code: str
    # Different units with the code, and the incidents that name one.
    units: int
    incidents: int
    # Those incidents by category (`beacon.radio.vocabulary.CATEGORIES`, with
    # `medical response` for a medical one), the most frequent first.
    by_category: tuple[tuple[str, int], ...]
    # Incidents in which units of this code are the only units named.
    alone: int
    # The mean number of units named in an incident that names one.
    mean_units: float

    def as_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "units": self.units,
            "incidents": self.incidents,
            "by_category": dict(self.by_category),
            "alone": self.alone,
            "mean_units": self.mean_units,
        }


@dataclass(frozen=True)
class Entry:
    """One code and the role proposed for it, which is one of `ROLES`."""

    code: str
    role: str


@dataclass(frozen=True)
class Glossary:
    """What `learn_glossary` came to."""

    # The codes that were given a role, in the order they were asked about. A code
    # answered `unknown` has no entry.
    entries: tuple[Entry, ...]
    # The codes Ember was asked about.
    asked: tuple[str, ...]
    # Entries of the form that were not kept: a code that was not asked about, a
    # role that is not on the list, or a second entry for a code.
    left_out: int
    # Whether the answer was the whole form. A glossary that is not whole has no entries.
    whole: bool
    # Which kind of Ember client proposed it: `live`, `replay` for a recorded answer
    # of a live one, or `fake` for a stand-in.
    model: Source
    # What the call cost by the client's accounting of it, when a live Ember
    # answered it. None for a stand-in's answer and for a recorded one, which cost
    # this call nothing and say nothing of what it costs.
    cost_nanousd: int | None = None

    @property
    def stand_in(self) -> bool:
        return self.model == "fake"

    def role_of(self, code: str) -> str | None:
        for entry in self.entries:
            if entry.code == code:
                return entry.role
        return None


def schema(codes: Sequence[str]) -> dict[str, Any]:
    """The form for a request about these codes: closed lists and nothing else."""
    return {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "enum": list(codes)},
                        "role": {"type": "string", "enum": [*ROLES, UNKNOWN]},
                    },
                    "required": ["code", "role"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["entries"],
        "additionalProperties": False,
    }


def learn_glossary(
    ember: EmberClient, patterns: Sequence[Pattern], *, limit_nanousd: int | None = None
) -> Glossary:
    """Have Ember propose a role for each code of `patterns`, and keep the entries
    that pass the check. `limit_nanousd` is the most the call may cost.

    Raises ValueError when the patterns are not ones to ask about: none, more than
    `MAX_CODES`, a code that is not letters, or a code twice. Raises EmberError as
    the client does; the call is not tried again here."""
    codes = tuple(pattern.code for pattern in patterns)
    if not 0 < len(codes) <= MAX_CODES:
        raise ValueError(f"A glossary is learned for 1 to {MAX_CODES} codes.")
    if len(set(codes)) != len(codes) or not all(_CODE.fullmatch(code) for code in codes):
        raise ValueError("A code is one to three capital letters, and is asked about once.")
    answer = ember.respond(
        task=TASK,
        effort="low",
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        # A table of counts and tags: no person, no place, and no call is in it.
        sensitive=False,
        limit_nanousd=limit_nanousd,
        instructions=INSTRUCTIONS,
        input=json.dumps({"codes": [pattern.as_data() for pattern in patterns]}),
        text={
            "format": {
                "type": "json_schema",
                "name": "glossary",
                "strict": True,
                "schema": schema(codes),
            }
        },
    )
    glossary = read_form(answer.output_text, codes, model=answer.source)
    cost = answer.usage.cost_nanousd if answer.source == "live" else None
    return replace(glossary, cost_nanousd=cost)


def read_form(text: str, codes: Sequence[str], *, model: Source) -> Glossary:
    """The glossary an answer holds, checked against the codes that were asked about."""
    asked = tuple(codes)
    try:
        form = json.loads(text)
    except ValueError:
        form = None
    listed = form.get("entries") if isinstance(form, dict) and set(form) == {"entries"} else None
    if not isinstance(listed, list):
        return Glossary(entries=(), asked=asked, left_out=0, whole=False, model=model)
    roles: dict[str, str] = {}
    left_out = 0
    for item in listed:
        code = item.get("code") if isinstance(item, dict) else None
        role = item.get("role") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"code", "role"}
            or code not in asked
            or code in roles
            or role not in (*ROLES, UNKNOWN)
        ):
            left_out += 1
            continue
        roles[code] = role
    entries = tuple(
        Entry(code, roles[code]) for code in asked if roles.get(code, UNKNOWN) != UNKNOWN
    )
    return Glossary(entries=entries, asked=asked, left_out=left_out, whole=True, model=model)
