"""Opens the incident engine on an evidence store, with the blocks and the names the
environment gives it."""

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from beacon.evidence import EvidenceStore
from beacon.incident.engine import Engine
from beacon.incident.places import NO_BLOCKS, Blocks, read_blocks
from beacon.ingest.setup import read_known
from beacon.radio.vocabulary import read_names
from beacon.redact import Names
from beacon.scenario import BLOCK_POINTS
from beacon.scenario import notices as scenario_notices
from beacon.scenario import radio as scenario_radio

BLOCKS_VARIABLE = "BEACON_BLOCKS_FILE"
PLACES_VARIABLE = "BEACON_PLACES_FILE"

logger = logging.getLogger("beacon.incident")


class IncidentConfigError(Exception):
    """The environment asks for an engine that cannot be built."""


def open_engine(
    env: Mapping[str, str],
    store: EvidenceStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Engine:
    """The engine that reads `store`. On the mock store its blocks are the story's
    own. On the live store they are the ones in the file `BEACON_BLOCKS_FILE` names,
    and with no file a radio call's place has no point, which the log says. `clock`
    replaces the system's time, which is how tests set it.

    The names a text may hold to pass the redaction pass are the story's own on the
    mock store, and on the live store the streets and units of the radio's lists
    (`BEACON_RADIO_STREETS_FILE`, `BEACON_RADIO_UNITS_FILE`) with the public places
    in the file `BEACON_PLACES_FILE` names.

    Raises IncidentConfigError when a file that is named cannot be read, and
    IngestConfigError for one of the radio's lists."""
    if store.mode == "mock":
        return Engine(store, Blocks.of(BLOCK_POINTS), names=mock_names(), clock=clock)
    names = _live_names(env)
    path = env.get(BLOCKS_VARIABLE)
    if not path:
        logger.warning(
            "No list of blocks (%s): an incident heard on the radio has a point only "
            "when 511 lists a closure on its street, and no tap is matched to it otherwise.",
            BLOCKS_VARIABLE,
        )
        return Engine(store, NO_BLOCKS, names=names, clock=clock)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise IncidentConfigError(
            f"{BLOCKS_VARIABLE} names a file that cannot be read as text."
        ) from error
    blocks, left_out = read_blocks(text)
    if left_out:
        logger.warning(
            "%d lines in the list of blocks are not a street, a block, and a position, "
            "and were left out.",
            left_out,
        )
    return Engine(store, blocks, names=names, clock=clock)


def mock_names() -> Names:
    """The names a text about the mock scenario may hold: the streets and units of
    its radio calls, and the streets and the place its notices name."""
    return Names.of(
        streets=(*scenario_radio.KNOWN.streets, *scenario_notices.STREETS),
        units=scenario_radio.KNOWN.units,
        places=scenario_notices.PLACES,
    )


def _live_names(env: Mapping[str, str]) -> Names:
    """The names a text about real evidence may hold. With no list of streets no
    text that names a street passes, which the log says where the radio's lists
    are read."""
    known = read_known(env)
    places: list[str] = []
    path = env.get(PLACES_VARIABLE)
    if path:
        try:
            places = read_names(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            raise IncidentConfigError(
                f"{PLACES_VARIABLE} names a file that cannot be read as text."
            ) from error
    return Names.of(streets=known.streets, units=known.units, places=places)
