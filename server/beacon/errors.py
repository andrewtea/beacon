"""Raising an error that shows nothing but its own fixed text.

Beacon reads things it must not repeat: an address that holds an API key, a source's
reply, radio traffic, what a person sent with a tap. An error raised about any of
them has a fixed text, and that is not enough, because Python chains errors: the one
being handled when another is raised is kept under it as its context, and a traceback
prints both. `raise_bare` raises an error with nothing under it.
"""

from typing import NoReturn


def raise_bare(error: Exception) -> NoReturn:
    """Raise an error with nothing under it. Call it outside any `except` block, so
    the error handled there, which can hold a reply or an address, is not kept as
    this one's context. Python would still put an error the *caller* is handling
    under it, and that one is not this error's to show either, so it is removed."""
    try:
        raise error from None
    finally:
        error.__context__ = None
