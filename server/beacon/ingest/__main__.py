"""`python -m beacon.ingest`: run the pollers outside the server.

    uv run python -m beacon.ingest            # poll once and print what the store holds
    uv run python -m beacon.ingest --follow   # keep polling until interrupted

With `BEACON_LIVE_SOURCES` unset this plays the mock scenario and touches no network.
The server runs the same pollers itself, so this is for a data directory no server
is polling for: one with no server, or one whose server was started without its
pollers (`BEACON_INGEST=0`). Beside a server that runs the pollers it is refused:
that server has the directory's stores alone (`beacon.datadir`).
"""

import argparse
import contextlib
import logging
import os
import threading

from beacon.datadir import DataLock, DataLockedError
from beacon.ember.client import default_data_dir
from beacon.ember.errors import EmberConfigError
from beacon.evidence import EvidenceStoreError
from beacon.ingest.setup import IngestConfigError, build
from beacon.telling import MOCK_EMBER_VARIABLE, TellingConfigError, teller_of


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m beacon.ingest", description=__doc__)
    parser.add_argument("--follow", action="store_true", help="keep polling until interrupted")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        # Held from before the store is opened until it is closed, together with a
        # server that runs no pollers and never beside one that does.
        lock = DataLock(default_data_dir(os.environ), shared=True)
    except (DataLockedError, EmberConfigError) as error:
        raise SystemExit(str(error)) from error
    try:
        _poll(options.follow)
    finally:
        lock.close()


def _poll(follow: bool) -> None:
    try:
        if teller_of(os.environ) == "ember":
            # This command shares the stores (`DataLock`), and a telling Muse
            # Ember is paid for has one process, which has them alone.
            raise IngestConfigError(
                f"{MOCK_EMBER_VARIABLE}=live is for the server (`python -m beacon.serve`), "
                "which has the data directory's stores alone while Muse Ember works on "
                "the invented story. This command shares them, and does not."
            )
        ingest = build(os.environ)
    except (IngestConfigError, EvidenceStoreError, EmberConfigError, TellingConfigError) as error:
        raise SystemExit(str(error)) from error
    try:
        ingest.catch_up()
        if ingest.scenario is None:
            ingest.runner.step()
        print(f"Evidence store: {ingest.store.path} ({ingest.store.mode})")
        counts = ingest.store.count_by_source()
        for status in ingest.runner.status():
            # Why a poll failed is in the log above.
            outcome = {True: "ok", False: "failed", None: "not polled"}[status.ok]
            print(f"  {status.name:<8} {counts.get(status.name, 0):>6} rows  {outcome}")
        if follow:
            with contextlib.suppress(KeyboardInterrupt):
                ingest.run(threading.Event())
    finally:
        ingest.close()


if __name__ == "__main__":
    main()
