"""Noticing when a source stops listing something.

NWS and 511 answer with what is active now. An alert that is cancelled, or a closure
that is lifted, is not announced: it is missing from the next answer. A poller hands
`no_longer_listed` what the source lists now, and gets back the rows whose items have
gone, so it can store that they cleared and when.

An item is taken as gone when it has been missing from two answers in a row, because
a source can leave an item out of one answer by mistake.
"""

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

from beacon.evidence import Evidence, EvidenceStore

MISSES_BEFORE_GONE = 2


def no_longer_listed(
    store: EvidenceStore,
    *,
    poller: str,
    source: str,
    listed: Mapping[str, str],
    replaced: Collection[str] = (),
    complete: bool,
    now: datetime,
) -> tuple[list[Evidence], dict[str, Any]]:
    """The stored rows of the items that have gone, and the state to save with this
    poll's rows so the next poll can tell.

    `listed` maps each item the source lists now to the reference of the row that
    holds it. `replaced` names items the source says were superseded, which are not
    gone. An item whose row had an `until` that has passed ended as it said it would,
    and is not reported. When the answer was not read in full (`complete` is false),
    nothing counts as missing from it."""
    previous = store.state(poller).get("listed")
    state: dict[str, Any] = {key: {"ref": ref, "missed": 0} for key, ref in listed.items()}
    gone: list[Evidence] = []
    for key, entry in (previous if isinstance(previous, dict) else {}).items():
        if key in listed or key in replaced:
            continue
        if not complete:
            state[key] = entry
            continue
        row = store.find(source, entry["ref"])
        if row is None or (row.until is not None and row.until <= now):
            continue
        missed = entry["missed"] + 1
        if missed >= MISSES_BEFORE_GONE:
            gone.append(row)
        else:
            state[key] = {"ref": entry["ref"], "missed": missed}
    return gone, {"listed": state}
