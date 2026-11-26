"""`python -m beacon.ingest`: run the pollers outside the server.

    uv run python -m beacon.ingest            # poll once and print what the store holds
    uv run python -m beacon.ingest --follow   # keep polling until interrupted

With `BEACON_LIVE_SOURCES` unset this plays the mock scenario and touches no network.
The server runs the same pollers itself, so use `--follow` only when it is not running.
"""

import argparse
import contextlib
import logging
import os
import threading

from beacon.ember.errors import EmberConfigError
from beacon.evidence import EvidenceStoreError
from beacon.ingest.setup import IngestConfigError, build


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m beacon.ingest", description=__doc__)
    parser.add_argument("--follow", action="store_true", help="keep polling until interrupted")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        ingest = build(os.environ)
    except (IngestConfigError, EvidenceStoreError, EmberConfigError) as error:
        raise SystemExit(str(error)) from error
    ingest.catch_up()
    ingest.runner.step()
    print(f"Evidence store: {ingest.store.path} ({ingest.store.mode})")
    counts = ingest.store.count_by_source()
    for status in ingest.runner.status():
        # Why a poll failed is in the log above.
        outcome = "ok" if status.ok else "failed"
        print(f"  {status.name:<8} {counts.get(status.name, 0):>6} rows  {outcome}")
    if options.follow:
        with contextlib.suppress(KeyboardInterrupt):
            ingest.runner.run(threading.Event())


if __name__ == "__main__":
    main()
