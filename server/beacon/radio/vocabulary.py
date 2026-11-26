"""The closed vocabulary a radio transmission is stored in.

Dispatch radio names people, describes patients, and gives house numbers, and none of
that may be stored. So nothing a transmission said is stored as free text. What is
kept of it is a category, a status, and detail tags from the fixed lists here, a
block number rounded down to the hundred, and unit ids and street names that are
**on a list** (`Known`). Whatever does not fit is dropped by `beacon.radio.extract`
before a `Transmission` exists, whatever Ember answered.

A unit or a street is checked twice. First by its shape, which also gives it its one
stored form: `truck 6` is `T6`, and `432 hayes street` is `Hayes St`. Shape is not
enough, though, because a model's answer is shaped by the model: `BP 180` has the
shape of a unit, and `One Two Three Hayes St` the shape of a street. So the name is
then looked up in `Known`, the units and the streets somebody listed, and one that
is not listed is dropped. With no list, no unit and no street is kept at all.

What is left after that: a listed name is kept whatever Ember meant by it. `M72` on
the list of units is kept as Medic 72 even if what was said was a man of 72, and the
hundred block is a number Ember gave. Nothing here can tell.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, get_args

Category = Literal[
    "fire",
    "medical",
    "rescue",
    "hazmat",
    "collision",
    "alarm",
    "utility",
    "service",
    "administrative",
    "other",
    "unintelligible",
]
Status = Literal[
    "dispatched",
    "en_route",
    "on_scene",
    "working",
    "under_control",
    "cleared",
    "cancelled",
    "unknown",
]
Detail = Literal[
    "smoke_showing",
    "fire_showing",
    "working_fire",
    "additional_alarm",
    "exposure_threatened",
    "rescue_in_progress",
    "evacuation",
    "shelter_in_place",
    "police_requested",
    "utilities_requested",
    "road_closed",
    "aircraft_overhead",
    "knocked_down",
    "overhaul",
    "all_clear",
]

CATEGORIES: tuple[str, ...] = get_args(Category)
STATUSES: tuple[str, ...] = get_args(Status)
DETAILS: tuple[str, ...] = get_args(Detail)

# What a medical call is called wherever it is stored or shown, and all it is called.
MEDICAL_EVENT = "medical response"

MAX_ALARM = 5
MAX_UNITS = 30

# A unit is a type code and a number: E36, T6, B2, RS1, M52.
_UNIT = re.compile(r"([A-Z]{1,3})0*([1-9]\d{0,2})")
# The type codes of the units a dispatcher names in full.
_UNIT_WORDS = {"ENGINE": "E", "TRUCK": "T", "BATTALION": "B", "DIVISION": "D", "MEDIC": "M"}

_SUFFIXES = {
    "street": "St",
    "avenue": "Ave",
    "boulevard": "Blvd",
    "drive": "Dr",
    "way": "Way",
    "court": "Ct",
    "place": "Pl",
    "terrace": "Ter",
    "lane": "Ln",
    "road": "Rd",
    "highway": "Hwy",
    "parkway": "Pkwy",
    "alley": "Aly",
    "plaza": "Plz",
    "circle": "Cir",
    "square": "Sq",
    "row": "Row",
    "walk": "Walk",
    "loop": "Loop",
}
_SUFFIX_OF = _SUFFIXES | {short.lower(): short for short in _SUFFIXES.values()}
# The streets that have no street type.
_BARE_STREETS = {
    name.lower(): name
    for name in ("Broadway", "The Embarcadero", "Embarcadero", "Great Highway", "La Playa")
}
_HIGHWAY = re.compile(r"(?:I|US|CA)-\d{1,3}")
_NUMBERED = re.compile(r"\d{1,2}(?:st|nd|rd|th)")
_NAME = re.compile(r"[a-z]+(?:'[a-z]+)?")
# Words that join two places or point into a building. A street's name has none of
# them, so text that has one is a description, and is not kept as a name.
_NOT_IN_A_NAME = frozenset(
    {
        "at", "and", "near", "between", "by", "off", "from", "to", "of", "on", "in", "x",
        "cross", "corner", "block", "apt", "apartment", "unit", "suite", "floor", "room",
        "number", "no",
    }
)  # fmt: skip
# A number in front of the name is a house number, whoever put it there.
_LEADING_NUMBER = re.compile(r"\A\s*\d+\s+(?=\S)")
_MAX_STREET = 40
_MAX_NAME_WORDS = 5


def clean_unit(value: object) -> str | None:
    """A unit id in its one stored form, without leading zeros: `T06`, `t6`, and
    `Truck 6` are `T6`. None for anything that is not a type code and a number from
    1 to 999."""
    if not isinstance(value, str) or len(value) > 20:
        return None
    text = value.strip().upper().replace("-", " ")
    words = text.split()
    if len(words) == 2 and words[0] in _UNIT_WORDS:
        text = _UNIT_WORDS[words[0]] + words[1]
    match = _UNIT.fullmatch(text.replace(" ", ""))
    return None if match is None else f"{match[1]}{match[2]}"


def clean_street(value: object) -> str | None:
    """A street name in its one stored form: `hayes street` and `432 Hayes St.` are
    `Hayes St`, and `O'FARRELL STREET` is `O'Farrell St`. A number in front of it is
    dropped and never kept. None for anything that is not shaped like the name of one
    street: `Hayes at Gough`, `Hayes St Apt 4`, and a name with no street type are
    not."""
    if not isinstance(value, str) or len(value) > 4 * _MAX_STREET:
        return None
    text = _LEADING_NUMBER.sub("", value, count=1).replace(".", " ").strip()
    words = text.lower().split()
    if not words:
        return None
    if " ".join(words) in _BARE_STREETS:
        return _BARE_STREETS[" ".join(words)]
    if len(words) == 1 and _HIGHWAY.fullmatch(text.upper()):
        return text.upper()
    *named, last = words
    suffix = _SUFFIX_OF.get(last)
    if suffix is None or not 1 <= len(named) <= _MAX_NAME_WORDS:
        return None
    for place, word in enumerate(named):
        # `St` or `Dr` in front of a name is a title: St Josephs Ave, Dr Tom Waddell Pl.
        title = place == 0 and word in ("st", "dr") and len(named) > 1
        if word in _NOT_IN_A_NAME or (word in _SUFFIX_OF and not title):
            return None
        if not (_NUMBERED.fullmatch(word) or _NAME.fullmatch(word)):
            return None
    street = " ".join([*(_name_word(word) for word in named), suffix])
    return street if len(street) <= _MAX_STREET else None


@dataclass(frozen=True)
class Known:
    """The units and the streets a transmission may name: a department's roster and
    a city's street names, each in its stored form. Build one with `Known.of`."""

    streets: frozenset[str] = frozenset()
    units: frozenset[str] = frozenset()

    @classmethod
    def of(cls, *, streets: Iterable[str] = (), units: Iterable[str] = ()) -> "Known":
        """The lists, each name put in its stored form.

        Raises ValueError when a name is not one `clean_street` or `clean_unit`
        keeps, and says which place in its list it has, not what it is."""
        return cls(
            streets=frozenset(_listed(streets, clean_street, "street")),
            units=frozenset(_listed(units, clean_unit, "unit")),
        )

    @classmethod
    def kept(cls, *, streets: Iterable[str] = (), units: Iterable[str] = ()) -> "tuple[Known, int]":
        """The lists as `of` makes them, with the names it would refuse left out,
        and how many those were. A city's own list of its streets has entries that
        are no street name Beacon keeps, such as `Avenue of the Palms`."""
        stored_streets = [clean_street(name) for name in streets]
        stored_units = [clean_unit(name) for name in units]
        known = cls(
            streets=frozenset(name for name in stored_streets if name is not None),
            units=frozenset(name for name in stored_units if name is not None),
        )
        left_out = stored_streets.count(None) + stored_units.count(None)
        return known, left_out

    def street(self, value: object) -> str | None:
        """A listed street in its stored form, or None."""
        name = clean_street(value)
        return name if name in self.streets else None

    def unit(self, value: object) -> str | None:
        """A listed unit in its stored form, or None."""
        name = clean_unit(value)
        return name if name in self.units else None


