"""Web push: telling a device that crisis mode was turned on near a place it named.

    from beacon.push import open_push

    push = open_push(os.environ, engine, runs)  # always with this mode's file open
    push.sends  # False with BEACON_PUSH=0, and with no BEACON_VAPID_SUBJECT
    permit = push.subscriptions.permit(now)  # what POST /api/push/permit does
    push.subscriptions.put(endpoint, points, now, permit=permit)  # POST /api/push/subscribe
    push.work(engine.snapshot, now)  # one pass; the server makes one every 15 s

This is the first thing Beacon keeps that is tied to a device, so it keeps as little
as sends a notification, and says all of it (`ABOUT`):

- **A subscription is an address and places.** The address is the one the
  browser's push service made for this device and this site (its `endpoint`). The
  places are up to five, each rounded to about a block as a tap's is, with no name
  and with when this server was first told of it. Beside them: when the
  subscription was last renewed, and for each incident a push was tried for, the
  incident's id, when, how many tries, and what came of the latest (`store`). No
  key of the browser's is kept, because nothing is encrypted to it: **a push
  carries no content**. It wakes a service worker of the app's, which shows a
  sentence the app already has. So the
  push service, which is the browser maker's, is sent no incident, no place, and
  no text. It does learn that Beacon sent this device a message, and when.
- **A subscription is kept for `KEPT` after it was last renewed**, which the app
  does when it is opened with notifications on, and deleted when the person turns
  notifications off, when the push service says the address is gone, and when its
  time is over. **That goes on whether or not this server sends anything**
  (`Push.work`), and for the other mode's file too.
- **A push for an incident is best effort: once as a rule, and neither certainly
  nor only once.** A device is sent a push when the investigator's decision turned
  crisis mode on for an open incident whose block Beacon has a point for, within
  `REACH_M` of a place the device had named by then, and the incident still has
  one of the signals the decision cited. Never for a medical response, and never
  from a point Beacon took from a 511 event, which a notification could not
  credit. A send is claimed before it is made (`Subscriptions.claim`) and tried
  again when the push service did not say it took it, or the server was stopped
  before it heard.
- **A push is sent only to a push service** (`endpoint.checked`): the address a
  stranger sends is one Beacon will make a request to, so it has to be `https` on a
  host of a browser maker's push service, written in a way that can be read one way
  only. That checks where a push goes, not that the push service ever made the
  address. The request follows no redirect, and nothing about it is logged but
  counts.
- **The words of a notification are the server's** (`notice.notice_of`), handed to
  the app before it subscribes, and they say what kind of server sent it: on the
  mock store, that the incident is invented and a stand-in decided. They tell
  nobody what to do.
- **A push names whom a push service may write to** (`BEACON_VAPID_SUBJECT`), and
  a server that was given nobody to name sends none (`vapid`).

`vapid` is the key a push is signed with, `endpoint` the check of an address,
`store` the file subscriptions are kept in, `desk` the pass that sends, `notice` the
words, and `setup` opens it all (`open_push`).
"""

from beacon.push.desk import Notifier, Pass
from beacon.push.endpoint import MAX_ENDPOINT, audience, checked
from beacon.push.notice import ABOUT, OFF, Notice, notice_of
from beacon.push.setup import (
    NO_CONTACT,
    PUSH_VARIABLE,
    TURNED_OFF,
    Push,
    PushConfigError,
    open_push,
)
from beacon.push.store import (
    KEPT,
    MAX_PERMITS,
    MAX_SUBSCRIPTIONS,
    PERMIT_LIFE,
    PERMIT_SHAPE,
    SPENT,
    Place,
    Subscription,
    Subscriptions,
    SubscriptionsError,
    Try,
)
from beacon.push.vapid import SUBJECT_VARIABLE, Vapid, VapidError, subject_of

__all__ = [
    "ABOUT",
    "KEPT",
    "MAX_ENDPOINT",
    "MAX_PERMITS",
    "MAX_SUBSCRIPTIONS",
    "NO_CONTACT",
    "OFF",
    "PERMIT_LIFE",
    "PERMIT_SHAPE",
    "PUSH_VARIABLE",
    "SPENT",
    "SUBJECT_VARIABLE",
    "TURNED_OFF",
    "Notice",
    "Notifier",
    "Pass",
    "Place",
    "Push",
    "PushConfigError",
    "Subscription",
    "Subscriptions",
    "SubscriptionsError",
    "Try",
    "Vapid",
    "VapidError",
    "audience",
    "checked",
    "notice_of",
    "open_push",
    "subject_of",
]
