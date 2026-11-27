"""Ingest: one poller per source, each writing evidence rows.

    from beacon.ingest import build

    ingest = build(os.environ)   # the mock scenario unless BEACON_LIVE_SOURCES names a source
    ingest.run(stop)             # polls until the threading.Event is set

See `setup` for how a source goes live, `runner` for the loop, a source's own module
(`nws`, `adsblol`, `traffic511`, `openmhz`, `notices`) for what its rows hold, and
`paid` for what the pollers that pay Ember for each item keep to.
"""

from beacon.ingest.http import SourceError, SourceHttp
from beacon.ingest.runner import (
    Clock,
    PaidWork,
    Poller,
    PollerHeldError,
    PollerStatus,
    PollReport,
    Runner,
)
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
    "PaidWork",
    "PollReport",
    "Poller",
    "PollerHeldError",
    "PollerStatus",
    "Runner",
    "SourceError",
    "SourceHttp",
    "build",
    "live_sources",
    "open_store",
    "source_pollers",
]
