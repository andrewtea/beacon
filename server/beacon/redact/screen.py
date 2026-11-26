"""The redaction pass: a text is shown only when every word of it is known.

One kind of text reaches a person from a model: **a reading**, which is what Ember
made of an authority's notice, such as its instruction. `reading` reads it first.
Nothing a model writes in Beacon's own voice is shown at all: where Ember has a part
in what Beacon says about an incident, it chooses among lines and code writes the
words (`beacon.story`). A list of words cannot keep a sentence from telling a reader
what to do, so the pass is not asked to.

The pass is an allowlist. A text passes when it is made only of:

- the ordinary words in `beacon.redact.vocabulary`, written without a capital
  except at the start of a sentence;
- acronyms of public bodies, in capitals; a month beside a number, and a day in
  front of such a date;
- the names somebody listed (`Names`): streets, units, public bodies, and public
  places, each as a whole phrase, so `Hayes St` passes and `Hayes` does not;
- numbers, and the punctuation of plain prose. A reading may hold any number: an
  order names the addresses it is for.

Anything else, a name above all, makes the whole text fail. A text is kept whole or
left out, never cut: a cut instruction can say the opposite of the whole one.

What this cannot do, and does not claim to:

- **It does not understand a text.** It cannot tell that a text made of ordinary
  words is true, or that it leaves out the exception that matters.
- **A listed name passes whatever is meant by it**, as on the radio: a person
  called Hayes St would pass. The lists are an operator's.
- **A name that is also an ordinary word passes at the start of a sentence**, where
  every word has a capital, and a month that is somebody's name passes beside a
  number. In the middle of a sentence, and with no number, neither does.
- **A person can be described without being named.** The vocabulary leaves out the
  words for who somebody is and what happened to them, which is the reach of it.

So the pass is a filter in front of the rule and not the rule: what is shown is
still labeled as Beacon's reading, beside the link to the document.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property

from beacon.redact.vocabulary import ACRONYMS, DAYS, MONTHS, OTHER_NAMES, PUBLIC_NAMES, WORDS

# A text longer than this is not read, and does not pass.
MAX_TEXT = 4000

# What stands in for a listed street and for any other listed name while the rest
# of a text is read.
_STREET, _NAME = "\x01", "\x02"

_PUNCTUATION = frozenset(".,;:!?()'\"-/&%+\u2019\u2018\u201c\u201d\u2013\u2014")
_ENDS_SENTENCE = frozenset(".!?")
_DIGITS = frozenset("0123456789")
_TOKEN = re.compile(
    r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*"  # a word, with its apostrophes
    r"|\d[\dA-Za-z]*"  # a number, and whatever is written onto it
    r"|[\x01\x02]"
    r"|\S"
)
_NUMBER = re.compile(r"[0-9]+(?:st|nd|rd|th|am|pm)?", re.IGNORECASE)
_NAME_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
_WORD = r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)*"
# A name written plainly: its words, with a space, a hyphen, or the full stop of an
# abbreviation between them, and not one character besides.
_PLAIN_NAME = re.compile(rf"{_WORD}(?:\.?[ \-]{_WORD})*\.?")


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
        a plaza, a school that is a shelter, an agency. A name that is not written
        plainly is left out whole (`kept` says how many were)."""
        return cls.kept(streets=streets, units=units, places=places)[0]

    @classmethod
    def kept(
        cls,
        *,
        streets: Iterable[str] = (),
        units: Iterable[str] = (),
        places: Iterable[str] = (),
    ) -> "tuple[Names, int]":
        """The lists as `of` makes them, and how many names were left out.

        A name is listed whole or not at all. One that holds anything besides
        plain words (another script, a bracket, an `&`) is left out: listing the
        words of it that can be read would list a name nobody gave, and `李 Lee 公園`
        would make `Lee` a name every text may hold."""
        left_out = 0

        def listed(names: Iterable[str]) -> set[tuple[str, ...]]:
            nonlocal left_out
            found: set[tuple[str, ...]] = set()
            for name in names:
                plain = _plain(name)
                if plain is None:
                    left_out += 1
                else:
                    found.add(_words(plain))
            return found

        public = listed((*PUBLIC_NAMES, *places))
        rest = listed((*units, *OTHER_NAMES))
        names = cls(
            streets=frozenset(listed(streets)),
            others=frozenset(public | rest),
            public=frozenset(public),
        )
        return names, left_out

    @cached_property
    def _pattern(self) -> re.Pattern[str] | None:
        """Finds every listed name in a text, the longest first, as whole words,
        with the `'s` of a possessive when it has one."""
        listed = sorted(self.streets | self.others, key=lambda name: (-len(name), name))
        if not listed:
            return None
        # Between the words of a name: spaces or a hyphen, and the full stop of `St.`.
        names = (r"\.?[\s\-]+".join(re.escape(word) for word in name) for name in listed)
        return re.compile(
            r"(?<![A-Za-z0-9'\u2019])(?P<name>"
            + "|".join(names)
            + r")(?:['\u2019]s)?(?![A-Za-z0-9'\u2019])",
            re.IGNORECASE,
        )

    def masked(self, text: str) -> str:
        """The text with every listed name replaced by a mark that says whether it
        was a street."""
        if self._pattern is None:
            return text

        def mark(found: re.Match[str]) -> str:
            return _STREET if _words(found["name"]) in self.streets else _NAME

        return self._pattern.sub(mark, text)

    def public_body(self, value: object) -> str | None:
        """The text when it is the name of a listed public body or place and not
        one character more, which is what a notice's issuer has to be to be shown.
        Else None: an issuer with anything beside the name is left out whole."""
        text = _plain(value)
        if text is None:
            return None
        return text if _words(text) in self.public else None


