"""Shared test setup: tests run in replay mode, without an API key, and with a guard
on Python's socket connections to other machines.

Ember calls cost real money from a $10 budget, and going over it disqualifies the
entry. What keeps a test run from spending it is the missing key: this module removes
MODEL_API_KEY when pytest imports it, which is before any test module or fixture
runs, and CI holds no key at all.

The connection guard is a backstop, not a sandbox. It replaces `socket.socket.connect`
and `connect_ex` in this process with versions that raise `NetworkBlockedError` for
an address that is not this machine. A test that catches that error still passes. The
guard does not cover DNS lookups, datagrams sent without `connect`, subprocesses, or
native code that opens sockets without Python's `socket` module.
"""

import ipaddress
import os
import socket

import pytest

os.environ["REPLAY"] = "1"
os.environ.pop("MODEL_API_KEY", None)


class NetworkBlockedError(RuntimeError):
    """Raised when a test opens a connection to anything but this machine."""


def _is_local(address: object) -> bool:
    # AF_INET and AF_INET6 addresses are (host, port, ...) tuples. Anything else,
    # such as an AF_UNIX path, cannot leave the machine.
    if not isinstance(address, tuple):
        return True
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard(method_name: str) -> None:
    real = getattr(socket.socket, method_name)

    def guarded(self: socket.socket, address: object, /):
        if not _is_local(address):
            raise NetworkBlockedError(
                f"Test tried to connect to {address!r}. Tests must run offline: "
                "replay recorded responses instead of calling the API or a data source."
            )
        return real(self, address)

    setattr(socket.socket, method_name, guarded)


# Installed at import, for the whole test process, so module imports and
# session-scoped fixtures are guarded as well as test functions.
_guard("connect")
_guard("connect_ex")


@pytest.fixture(autouse=True)
def offline_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Start every test in replay mode without a key or a spending stop, whatever
    earlier tests or the shell set, and with a data directory of its own, so no test
    writes to the repository's `data/` or sees what another test stored."""
    monkeypatch.setenv("REPLAY", "1")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("EMBER_STOP_USD", raising=False)
    monkeypatch.delenv("EMBER_SPENT_BEFORE_USD", raising=False)
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    # No test asks a real source anything: the pollers run on the mock scenario
    # unless a test names a live source itself, with a transport of its own.
    for name in (
        "BEACON_LIVE_SOURCES",
        "BEACON_INGEST",
        "BEACON_511_API_KEY",
        "BEACON_OPENSKY_PERMITTED",
        "OPENSKY_CLIENT_ID",
        "OPENSKY_CLIENT_SECRET",
        "BEACON_OPENMHZ_PERMITTED",
        "BEACON_OPENMHZ_TALKGROUPS",
        "BEACON_OPENMHZ_EMS_TALKGROUPS",
        "BEACON_OPENMHZ_MEDIA_HOSTS",
        "BEACON_OPENMHZ_BUDGET_USD",
        "BEACON_RADIO_STREETS_FILE",
        "BEACON_RADIO_UNITS_FILE",
        "BEACON_NOTICES_FEED_URL",
        "BEACON_NOTICES_HOSTS",
        "BEACON_NOTICES_BUDGET_USD",
        "BEACON_BLOCKS_FILE",
        "BEACON_PLACES_FILE",
        "BEACON_STORY_BUDGET_USD",
    ):
        monkeypatch.delenv(name, raising=False)
