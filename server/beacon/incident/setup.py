"""Opens the incident engine on an evidence store, with the blocks the environment names."""

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from beacon.evidence import EvidenceStore
from beacon.incident.engine import Engine
from beacon.incident.places import NO_BLOCKS, Blocks, read_blocks
from beacon.scenario import BLOCK_POINTS

BLOCKS_VARIABLE = "BEACON_BLOCKS_FILE"

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

    Raises IncidentConfigError when a file that is named cannot be read."""
    if store.mode == "mock":
        return Engine(store, Blocks.of(BLOCK_POINTS), clock=clock)
    path = env.get(BLOCKS_VARIABLE)
    if not path:
        logger.warning(
            "No list of blocks (%s): an incident heard on the radio has a point only "
            "when 511 lists a closure on its street, and no tap is matched to it otherwise.",
            BLOCKS_VARIABLE,
        )
        return Engine(store, NO_BLOCKS, clock=clock)
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
    return Engine(store, blocks, clock=clock)