def reading(text: object, names: Names) -> str | None:
    """A text Ember made of an authority's notice, when it passes: its white space
    made single spaces, and nothing else changed. None when it does not pass, and
    for what is no text."""
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return None
    shown = " ".join(text.split())
    if not shown or _STREET in shown or _NAME in shown:
        return None
    tokens = _TOKEN.findall(names.masked(shown))
    starts_sentence = True
    for place, token in enumerate(tokens):
        if token in (_STREET, _NAME):
            starts_sentence = False
        elif token[0] in _DIGITS:
            if not _NUMBER.fullmatch(token):
                return None
            starts_sentence = False
        elif token[0].isascii() and token[0].isalpha():
            if not (_in_a_date(tokens, place) or _word_passes(token, starts_sentence)):
                return None
            starts_sentence = False
        elif token in _PUNCTUATION:
            if token in _ENDS_SENTENCE:
                starts_sentence = True
        else:
            return None
    return shown


def _word_passes(token: str, starts_sentence: bool) -> bool:
    plain = token.replace("\u2019", "'")
    base = plain[:-2] if plain.lower().endswith("'s") else plain
    if base in ACRONYMS:
        return True
    if base.lower() not in WORDS:
        return False
    if token.islower():
        return True
    # A capital on an ordinary word: at the start of a sentence, and only the first
    # letter. `May` in the middle of a sentence is somebody's name, and so is `MAY`.
    return starts_sentence and (len(token) == 1 or token[1:].islower())


def _in_a_date(tokens: Sequence[str], place: int) -> bool:
    """Whether the word at `place` is a month or a day written as part of a date:
    a month with a number beside it, or a day in front of such a month, with a
    comma between them or not. `April 3` is a date. `April` alone is a name."""
    if _is_month(tokens, place):
        return True
    token = tokens[place]
    if token != token.lower().capitalize() or token.lower() not in DAYS:
        return False
    after = place + 2 if tokens[place + 1 : place + 2] == [","] else place + 1
    return _is_month(tokens, after)


def _is_month(tokens: Sequence[str], place: int) -> bool:
    if not 0 <= place < len(tokens):
        return False
    token = tokens[place]
    if token != token.lower().capitalize() or token.lower() not in MONTHS:
        return False
    return any(
        0 <= beside < len(tokens) and _NUMBER.fullmatch(tokens[beside]) is not None
        for beside in (place - 1, place + 1)
    )


def _plain(value: object) -> str | None:
    """A name with its white space made single spaces, when it is written plainly
    from its first character to its last. Else None."""
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        return None
    text = " ".join(value.split())
    return text if _PLAIN_NAME.fullmatch(text) is not None else None


def _words(name: str) -> tuple[str, ...]:
    return tuple(_NAME_WORD.findall(name.lower().replace("\u2019", "'")))


# With these, a text may name the public bodies Beacon knows and nothing else.
NO_NAMES = Names.of()
