"""Which addresses Beacon sends a push to.

A subscription's address comes from whoever posts it, and Beacon then makes a
request to it. So it is taken only when it is plainly an address of a browser
maker's push service: `https`, a listed host, the usual port, and written with
nothing in it that one program reads as one host and another as a second (no name
and password before the host, no backslash, no space, nothing outside ASCII).
"""

import re

# The longest address that is kept. Those of the push services are a few hundred
# characters.
MAX_ENDPOINT = 1024

# The push services of the browsers the app runs in: Chrome and the browsers built
# on it, Firefox, Safari, and Edge.
_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
    }
)
# Edge's are many hosts under one name, and Apple asks that any host under its
# name be taken (`web.push.apple.com` is the one it uses now).
_HOSTS_UNDER = (".notify.windows.com", ".push.apple.com")

# The scheme, a host in lower case, the usual port or none, and a path of
# characters a URL's path and query may hold as they are. No `@`, no `#`, no `\`.
_SHAPE = re.compile(r"https://([a-z0-9.-]{1,253})(?::443)?(/[A-Za-z0-9._~:/?!$&'()*+,;=%-]*)")


def checked(sent: object) -> str | None:
    """The address as it was sent, when it is one a push may be sent to, else None."""
    if not isinstance(sent, str) or len(sent) > MAX_ENDPOINT:
        return None
    shaped = _SHAPE.fullmatch(sent)
    if shaped is None:
        return None
    host = shaped.group(1)
    if host not in _HOSTS and not any(
        host.endswith(under) and _label(host[: -len(under)]) for under in _HOSTS_UNDER
    ):
        return None
    return sent


def audience(endpoint: str) -> str:
    """Whom a push to a checked address is signed for: the push service's origin."""
    shaped = _SHAPE.fullmatch(endpoint)
    if shaped is None:
        raise ValueError("Not an address a push is sent to.")
    return f"https://{shaped.group(1)}"


def _label(name: str) -> bool:
    """Whether this is host labels that can stand before a listed name: not empty,
    and no label empty or beginning or ending with a hyphen."""
    return all(
        label and not label.startswith("-") and not label.endswith("-") for label in name.split(".")
    )
