"""Noticing when a source stops listing something, and when it lists it again.

NWS and 511 answer with what is active now. An alert that is cancelled, or a closure
that is lifted, is not announced: it is missing from the next answer. A poller hands
`compare_listing` what the source lists now and gets back the rows whose items have
gone, so it can store that they are no longer listed, and the rows whose items have
come back after that, so it can store that they are listed again.

Nobody said the item is over. Its absence is what Beacon saw, and a row that records
it says so and is not `official`.

An item is taken as gone when it has been missing from two answers in a row, because
a source can leave an item out of one answer by mistake.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from beacon.evidence import Evidence, EvidenceStore

MISSES_BEFORE_GONE = 2


@dataclass(frozen=True)
class Listing:
    # Rows of items that are no longer listed, as of this poll.
    gone: list[Evidence]
    # Rows of items that were reported gone, or ran out, and are listed again.
    back: list[Evidence]
    # What to save with this poll's rows, so that the next poll can tell.
    state: dict[str, Any]


def compare_listing(
    store: EvidenceStore,
    *,
    poller: str,
    source: str,
    listed: Mapping[str, str],
    replaced: Collection[str] = (),
    complete: bool,
    now: datetime,
) -> Listing:
    """Compare what a source lists now with what it listed at the last poll.

    `listed` maps each item the source lists now to the reference of the row that
    holds it. `replaced` names items the source says were superseded, which are not
    gone. An item whose row had an `until` that has passed ended as it said it would,
    and is not reported. When the answer was not read in full (`complete` is false),
    nothing counts as missing from it.

    A poll that is made twice for the same moment, as when a step of the mock
    scenario is played again after a crash, changes nothing the second time."""
    saved = store.state(poller)
    previous = saved.get("listed")
    previous = previous if isinstance(previous, dict) else {}
    moment = now.isoformat()
    if saved.get("at") == moment:
        return Listing([], [], saved)
    state: dict[str, Any] = {key: {"ref": ref, "missed": 0} for key, ref in listed.items()}
    gone: list[Evidence] = []
    back: list[Evidence] = []
    for key, ref in listed.items():
        if key in previous:
            continue
        # Not listed at the last poll, and its row is already stored: it was listed
        # before that, was dropped, and is back.
        row = store.find(source, ref)
        if row is not None:
            back.append(row)
    for key, entry in previous.items():
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
    return Listing(gone, back, {"at": moment, "listed": state})


def not_listed_row(row: Evidence, now: datetime) -> dict[str, Any]:
    """The fields every "no longer listed" row shares. Its reference holds the
    moment, so an item that goes, comes back, and goes again has a row for each."""
    return {
        "kind": row.kind,
        "source": row.source,
        "ref": f"{row.ref}#not-listed@{now.isoformat()}",
        "occurred_at": now,
        "point": row.point,
        "place": row.place,
        # The source is an authority, and it did not say this: Beacon saw the item
        # missing from two of its answers.
        "confidence": "medium",
        "origin": row.origin,
    }


def listed_again_row(row: Evidence, now: datetime) -> dict[str, Any]:
    """The fields every "listed again" row shares."""
    return not_listed_row(row, now) | {
        "ref": f"{row.ref}#listed-again@{now.isoformat()}",
        "until": row.until,
        # The source lists it, which is the source speaking.
        "confidence": row.confidence,
    }
