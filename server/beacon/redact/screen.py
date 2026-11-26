"""The redaction pass: a text is shown only when every word of it is known.

Two kinds of text reach a person from a model, and each is read here first:

- **A reading** (`reading`): what Ember made of an authority's notice, such as its
  instruction. It may use the words an instruction is made of, and numbers freely,
  because an order names the addresses it is for.
- **Beacon's own voice** (`own`): what Ember wrote for Beacon about an incident. It
  may not use a word that tells a reader what to do, and may not put a number in
  front of a street, which would be a house and not a block.

Both are an allowlist. A text passes when it is made only of:

- the ordinary words in `beacon.redact.vocabulary`, written without a capital
  except at the start of a sentence, and a few that are always written with one;
- the names somebody listed (`Names`): streets, units, public bodies, and public
  places, each as a whole phrase, so `Hayes St` passes and `Hayes` does not;
- numbers, and the punctuation of plain prose.

Anything else, a name above all, makes the whole text fail. A text is kept whole or
left out, never cut: a cut instruction can say the opposite of the whole one.

What this cannot do, and does not claim to:

- **It does not understand a text.** It cannot tell that a text made of ordinary
  words is true, or that it leaves out the exception that matters.
- **A listed name passes whatever is meant by it**, as on the radio: a person
  called Hayes St would pass. The lists are an operator's.
- **A name that is also an ordinary word passes at the start of a sentence**, where
  every word has a capital. In the middle of one it does not.
- **A person can be described without being named.** The vocabulary leaves out the
  words for who somebody is and what happened to them, which is the reach of it.

So the pass is a filter in front of the rule and not the rule: what is shown as a
reading is still labeled as Beacon's reading, beside the link to the document.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

from beacon.redact.vocabulary import FIXED_PHRASES, ORDER_WORDS, PROPER, PUBLIC_NAMES, WORDS

# A text longer than this is not read, and does not pass.
MAX_TEXT = 4000
# In Beacon's own voice, a number this long is a telephone or an account, not a
# count, a time, or a block.
_MAX_OWN_DIGITS = 4

# What stands in for a listed street and for any other listed name while the rest
# of a text is read.
_STREET, _NAME = "\x01", "\x02"

_PUNCTUATION = frozenset(".,;:!?()'\"-/&%+\u2019\u2018\u201c\u201d\u2013\u2014")
_ENDS_SENTENCE = frozenset(".!?")
_TOKEN = re.compile(
    r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*"  # a word, with its apostrophes
    r"|\d[\dA-Za-z]*"  # a number, and whatever is written onto it
    r"|[\x01\x02]"
    r"|\S"
)
_NUMBER = re.compile(r"\d+(?:st|nd|rd|th|am|pm)?", re.IGNORECASE)
_NAME_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
_HOUSE = re.compile(r"\d\s*\x01")


@dataclass(frozen=True)
class Names:
    """The names a text may hold besides ordinary words. Build one with `Names.of`."""

    # Each name as its words, in lower case.
    streets: frozenset[tuple[str, ...]] = frozenset()
    others: frozenset[tuple[str, ...]] = frozenset()
    # The public bodies and places among `others`, which can be a notice's issuer.
    public: frozenset[tuple[str, ...]] = frozenset()

    @classmethod
    def of(
        cls,
        *,
        streets: Iterable[str] = (),
        units: Iterable[str] = (),
        places: Iterable[str] = (),
    ) -> "Names":
        """The lists an operator gives, with the public bodies Beacon knows itself
        (`PUBLIC_NAMES`). `places` are public places and bodies a notice may name:
        a plaza, a school that is a shelter, an agency. A name with no letter or
        digit in it is left out."""
        public = {_words(name) for name in (*PUBLIC_NAMES, *places)} - {()}
        fixed = {_words(name) for name in (*units, *FIXED_PHRASES)} - {()}
        return cls(
            streets=frozenset({_words(name) for name in streets} - {()}),
            others=frozenset(public | fixed),
            public=frozenset(public),
        )

    @cached_property
    def _pattern(self) -> re.Pattern[str] | None:
        """Finds every listed name in a text, the longest first, as whole words."""
        listed = sorted(self.streets | self.others, key=lambda name: (-len(name), name))
        if not listed:
            return None
        # Between the words of a name: spaces or a hyphen, and the full stop of `St.`.
        names = (r"\.?[\s\-]+".join(re.escape(word) for word in name) for name in listed)
        return re.compile(
            r"(?<![A-Za-z0-9'\u2019])(?:" + "|".join(names) + r")(?![A-Za-z0-9'\u2019])",
            re.IGNORECASE,
        )

    def masked(self, text: str) -> str:
        """The text with every listed name replaced by a mark that says whether it
        was a street."""
        if self._pattern is None:
            return text

        def mark(found: re.Match[str]) -> str:
            return _STREET if _words(found[0]) in self.streets else _NAME

        return self._pattern.sub(mark, text)

    def public_body(self, value: object) -> str | None:
        """The text when it is the name of a listed public body or place and nothing
        else, which is what a notice's issuer has to be to be shown. Else None."""
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            return None
        text = " ".join(value.split())
        return text if _words(text) in self.public else None


def reading(text: object, names: Names) -> str | None:
    """A text Ember made of an authority's notice, when it passes: its white space
    made single spaces, and nothing else changed. None when it does not pass, and
    for what is no text."""
    return _screen(text, names, WORDS | ORDER_WORDS, own=False)


def own(text: object, names: Names) -> str | None:
    """A text Ember wrote in Beacon's own voice, when it passes: as `reading`, and
    with no word that tells a reader what to do, no number in front of a street,
    and no long number. None when it does not pass."""
    return _screen(text, names, WORDS, own=True)


def _screen(text: object, names: Names, words: frozenset[str], *, own: bool) -> str | None:
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return None
    shown = " ".join(text.split())
    if not shown or "\x01" in shown or "\x02" in shown:
        return None
    masked = names.masked(shown)
    if own and _HOUSE.search(masked):
        return None
    starts_sentence = True
    for found in _TOKEN.finditer(masked):
        token = found[0]
        if token in (_STREET, _NAME):
            starts_sentence = False
        elif token[0].isdigit():
            if not _NUMBER.fullmatch(token):
                return None
            if own and sum(character.isdigit() for character in token) > _MAX_OWN_DIGITS:
                return None
            starts_sentence = False
        elif token[0].isascii() and token[0].isalpha():
            if not _word_passes(token, words, starts_sentence):
                return None
            starts_sentence = False
        elif token in _PUNCTUATION:
            if token in _ENDS_SENTENCE:
                starts_sentence = True
        else:
            return None
    return shown


def _word_passes(token: str, words: frozenset[str], starts_sentence: bool) -> bool:
    word = token.lower().replace("\u2019", "'")
    if word.endswith("'s"):
        word = word[:-2]
    if word in PROPER:
        return True
    if word not in words:
        return False
    if token.islower():
        return True
    # A capital on an ordinary word: at the start of a sentence, and only the first
    # letter. `May` in the middle of a sentence is somebody's name, and so is `MAY`.
    return starts_sentence and (len(token) == 1 or token[1:].islower())


def _words(name: str) -> tuple[str, ...]:
    return tuple(_NAME_WORD.findall(name.lower().replace("\u2019", "'")))


# With these, a text may name the public bodies Beacon knows and nothing else.
NO_NAMES = Names.of()
