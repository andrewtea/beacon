"""Opens web push for a server, as the environment asks."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx2

from beacon.ember.client import default_data_dir
from beacon.errors import raise_bare
from beacon.files import Mode
from beacon.incident import Engine
from beacon.ingest.http import SourceHttp
from beacon.investigate import Runs
from beacon.push.desk import Notifier
from beacon.push.store import Subscriptions
from beacon.push.vapid import Vapid, open_vapid

PUSH_VARIABLE = "BEACON_PUSH"


class PushConfigError(Exception):
    """The environment asks for web push that cannot be set up."""


@dataclass(frozen=True)
class Push:
    """Web push for one server: the key, the subscriptions, and the pass that sends."""

    mode: Mode
    vapid: Vapid
    subscriptions: Subscriptions
    notifier: Notifier

    def close(self) -> None:
        """End a send that is out, make no more, and write nothing more."""
        try:
            self.notifier.close()
        finally:
            self.subscriptions.close()


def open_push(
    env: Mapping[str, str],
    engine: Engine,
    runs: Runs,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Push | None:
    """Web push for the store `engine` reads, or None when `BEACON_PUSH=0`.

    The subscriptions are kept in `data/push/mock.sqlite3` or `live.sqlite3`, as
    the evidence store is the mock one or the live one, beside that store's key.
    A push goes to the network, on the mock store too: a device that subscribed to
    the invented scenario is woken, and shows that it is the invented scenario.
    With a `transport`, it answers every push and nothing is sent anywhere.

    Raises PushConfigError for a setting that is not 1 or 0 and for a directory
    that cannot be made, VapidError for a key that cannot be had, and
    SubscriptionsError for a subscriptions file of the other mode or another
    shape."""
    setting = env.get(PUSH_VARIABLE, "1").strip()
    if setting not in ("0", "1"):
        raise PushConfigError(f"{PUSH_VARIABLE} must be 1 or 0.")
    if setting == "0":
        return None
    if runs.mode != engine.mode:
        raise PushConfigError("The investigations file is not the incident engine's store's.")
    directory = default_data_dir(env) / "push"
    made = True
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # The key and the subscriptions are for the server's user only.
        os.chmod(directory, 0o700)
    except OSError:
        made = False
    if not made:
        raise_bare(PushConfigError("The push directory could not be made for this user only."))
    mode = engine.mode
    vapid = open_vapid(env, directory, mode)
    subscriptions = Subscriptions(directory / f"{mode}.sqlite3", mode)
    notifier = Notifier(subscriptions, runs, vapid, SourceHttp(transport))
    return Push(mode=mode, vapid=vapid, subscriptions=subscriptions, notifier=notifier)
