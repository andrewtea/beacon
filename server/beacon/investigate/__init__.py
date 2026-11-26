"""The investigator: what Ember finds out about one tap, and what is kept of it.

    from beacon.investigate import open_investigations

    desk = open_investigations(os.environ, engine, taps)  # request.app.state.investigations
    desk.submit(tap_id)  # hand a tap in; it is looked into on a thread of the desk's
    found = desk.runs.get(tap_id)  # (claimed_at, Run | None) or None; asks Ember nothing
    desk.runs.crisis(incident_id)  # the decision on crisis mode, or None

See `tools` for what Ember may look at, `loop` for the run and how code checks its
answer, `card` for the words a person is shown, `store` for where runs are kept,
`desk` for when one is paid for, and `stand_in` for the stand-in that investigates
with no key.
"""

from beacon.investigate.desk import QUEUE, SCOPE, WORKERS, InvestigationDesk
from beacon.investigate.loop import (
    GROUNDS,
    MAX_ROUNDS,
    TAP_BUDGET_USD,
    UNCONFIRMED,
    Investigation,
    Outcome,
    Round,
    Run,
    investigate_tap,
)
from beacon.investigate.setup import BUDGET_VARIABLE, InvestigateConfigError, open_investigations
from beacon.investigate.stand_in import stand_in_investigator
from beacon.investigate.store import KEPT, Decision, Runs, RunsError
from beacon.investigate.tools import (
    MINUTES,
    RADII_M,
    SIGHTS,
    SOUNDS,
    THINKING,
    TOOL_NAMES,
    WHY,
    Accounting,
    Call,
    Found,
    Scene,
)

__all__ = [
    "BUDGET_VARIABLE",
    "GROUNDS",
    "KEPT",
    "MAX_ROUNDS",
    "MINUTES",
    "QUEUE",
    "RADII_M",
    "SCOPE",
    "SIGHTS",
    "SOUNDS",
    "TAP_BUDGET_USD",
    "THINKING",
    "TOOL_NAMES",
    "UNCONFIRMED",
    "WHY",
    "WORKERS",
    "Accounting",
    "Call",
    "Decision",
    "Found",
    "InvestigateConfigError",
    "Investigation",
    "InvestigationDesk",
    "Outcome",
    "Round",
    "Run",
    "Runs",
    "RunsError",
    "Scene",
    "investigate_tap",
    "open_investigations",
    "stand_in_investigator",
]
