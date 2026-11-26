"""The closed vocabulary a radio transmission is stored in.

Dispatch radio names people, describes patients, and gives house numbers, and none of
that may be stored. So nothing a transmission said is stored as free text. What is
kept of it is a category, a status, and detail tags from the fixed lists here, unit
ids and street names that fit a strict pattern, and a block number rounded down to
the hundred. Whatever does not fit is dropped by `beacon.radio.extract` before a
`Transmission` exists, whatever Ember answered.

The limit of this: a street is checked by its shape, not against a list of the
city's streets, so words shaped like a street name would be kept as one. A name has
to end in a street type, so `Jane Doe` is not kept and `Jane Doe St` would be.
"""

import re
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