# With this, a transmission keeps no unit and no street.
NOTHING_KNOWN = Known()


def read_names(text: str) -> list[str]:
    """The names in a list file: one to a line, with blank lines and lines that
    begin with `#` left out."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _listed(names: Iterable[str], clean, what: str) -> list[str]:  # type: ignore[no-untyped-def]
    kept = []
    for place, name in enumerate(names, start=1):
        stored = clean(name)
        if stored is None:
            raise ValueError(f"Name {place} in the list of {what}s is not a {what} Beacon keeps.")
        kept.append(stored)
    return kept


def _name_word(word: str) -> str:
    """A word of a street's name with its capitals: `19th`, `Hayes`, `O'Farrell`,
    `McAllister`."""
    if _NUMBERED.fullmatch(word):
        return word
    if "'" in word:
        return "'".join(part.capitalize() for part in word.split("'"))
    if word.startswith("mc") and len(word) > 3:
        return "Mc" + word[2:].capitalize()
    return word.capitalize()


def block_of(number: object) -> int | None:
    """The hundred block a house number is on: 1458 is the 1400 block. The number
    itself is not kept anywhere. None for anything that is not a house number."""
    if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < 100_000:
        return None
    return number // 100 * 100


def place_in_words(street: str | None, block: int | None, cross_street: str | None) -> str | None:
    """A block-level place: `400 block of Hayes St`, or `Hayes St at Gough St`."""
    if street is None:
        return None
    if block is not None:
        return f"{block} block of {street}"
    if cross_street is not None:
        return f"{street} at {cross_street}"
    return street
