"""Noticing when a source stops listing something, and when it lists it again.

NWS and 511 answer with what is active now. An alert that is dropped, or a closure
that is lifted, is mostly not announced: it is missing from the next answer. (NWS can
also cancel an alert outright, and that message is a row of its own.) A poller hands
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

from beacon.evidence import Evidence, EvidenceStore, Origin

MISSES_BEFORE_GONE = 2


@dataclass(frozen=True)
class Listing:
    # Rows of items that are no longer listed, as of this poll.
    gone: list[Evidence]
    # Rows of items that were reported gone, or ran out, and are listed again.
    back: list[Evidence]
    # What to save with this poll's rows, so that the next poll can tell.
    state: dict[str, Any]
    # The origin of the rows that record these changes. A change is worked out from
    # this poll and the ones before it, and the saved state does not say which
    # poller made each of those. So in the mock store, where a stand-in may have
    # made any of them, every change is a stand-in's. The live store takes no
    # stand-in's poll at all, so there a change is as live as the poller.
    origin: Origin


def compare_listing(
    store: EvidenceStore,
    *,
    poller: str,
    source: str,
    listed: Mapping[str, str],
    replaced: Collection[str] = (),
    complete: bool,
    now: datetime,
    origin: Origin,
) -> Listing:
    """Compare what a source lists now with what it listed at the last poll.
    `origin` is the origin of the poller that asks.

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
    change_origin: Origin = "mock" if store.mode == "mock" else origin
    if saved.get("at") == moment:
        return Listing([], [], saved, change_origin)
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
    return Listing(gone, back, {"at": moment, "listed": state}, change_origin)


def not_listed_row(row: Evidence, now: datetime, origin: Origin) -> dict[str, Any]:
    """The fields every "no longer listed" row shares. Its reference holds the
    moment, so an item that goes, comes back, and goes again has a row for each.
    `origin` is `Listing.origin`. The row is a stand-in's when that is, and when the
    row it is about is one, whose place and words the new row repeats."""
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
        "origin": "mock" if "mock" in (origin, row.origin) else "live",
        # A fake Ember's part in the row this is about is a part in this one.
        "model": "fake" if row.model == "fake" else None,
    }


def listed_again_row(row: Evidence, now: datetime, origin: Origin) -> dict[str, Any]:
    """The fields every "listed again" row shares."""
    return not_listed_row(row, now, origin) | {
        "ref": f"{row.ref}#listed-again@{now.isoformat()}",
        "until": row.until,
        # The source lists it, which is the source speaking.
        "confidence": row.confidence,
    }
