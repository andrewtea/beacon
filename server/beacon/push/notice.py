"""The words about notifications, which are the server's.

A push carries no content, so the sentence a device shows is one the app was handed
when it subscribed (`notice_of`) and kept for its service worker. It says what a
push means and no more: that the investigator's decision turned crisis mode on for
an incident Beacon has near a place the device named. It is the investigator's
reading, and the sentence says so; it tells nobody what to do. On the mock store it
says first that the incident is invented and that a stand-in decided.
"""

from dataclasses import dataclass

from beacon.files import Mode
from beacon.push.store import KEPT, PERMIT_LIFE, SENT_KEPT


@dataclass(frozen=True)
class Notice:
    """What a device shows when a push arrives."""

    title: str
    text: str


_LIVE = Notice(
    title="Beacon: crisis mode near a saved place",
    text=(
        "Beacon's investigator turned crisis mode on for an incident that Beacon has "
        "within about a kilometre of a place you get notifications for. That is the "
        "investigator's reading and no authority's, and the distance is rough. Open "
        "Beacon to read what each source said."
    ),
)
_MOCK = Notice(
    title="Beacon: invented scenario",
    text=(
        "Invented scenario, not a real event. A stand-in for Beacon's investigator "
        "turned crisis mode on for an invented incident that Beacon has within about a "
        "kilometre of a place you get notifications for. Open Beacon to read it."
    ),
)

# Everything Beacon keeps to send a notification and for how long, when it sends
# one, and what the push service is and is not sent. Shown before a person turns
# notifications on, so it has to name every column of `store`'s tables. Its
# paragraphs are parted by a blank line, which the app shows as paragraphs.
ABOUT = (
    "To notify this device, Beacon's server keeps the address your browser's push "
    "service made for it; where the places you have saved are, each rounded to "
    "about a block and without its name, with when this server was first told of "
    "it; and when this device last renewed all that, which it does when you open "
    "Beacon with notifications on and when you save or remove a place. For each "
    "incident it tries to notify this device about, it also keeps, beside that "
    "address, the incident's id, when it last tried, how many times it tried, and "
    f"whether the push service took the notification, for {SENT_KEPT.days} days at "
    "most. While this device is giving it that address, it also keeps, for "
    f"{int(PERMIT_LIFE.total_seconds()) // 60} minutes at most, a random number it "
    "handed this device, which is tied to no address and no place. It keeps no name, no "
    "browser, and no address a request came from.\n\n"
    "It sends a notification when its investigator turns crisis mode on for an "
    "incident it has within about a kilometre of one of those places, if this "
    "server had been told of the place by then: once for an incident as a rule, "
    "though a notification can fail to arrive, and one can arrive twice.\n\n"
    "A notification carries no words: your device shows a sentence it already "
    "has. The push service, which is your browser maker's, is sent no incident, no "
    "place, and no text; it does learn that Beacon sent this device a message, and "
    "when.\n\n"
    "Beacon's server deletes all of this when it receives the request your device "
    "sends when you turn notifications off, which your device sends again until "
    "the server answers; when the push service says the address is no longer "
    f"good; and {KEPT.days} days after this device last renewed it."
)
OFF = "This server sends no notifications."


def notice_of(mode: Mode) -> Notice:
    return _MOCK if mode == "mock" else _LIVE
