"""The incident engine: evidence rows grouped into incidents, with a source on every claim.

    from beacon.incident import open_engine

    engine = open_engine(os.environ, store)
    for incident in engine.snapshot().incidents:
        incident.title, incident.status, incident.place      # "Fire at the 400 block of Hayes St"
        for entry in incident.timeline:
            entry.text, entry.confidence, entry.sources      # what one source said, and which

See `calls` for which radio calls are one incident, `build` for what else is attached
to it and how its lines are worded, `inforce` for what is in force among alerts,
closures, and notices, `aircraft` for tracks and circling, `places` for the list of
blocks that gives a radio call's place a point, and `engine` for the reader.
"""

from beacon.incident.aircraft import Track, tracks
from beacon.incident.build import build_incidents, incident_id, source_of, weakest
from beacon.incident.calls import IDLE, CallGroup, group_calls
from beacon.incident.engine import HORIZON, Engine, Snapshot
from beacon.incident.inforce import Standing, standings
from beacon.incident.model import (
    CONFIDENCE_RANK,
    Credit,
    Entry,
    Incident,
    Official,
    Signal,
    Source,
)
from beacon.incident.places import NO_BLOCKS, Blocks, read_blocks
from beacon.incident.setup import BLOCKS_VARIABLE, IncidentConfigError, open_engine

__all__ = [
    "BLOCKS_VARIABLE",
    "CONFIDENCE_RANK",
    "HORIZON",
    "IDLE",
    "NO_BLOCKS",
    "Blocks",
    "CallGroup",
    "Credit",
    "Engine",
    "Entry",
    "Incident",
    "IncidentConfigError",
    "Official",
    "Signal",
    "Snapshot",
    "Source",
    "Standing",
    "Track",
    "build_incidents",
    "group_calls",
    "incident_id",
    "open_engine",
    "read_blocks",
    "source_of",
    "standings",
    "tracks",
    "weakest",
]
