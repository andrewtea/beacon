"""Opens the investigation desk for an incident engine, as the environment asks."""

import logging
from collections.abc import Mapping

from beacon import telling
from beacon.ember import EmberClient, SpendScope, Storage
from beacon.ember.client import default_data_dir
from beacon.ember.pricing import parse_usd
from beacon.incident import Engine
from beacon.investigate.desk import SCOPE, InvestigationDesk
from beacon.investigate.stand_in import stand_in_investigator
from beacon.investigate.store import Runs
from beacon.taps import Taps

BUDGET_VARIABLE = "BEACON_INVESTIGATE_BUDGET_USD"

logger = logging.getLogger("beacon.investigate")


class InvestigateConfigError(Exception):
    """The environment asks for an investigation desk that cannot be built."""


# What a person reads where a clip or a photo is added to a tap, on a server whose
# investigator is Muse Ember and which takes a visitor's own: the one thing of a
# person's that leaves Beacon for Meta. It says when it is sent and no more than
# Beacon knows of what becomes of it there.
MEDIA_SENT = (
    "On this server the investigator is a model, Muse Ember, on Meta's Model API. A "
    "clip or a photo you add to a tap is sent there when the investigator asks what "
    "can be heard or seen in it, and Beacon keeps only which of a short list of "
    "things the model named. Beacon cannot say what Meta keeps of a request."
)


def open_investigations(
    env: Mapping[str, str],
    engine: Engine,
    taps: Taps,
    *,
    paid: telling.PaidClient | None = None,
) -> tuple[Runs, InvestigationDesk | None]:
    """The file the investigations of `taps`' taps are kept in, and the desk that
    makes them against `engine`'s incidents, or None for the desk when taps are not
    investigated.

    The file is opened either way, `data/investigate/mock.sqlite3` or
    `data/investigate/live.sqlite3` as the evidence store is the mock one or the
    live one: the runs and the decisions on crisis mode that were made and paid for
    are read whether or not more are being made. Whoever opens it closes it
    (`Runs.close`); a desk closes it when it is stopped.

    On the mock store a stand-in for Ember investigates, which costs nothing; when
    Muse Ember works on the invented story (`beacon.telling`), it investigates,
    under a scope of the telling's own, and each run says who did. `paid` is how
    that paying client is made, which a test hands in. On the live store a live
    Ember does, under a budget of its own, `BEACON_INVESTIGATE_BUDGET_USD`; with
    no budget set no tap is investigated,
    which the log says, because every investigation is paid for.

    Raises InvestigateConfigError when a budget is set that is not a dollar amount,
    or is set without a live Ember, and RunsError when the file was begun as the
    other mode's or is in a shape this Beacon does not know, and
    TellingConfigError as `beacon.telling.paying` does."""
    data_dir = default_data_dir(env)
    if engine.mode == "mock":
        scope = SCOPE
        if telling.teller_of(env) == "ember":
            scope = telling.TAPS
            ember = telling.paying(env, scope, paid)
        else:
            ember = EmberClient.fake(Storage.under(data_dir), stand_in_investigator)
        try:
            runs = Runs(data_dir / "investigate" / "mock.sqlite3", "mock")
        except BaseException:
            # A client that was built is closed when the desk it was for is not.
            ember.close()
            raise
        return runs, InvestigationDesk(ember, runs, engine, taps, scope=scope)
    path = data_dir / "investigate" / "live.sqlite3"
    budget = env.get(BUDGET_VARIABLE, "").strip()
    if not budget:
        logger.warning(
            "No budget for investigations (%s): a tap is taken and stored, and its "
            "answer says Beacon has no investigation of it. What was investigated "
            "before is still read.",
            BUDGET_VARIABLE,
        )
        return Runs(path, "live"), None
    try:
        scope = SpendScope(SCOPE, parse_usd(budget))
    except ValueError:
        scope = None
    if scope is None:
        raise InvestigateConfigError(
            f"{BUDGET_VARIABLE} is the most, in dollars, that investigating taps may "
            "cost over every run, such as 0.50."
        )
    ember = EmberClient.from_env(env, scope=scope)
    if ember.mode != "live":
        raise InvestigateConfigError(
            f"{BUDGET_VARIABLE} is set, and a tap is investigated by a live Ember and "
            "cannot be replayed: set REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD."
        )
    try:
        runs = Runs(path, "live")
    except BaseException:
        # A client that was built is closed when the desk it was for is not.
        ember.close()
        raise
    return runs, InvestigationDesk(ember, runs, engine, taps)
