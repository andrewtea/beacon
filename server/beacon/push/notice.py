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

# What Beacon keeps to send a notification, when it sends one, and who learns what.
# Shown before a person turns notifications on.
ABOUT = (
    "To notify this device, Beacon keeps the address your browser's push service "
    "made for it and the places you have saved, each rounded to about a block and "
    "without its name. It keeps nothing else about you or this device. It sends one "
    "notification for an incident, when its investigator turns crisis mode on for "
    "one it has within about a kilometre of one of those places. A notification "
    "carries no words: your device shows a sentence it already has, so the push "
    "service, which is your browser maker's, learns only that Beacon sent this "
    "device a message and when. Beacon deletes the address and the places when you "
    "turn notifications off, when the push service says the address is no longer "
    "good, and 30 days after you last opened Beacon with them on."
)
OFF = "This server sends no notifications."


def notice_of(mode: Mode) -> Notice:
    return _MOCK if mode == "mock" else _LIVE
