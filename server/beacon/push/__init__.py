"""Web push: telling a device that crisis mode was turned on near a place it named.

    from beacon.push import open_push

    push = open_push(os.environ, engine, runs)  # None with BEACON_PUSH=0
    push.subscriptions.put(endpoint, points, now)  # what POST /api/push/subscribe does
    push.notifier.notify(engine.snapshot(), now)   # one pass; the server makes one every 15 s

This is the first thing Beacon keeps that is tied to a device, so it keeps as little
as sends a notification, and says what (`ABOUT`):

- **A subscription is an address and places, and nothing else.** The address is the
  one the browser's push service made for this device and this site (its
  `endpoint`). The places are up to five, each rounded to about a block as a tap's
  is, with no name. No key of the browser's is kept, because nothing is encrypted
  to it: **a push carries no content**. It wakes the app's service worker, which
  shows a sentence the app already has. So the push service, which is the browser
  maker's, learns that Beacon sent this device a message and when, and nothing of
  what it was about.
- **A subscription is kept for `KEPT` after it was last renewed**, which the app
  does whenever it is opened with notifications on, and deleted when the person
  turns notifications off, when the push service says the address is gone, and
  when its time is over.
- **One push for each incident, at most.** A device is sent a push when the
  investigator's decision turned crisis mode on for an open incident that Beacon
  has a point for within `REACH_M` of one of its places, and the incident still has
  one of the signals the decision cited. Never for a medical response. A send is
  claimed before it is made (`Subscriptions.claim`) and tried again only when the
  push service itself answered that it did not take it.
- **A push is sent only to a push service** (`endpoint.checked`): the address a
  stranger sends is one Beacon will make a request to, so it has to be `https` on a
  host of a browser maker's push service, written in a way that can be read one way
  only. The request follows no redirect, and nothing about it is logged but counts.
- **The words of a notification are the server's** (`notice.notice_of`), handed to
  the app when it subscribes, and they say what kind of server sent it: on the mock
  store, that the incident is invented and a stand-in decided. They tell nobody
  what to do.

`vapid` is the key a push is signed with, `endpoint` the check of an address,
`store` the file subscriptions are kept in, `desk` the pass that sends, `notice` the
words, and `setup` opens it all (`open_push`).
"""

from beacon.push.desk import Notifier, Pass
from beacon.push.endpoint import MAX_ENDPOINT, audience, checked
from beacon.push.notice import ABOUT, OFF, Notice, notice_of
from beacon.push.setup import PUSH_VARIABLE, Push, PushConfigError, open_push
from beacon.push.store import (
    KEPT,
    MAX_SUBSCRIPTIONS,
    Subscription,
    Subscriptions,
    SubscriptionsError,
)
from beacon.push.vapid import Vapid, VapidError

__all__ = [
    "ABOUT",
    "KEPT",
    "MAX_ENDPOINT",
    "MAX_SUBSCRIPTIONS",
    "OFF",
    "PUSH_VARIABLE",
    "Notice",
    "Notifier",
    "Pass",
    "Push",
    "PushConfigError",
    "Subscription",
    "Subscriptions",
    "SubscriptionsError",
    "Vapid",
    "VapidError",
    "audience",
    "checked",
    "notice_of",
    "open_push",
]
