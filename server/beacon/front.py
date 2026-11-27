"""What a server takes from a visitor, by what stands in front of it.

A deployment is reached through somebody else's host, which ends TLS and passes
requests on. Beacon keeps the private routes out of its own log, and the rule for
anything in front of it is the same (README, "The app"): the address of a tap's
routes holds the tap's id, which is what its sender reads the answer with. Whether
a host keeps to that is for its operator to find out, and until somebody has,
`BEACON_FRONT_END=unverified` says so, and the server then **takes nothing of a
visitor's own**:

- a tap only from one of the invented scenario's two places (`SPOTS`), to the
  digit as it was sent, with no accuracy, no clip, and no photo. The row of a tap
  it takes says so, and it answers only for a tap whose row does: what a row holds
  cannot show it, since a place a block from one of the two is kept as that place;
- a check of saved places only when every place is one of those two as it was sent;
- no notification address, and so it reads no contact. It still deletes an address
  when its device asks and when its time is over, as any server does.

So what such a host could write down of a request is an id of a tap that holds an
invented place. That is the invented scenario's server only: one that names a real
source and is `unverified` does not start.

`verified` is the operator saying they have seen that the host keeps those routes
out of its records, and nothing else: to take a visitor's own through a host nobody
has checked would be a change to the rule, not a use of this setting. With the
variable not set at all, as on a developer's machine, a server takes a visitor's
own; set to nothing, it is refused, so that a deployment's default cannot be
emptied by accident.
"""

from collections.abc import Mapping
from typing import Literal

from beacon.evidence import Point

VARIABLE = "BEACON_FRONT_END"

# What a server takes: a visitor's `own` place, clip, photo, saved places, and
# notification address, or only the `invented` scenario's two places.
Input = Literal["own", "invented"]

# The invented scenario's two places, as the app offers them (`MOCK_SPOTS` in
# `web/src/lib/geo.ts`, which a test holds equal): near the invented fire, and
# across town from it.
SPOTS = (Point(37.776, -122.424), Point(37.8, -122.41))

# What a visitor reads before they send anything, and the rest of it, which the
# app shows when it is asked for.
INVENTED = (
    "On this server a tap is sent from one of two invented places, not from where "
    "you are, and no sound, photo, place of your own, or notification address is "
    "taken: nobody has checked what the hosting service this server is reached "
    "through writes down of a request."
)
INVENTED_MORE = (
    "The hosting service's records are not Beacon's to read or set. As with any "
    "website, it may keep the address of each request, when it came, and where "
    "from. The address a tap's answer is read at holds the tap's id, and whoever "
    "has that id can read the answer for as long as this server keeps it; on this "
    "server that answer is about one of the two invented places. Beacon's own "
    "server writes no log of those requests. A Beacon server whose operator has "
    "checked what stands in front of it takes a tap from where you are."
)

# The refusals, which repeat nothing that was sent.
TAP_REFUSED = (
    "This server takes a tap only from one of the invented scenario's two places, "
    "with no accuracy, sound, or photo."
)
PLACES_REFUSED = "This server is asked only about the invented scenario's two places."
# What a person is told of notifications, and what the operator's log says.
PUSH_OFF = (
    "This server takes no notification address: nobody has checked what the hosting "
    "service it is reached through writes down of a request."
)
PUSH_OFF_REASON = f"{VARIABLE}=unverified"
NOT_WITH_A_REAL_SOURCE = (
    f"{VARIABLE}=unverified is for the invented scenario: such a server takes only "
    "its two invented places, which a server on real sources has no use for. Find "
    "out what the host in front of this server writes down of a request (README, "
    f'"Deploy"), and set {VARIABLE}=verified.'
)


class FrontError(Exception):
    """`BEACON_FRONT_END` is neither word. The text is fixed."""


def input_of(env: Mapping[str, str]) -> Input:
    """What this server takes from a visitor.

    Raises FrontError for a setting that is neither `verified` nor `unverified`,
    the empty one included."""
    setting = env.get(VARIABLE)
    if setting is None or setting == "verified":
        return "own"
    if setting == "unverified":
        return "invented"
    raise FrontError(f"{VARIABLE} is verified or unverified.")
