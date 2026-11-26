"""What is in force: reading the evidence store's history as the state of things now.

The evidence store keeps what each source said and never changes a row, so a search
returns history. An alert that was replaced, cancelled, or dropped is still found by
its own span, and the row that ended it is a different row. `standings` reads a set
of alert and traffic rows together and says, for each message, how it stands at a
moment:

- An NWS message that a later message names in `data["replaces"]` is `replaced`,
  or `cancelled` when that later message is a `Cancel`: the authority said so.
- A 511 event is a row for each version, and only its latest version can be in force.
- A message whose latest change row (`data["change"]`) is `not_listed` is
  `not_listed`: the source stopped listing it. Nobody announced that it is over,
  and nothing here says that anybody did.
- An alert that ran to its `until` is `ended`.

The caller hands over every row that can end one of the others. The row that ends a
message is later than the message and can be anywhere, so it is searched for with no
distance filter and no upper bound on its age, as `AGENTS.md` says.

A notice is lifted only by another notice. `lifted` says which all-clear Beacon
reads as lifting which order, among notices already matched to one incident: an
earlier order about the same hazard that names a street the all-clear names too.
Two notices that name no street in common are about different places as far as
Beacon can tell, and the order stays as it was. Both
are a model's readings of documents, so the result is how Beacon reads them and
never an instruction: an all-clear can have exceptions that only the document gives.
"""

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from beacon.evidence import Evidence
from beacon.radio.vocabulary import clean_street

State = Literal[
    "in_force", "replaced", "cancelled", "not_listed", "ended", "cancellation", "unknown"
]

# The types of notice an all-clear can lift.
_LIFTED_BY_ALL_CLEAR = frozenset({"evacuation_order", "evacuation_warning", "shelter_in_place"})


@dataclass(frozen=True)
class Standing:
    """How one alert message or one version of a traffic event stands."""

    # `cancellation` is an NWS `Cancel` message itself: it ends others and is never
    # in force. `unknown` is a message that nothing read here ends, when not every
    # row that could end it was read (`beacon.incident.build.build_incidents`).
    state: State
    # The row that ended it: the message that replaced or cancelled it, the later
    # version of the event, or the row that says it is no longer listed.
    by: Evidence | None = None

    @property
    def in_force(self) -> bool:
        return self.state == "in_force"


def standings(rows: Iterable[Evidence], now: datetime) -> dict[int, Standing]:
    """How each alert message and each version of a traffic event among `rows`
    stands at `now`, by the row's id. A row that records a change is not a message
    and has no standing of its own. Rows from after `now` are left out of account."""
    known = [row for row in rows if row.kind in ("alert", "traffic") and row.occurred_at <= now]
    changes: dict[tuple[str, str], Evidence] = {}
    messages: list[Evidence] = []
    for row in sorted(known, key=_order):
        change, about = row.data.get("change"), row.data.get("about")
        if change is None:
            messages.append(row)
        elif isinstance(about, str):
            # The latest change row about a message is the one that holds.
            changes[(row.source, about)] = row
    found: dict[int, Standing] = {}
    alerts = [row for row in messages if row.kind == "alert"]
    for alert in alerts:
        found[alert.id] = _alert_standing(alert, alerts, changes, now)
    versions: dict[tuple[str, object], list[Evidence]] = {}
    for row in messages:
        if row.kind == "traffic":
            versions.setdefault((row.source, row.data.get("event_id")), []).append(row)
    for event in versions.values():
        for earlier, later in itertools.pairwise(event):
            found[earlier.id] = Standing("replaced", later)
        found[event[-1].id] = _listed(event[-1], changes) or Standing("in_force")
    return found


def lifted(notices: Sequence[Evidence]) -> dict[int, Evidence]:
    """Which all-clear Beacon reads as lifting which order, among the notices of
    one incident: the id of each order's row, and the all-clear's row. An all-clear
    lifts the orders about the same hazard that were published before it and name
    a street it names too."""
    found: dict[int, Evidence] = {}
    ordered = sorted(notices, key=_order)
    for place, notice in enumerate(ordered):
        if notice.data.get("notice_type") != "all_clear":
            continue
        streets = set(notice_streets(notice))
        for earlier in ordered[:place]:
            lifts = earlier.data.get("notice_type") in _LIFTED_BY_ALL_CLEAR
            same_hazard = earlier.data.get("hazard") == notice.data.get("hazard")
            if lifts and same_hazard and streets & set(notice_streets(earlier)):
                found.setdefault(earlier.id, notice)
    return found


def notice_streets(row: Evidence) -> list[str]:
    """The streets a notice row names, each in the form Beacon keeps a street in."""
    streets = row.data.get("streets")
    cleaned = (clean_street(street) for street in (streets if isinstance(streets, list) else []))
    return [street for street in cleaned if street is not None]


def _alert_standing(
    alert: Evidence,
    alerts: Sequence[Evidence],
    changes: dict[tuple[str, str], Evidence],
    now: datetime,
) -> Standing:
    if alert.data.get("message_type") == "Cancel":
        return Standing("cancellation")
    for later in alerts:
        named = later.data.get("replaces")
        if later.id == alert.id or later.source != alert.source or _order(later) < _order(alert):
            continue
        if isinstance(named, list) and alert.ref in named:
            cancelled = later.data.get("message_type") == "Cancel"
            return Standing("cancelled" if cancelled else "replaced", later)
    dropped = _listed(alert, changes)
    if dropped is not None:
        return dropped
    if alert.until is not None and alert.until <= now:
        return Standing("ended")
    return Standing("in_force")


def _listed(row: Evidence, changes: dict[tuple[str, str], Evidence]) -> Standing | None:
    """The standing of a message whose source stopped listing it, or None when the
    source lists it, or lists it again."""
    change = changes.get((row.source, row.ref))
    if change is not None and change.data.get("change") == "not_listed":
        return Standing("not_listed", change)
    return None


def _order(row: Evidence) -> tuple[datetime, int]:
    return (row.occurred_at, row.id)
