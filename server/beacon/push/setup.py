"""Opens web push for a server, as the environment asks."""

import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import httpx2

from beacon.ember.client import default_data_dir
from beacon.errors import raise_bare
from beacon.files import Mode
from beacon.incident import Engine, Snapshot
from beacon.ingest.http import SourceHttp
from beacon.investigate import Runs
from beacon.push.desk import Notifier, Pass
from beacon.push.store import Subscriptions, SubscriptionsError
from beacon.push.vapid import SUBJECT_VARIABLE, Vapid, open_vapid, subject_of

PUSH_VARIABLE = "BEACON_PUSH"

# Why a server that keeps subscriptions sends nothing to them, for its operator.
TURNED_OFF = f"{PUSH_VARIABLE}=0"
NO_CONTACT = (
    f"{SUBJECT_VARIABLE} names nobody a push service may write to about this server, "
    "and a push service may refuse a push that names nobody"
)


class PushConfigError(Exception):
    """The environment asks for web push that cannot be set up."""


@dataclass(frozen=True)
class Push:
    """Web push for one server: the subscriptions, and, when this server sends
    notifications, the key and the pass that sends.

    **What is kept is deleted on time whether or not anything is sent.** A server
    that sends no notifications (`sends` is False) still has the file of the
    subscriptions an earlier one kept: it takes no new one, deletes one when its
    device asks, and deletes each when its time is over. So does it for the file
    of the other mode, which only time empties here (`others`)."""

    mode: Mode
    subscriptions: Subscriptions
    # None on a server that sends no notifications.
    vapid: Vapid | None
    notifier: Notifier | None
    # Why this server sends none, for its operator, or None when it sends.
    off: str | None = None
    # The other mode's file, when there is one: swept, and nothing else.
    others: tuple[Subscriptions, ...] = ()
    # Whether the other mode has a file this Beacon could not open, which it
    # therefore does not sweep.
    unswept: bool = False

    @property
    def sends(self) -> bool:
        return self.notifier is not None

    def work(self, snapshot: Callable[[], Snapshot], now: datetime) -> Pass:
        """One pass: delete what ran out, from each file by itself, and then send
        what is due when this server sends. The deleting comes first and depends
        on nothing else: `snapshot`, which is asked for a reading of the incidents
        only after it and only by a server that sends, can fail without anything
        that ran out being kept for it. A file that could not be swept is counted
        (`Pass.unswept`) and does not keep the next from being swept.

        Raises SubscriptionsError when a file is closed, and whatever `snapshot`
        raises."""
        # The budget of the pass begins here, before anything is swept or read.
        stop_at = None if self.notifier is None else self.notifier.deadline()
        unswept = 0
        for kept in (self.subscriptions, *self.others):
            try:
                kept.sweep(now)
            except sqlite3.Error:
                unswept += 1
        if self.notifier is None:
            return Pass(unswept=unswept)
        return replace(self.notifier.notify(snapshot(), now, stop_at=stop_at), unswept=unswept)

    def close(self) -> None:
        """End a send that is out, make no more, and write nothing more."""
        try:
            if self.notifier is not None:
                self.notifier.close()
        finally:
            self.subscriptions.close()
            for other in self.others:
                other.close()


def open_push(
    env: Mapping[str, str],
    engine: Engine,
    runs: Runs,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Push | None:
    """Web push for the store `engine` reads.

    The subscriptions are kept in `data/push/mock.sqlite3` or `live.sqlite3`, as
    the evidence store is the mock one or the live one, beside that store's key.
    A push goes to the network, on the mock store too: a device that subscribed to
    the invented scenario is woken, and shows that it is the invented scenario.
    With a `transport`, it answers every push and nothing is sent anywhere.

    A server sends notifications when `BEACON_PUSH` is not 0 and
    `BEACON_VAPID_SUBJECT` names a contact. One that does not send still opens
    the files there are, so that what they hold is deleted on time (`Push`), and
    makes no key. The answer is None only when notifications are turned off and
    there is no file of either mode: nothing is kept, and nothing is made.

    Raises PushConfigError for a setting that is not 1 or 0 and for a directory
    that cannot be made, VapidError for a contact that cannot be used by a server
    that is to send and for a key that cannot be had, and SubscriptionsError for a
    subscriptions file of this mode that is the other mode's or in another shape."""
    setting = env.get(PUSH_VARIABLE, "1").strip()
    if setting not in ("0", "1"):
        raise PushConfigError(f"{PUSH_VARIABLE} must be 1 or 0.")
    if runs.mode != engine.mode:
        raise PushConfigError("The investigations file is not the incident engine's store's.")
    # Read only by a server that is to send: one that only deletes is not kept
    # from starting, and so from deleting, by a contact it would never use.
    subject = subject_of(env) if setting == "1" else None
    mode = engine.mode
    other: Mode = "live" if mode == "mock" else "mock"
    directory = default_data_dir(env) / "push"
    off = TURNED_OFF if setting == "0" else NO_CONTACT if subject is None else None
    if setting == "0" and not any(_file(directory, each).exists() for each in (mode, other)):
        return None
    made = True
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # The key and the subscriptions are for the server's user only.
        os.chmod(directory, 0o700)
    except OSError:
        made = False
    if not made:
        raise_bare(PushConfigError("The push directory could not be made for this user only."))
    subscriptions = Subscriptions(_file(directory, mode), mode)
    others: tuple[Subscriptions, ...] = ()
    unswept = False
    if _file(directory, other).exists():
        try:
            others = (Subscriptions(_file(directory, other), other),)
        except SubscriptionsError:
            # A newer Beacon's, say. It is left as it is, and the operator is told.
            unswept = True
    vapid: Vapid | None = None
    notifier: Notifier | None = None
    if off is None and subject is not None:
        vapid = open_vapid(directory, mode, subject)
        notifier = Notifier(subscriptions, runs, vapid, SourceHttp(transport))
    return Push(
        mode=mode,
        subscriptions=subscriptions,
        vapid=vapid,
        notifier=notifier,
        off=off,
        others=others,
        unswept=unswept,
    )


def _file(directory: Path, mode: Mode) -> Path:
    return directory / f"{mode}.sqlite3"
