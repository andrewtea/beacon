"""The redaction pass: what a model read in a notice is checked before a person is
shown it.

    from beacon.redact import Names, reading

    names = Names.of(streets=known.streets, units=known.units, places=["Civic Center Plaza"])
    reading(notice_instruction, names)  # the text, or None when it does not pass
    names.public_body(notice_issuer)  # the issuer, or None when it is not a listed body

See `screen` for what passes and what the pass cannot do, and `vocabulary` for the
words.
"""

from beacon.redact.screen import MAX_TEXT, NO_NAMES, Names, reading
from beacon.redact.vocabulary import ACRONYMS, DAYS, MONTHS, PUBLIC_NAMES, WORDS

__all__ = [
    "ACRONYMS",
    "DAYS",
    "MAX_TEXT",
    "MONTHS",
    "NO_NAMES",
    "PUBLIC_NAMES",
    "WORDS",
    "Names",
    "reading",
]
