"""The redaction pass: what a model wrote is read before a person is shown it.

    from beacon.redact import Names, own, reading

    names = Names.of(streets=known.streets, units=known.units, places=["Civic Center Plaza"])
    reading(notice_instruction, names)  # the text, or None when it does not pass
    own(story_sentence, names)  # as strict, and with no word that tells a reader what to do

See `screen` for what passes and what the pass cannot do, and `vocabulary` for the
words.
"""

from beacon.redact.screen import MAX_TEXT, NO_NAMES, Names, own, reading
from beacon.redact.vocabulary import ORDER_WORDS, PROPER, PUBLIC_NAMES, WORDS

__all__ = [
    "MAX_TEXT",
    "NO_NAMES",
    "ORDER_WORDS",
    "PROPER",
    "PUBLIC_NAMES",
    "WORDS",
    "Names",
    "own",
    "reading",
]
