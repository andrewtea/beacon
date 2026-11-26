"""Ingest: one poller per source, each writing evidence rows.

    from beacon.ingest import build

    ingest = build(os.environ)   # the mock scenario unless BEACON_LIVE_SOURCES names a source
    ingest.run(stop)             # polls until the threading.Event is set

See `setup` for how a source goes live, `runner` for the loop, and a source's own
module (`nws`, `opensky`, `traffic511`) for what its rows hold.
"""

from beacon.ingest.http import SourceError
from beacon.ingest.runner import Clock, Poller, PollerStatus, PollReport, Runner
from beacon.ingest.setup import (
    SOURCES,
    Ingest,
    IngestConfigError,
    build,
    live_sources,
    open_store,
    source_pollers,
)

__all__ = [
    "SOURCES",
    "Clock",
    "Ingest",
    "IngestConfigError",
    "PollReport",
    "Poller",
    "PollerStatus",
    "Runner",
    "SourceError",
    "build",
    "live_sources",
    "open_store",
    "source_pollers",
]
