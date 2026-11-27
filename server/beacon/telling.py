"""Who does a model's work on the invented story: a stand-in for Muse Ember, or Muse Ember.

    BEACON_DATA_DIR=/the/one/data/directory BEACON_FRONT_END=unverified \\
      BEACON_MOCK_EMBER=live BEACON_MOCK_EMBER_BUDGET_USD=0.10 \\
      REPLAY=0 MODEL_API_KEY=... EMBER_SPENT_BEFORE_USD=... python -m beacon.serve

By default (`stand-in`) every Ember client of a server on the mock store is a
stand-in that works by rule and costs nothing, and everything it had a part in says
so. `BEACON_MOCK_EMBER=live` has Muse Ember itself do the four things a model does
in Beacon, on the invented story's evidence:

- hear its radio calls, which are then synthesized speech of invented words, made
  in memory (`beacon.scenario.radio.SpokenShelf`): a tone says nothing, and is
  never sent to a paid Ember;
- read its notices, the invented PDFs;
- pick the points an incident's story is told by;
- and investigate taps.

**The story stays invented, and stays in the mock store.** Its rows are `mock`
rows, the incidents built from them are a stand-in's (`stand_in`), and every view
says the scenario is invented. What changes is who did the model's part, which each
thing says of itself: a row's `model`, a story's, and a run's are `live`, and a
sentence about one says a model did it. Nothing here makes an invented row a live
one, and the live store still refuses every one of them.

**A tap is a person's, whatever the story is.** On a server that takes a visitor's
own (`beacon.front`), a clip or a photo that was added to a tap is sent to Muse
Ember when the investigator asks what is in it, as on real sources, and the app
says so where one is added (`beacon.investigate.setup.MEDIA_SENT`). A server that
is to send Meta nothing but invented material takes only the invented places
(`BEACON_FRONT_END=unverified`), which takes no clip and no photo.

**It is paid for, under a budget with no default**, split among four spending
scopes of its own (`SCOPES`), so that what the invented story cost is in the ledger
apart from what real sources cost, and so that the investigator is not starved by
the story desk. **A mock store is one teller's for as long as it is kept**
(`beacon.ingest.setup.keep_teller`): a stand-in does not go on with a store Ember
worked on, nor Ember with a stand-in's, with the pollers or without them, and what
tells the story again by deleting the store refuses one that was paid for.
"""

import os
import sys
from collections.abc import Callable, Mapping
from typing import Literal

from beacon.ember import EmberClient, SpendScope
from beacon.ember.client import Storage, default_data_dir
from beacon.ember.errors import EmberConfigError
from beacon.ember.ledger import FILE_NAME, kept_file
from beacon.ember.pricing import SPENT_BEFORE_VARIABLE, parse_usd, read_stop_settings

MOCK_EMBER_VARIABLE = "BEACON_MOCK_EMBER"
BUDGET_VARIABLE = "BEACON_MOCK_EMBER_BUDGET_USD"
# The ledger file a telling by Muse Ember pays from, by the name a client gives the
# file it is open on (`EmberClient.ledger_file`: generation, device, inode). When
# it is set, no paying client is made unless the ledger under the data directory
# is there and is that file, at every start of a server: a client begins a ledger
# where it finds none, and a ledger begun anew, or a copy put in its place, counts
# less than was spent. It only refuses. `python -m beacon.telling` prints the name
# of the ledger that is there, for whoever starts a server to hand it on
# (`scripts/film`). What it cannot tell is a ledger rolled back in place.
LEDGER_VARIABLE = "BEACON_MOCK_EMBER_LEDGER"

# Who does the model's work on the invented story.
Teller = Literal["stand_in", "ember"]
_SETTINGS: dict[str, Teller] = {"stand-in": "stand_in", "live": "ember"}

# The spending scopes Muse Ember's work on the invented story is paid under, each
# with its share of the budget in hundredths. The shares are from what the first
# telling cost on Nov 27, 2026 (README, "The invented story, told by Muse Ember"):
# hearing the story's calls about $0.003 and reading its notices about $0.002, an
# incident's story about $0.0005 each time its lines have changed, which is most
# of what a telling costs, and a tap's investigation under $0.001.
RADIO = "telling:radio"
NOTICES = "telling:notices"
STORIES = "telling:stories"
TAPS = "telling:taps"
SCOPES: Mapping[str, int] = {RADIO: 15, NOTICES: 10, STORIES: 45, TAPS: 30}

# How a paid client is made for a scope: `EmberClient.from_env`, unless a test hands
# in a live client over a transport of its own.
PaidClient = Callable[[SpendScope], EmberClient]

# What `GET /api/health` says of a server on which Muse Ember works on the invented
# story, shown with what says the scenario is invented.
TOLD_BY_EMBER = (
    "On this server a model, Muse Ember, hears the invented story's radio calls, which "
    "are synthesized speech, reads its notices, picks the points of its stories, and "
    "investigates taps. The story is still made up, and what the model makes of it can "
    "be wrong."
)
# Said to whoever starts a server on a mock store the other worked on. It advises
# no deletion and no second data directory: a store Muse Ember worked on holds the
# claims on what was paid for, and the ledger is the data directory's.
OTHER_TELLER = (
    "The mock store under this data directory is one {began} worked on, and this "
    "server is set to work on it {now} ({variable}). A mock store is one or the "
    "other's for as long as it is kept, so that nothing a stand-in made is shown "
    "beside what Muse Ember made as if they were one telling. Start the server as "
    "the store was begun: {how}."
)
_AS = {"stand_in": "a stand-in for Muse Ember", "ember": "Muse Ember"}
_WITH = {"stand_in": "with a stand-in for Muse Ember", "ember": "with Muse Ember"}
_HOW = {
    "stand_in": f"without {MOCK_EMBER_VARIABLE}, or with {MOCK_EMBER_VARIABLE}=stand-in",
    "ember": f"with {MOCK_EMBER_VARIABLE}=live and its budget",
}


class TellingConfigError(Exception):
    """The environment asks for a telling of the invented story that cannot be had.
    The text says what to change."""


def teller_of(env: Mapping[str, str]) -> Teller:
    """Who does the model's work on the invented story, by `BEACON_MOCK_EMBER`.

    Raises TellingConfigError for a setting that is neither `stand-in` nor `live`."""
    setting = env.get(MOCK_EMBER_VARIABLE, "stand-in")
    teller = _SETTINGS.get(setting)
    if teller is None:
        raise TellingConfigError(
            f"{MOCK_EMBER_VARIABLE} is stand-in, which is the default, or live."
        )
    return teller


def scope_of(env: Mapping[str, str], label: str) -> SpendScope:
    """The spending scope one part of Ember's work on the invented story is paid
    under: its share of `BEACON_MOCK_EMBER_BUDGET_USD`, rounded down.

    Raises TellingConfigError when the budget is not a dollar amount a scope takes."""
    try:
        budget = parse_usd(env.get(BUDGET_VARIABLE, ""))
        return SpendScope(label, budget * SCOPES[label] // 100)
    except ValueError:
        # Not the error itself, which would be chained to a setting.
        pass
    raise TellingConfigError(
        f"{MOCK_EMBER_VARIABLE}=live needs {BUDGET_VARIABLE}: the most, in dollars, that "
        "Muse Ember's work on the invented story may cost over every telling, such as "
        "0.10. It has no default, because every call it hears, every notice it reads, "
        "every story, and every tap is paid for."
    )


def check(env: Mapping[str, str], make: PaidClient | None = None) -> None:
    """Refuse a telling by Muse Ember that has no live Ember to ask, **before
    anything is made**: a client, even one that only replays, makes the ledger's
    file, and a store that was opened for a telling is that telling's from then on.

    Raises TellingConfigError when `REPLAY` is not 0 or there is no key, and when
    `BEACON_MOCK_EMBER_LEDGER` names a ledger that is not the one under the data
    directory (`pinned_ledger`); EmberConfigError for a relative `BEACON_DATA_DIR`.
    A test that hands in what makes its clients (`make`) is asked only for the
    ledger."""
    if make is None and (env.get("REPLAY", "1") != "0" or not env.get("MODEL_API_KEY")):
        raise TellingConfigError(_NO_LIVE_EMBER)
    pinned_ledger(env)


def pinned_ledger(env: Mapping[str, str]) -> str | None:
    """The ledger file `BEACON_MOCK_EMBER_LEDGER` names, seen to be the one under
    the data directory, or None when the setting is not there. The ledger is read
    through a connection that cannot write, and nothing is made.

    Raises TellingConfigError when the setting is no ledger file's name, and when
    the ledger that is there is another file or no ledger is there;
    EmberConfigError for a relative `BEACON_DATA_DIR`."""
    if LEDGER_VARIABLE not in env:
        return None
    named = env[LEDGER_VARIABLE]
    if FILE_NAME.fullmatch(named) is None:
        raise TellingConfigError(
            f"{LEDGER_VARIABLE} is the name of the ledger file that is paid from, as "
            "`python -m beacon.telling` prints it."
        )
    if kept_file(Storage.under(default_data_dir(env)).ledger_db) != named:
        raise TellingConfigError(OTHER_LEDGER)
    return named


OTHER_LEDGER = (
    f"{LEDGER_VARIABLE} names the ledger file this telling is paid from, and what is "
    "under the data directory (ember/ledger.sqlite3) is not that file: it is another "
    "ledger, a copy that was put in its place, or no ledger. Nothing was paid for, "
    "and no ledger was begun: one begun anew, or an older copy, would count less than "
    "was spent. Find out what became of the ledger before a server is started here, "
    "and compare Meta's Usage page with what the ledger held."
)
NO_LEDGER = (
    f"{MOCK_EMBER_VARIABLE}=live is to go on paying from the ledger that was paid from "
    "before, and no ledger that was begun is at ember/ledger.sqlite3 under the data "
    "directory: nothing is there, or what is there is empty, a link, or no ledger. A "
    "ledger begun anew would count nothing of what was spent."
)


def preflight(env: Mapping[str, str]) -> str | None:
    """What can be seen, before anything is stopped, moved, or made, of whether a
    server can be started on `env` to tell the invented story: who tells it, and,
    for Muse Ember, that paid requests are asked for by name (`REPLAY=0`), that the
    budget is one every scope takes, and that a ledger that was begun is under the
    data directory, which is the one `BEACON_MOCK_EMBER_LEDGER` names when that is
    set. Answers that ledger file's name, to be handed to the server as
    `BEACON_MOCK_EMBER_LEDGER`, or None for a stand-in's telling.

    The spending settings in `env` (`EMBER_SPENT_BEFORE_USD`, which has to be
    there, and `EMBER_STOP_USD`) are read as the client reads them, so that one it
    would refuse is refused here. **That is a look at what `env` holds, and not at
    what the server will keep**: a client reads both from its own process's
    environment, and the key too, which this does not look for. It makes nothing
    and asks nobody.

    Raises TellingConfigError when it cannot, and EmberConfigError for a relative
    `BEACON_DATA_DIR` and for a spending setting that cannot be read."""
    if teller_of(env) == "stand_in":
        return None
    if env.get("REPLAY") != "0":
        raise TellingConfigError(
            f"{MOCK_EMBER_VARIABLE}=live makes paid requests, which REPLAY=0 asks for. "
            "Set REPLAY=0 beside it."
        )
    for label in SCOPES:
        scope_of(env, label)
    if read_stop_settings(env)[1] is None:
        raise TellingConfigError(
            f"{MOCK_EMBER_VARIABLE}=live needs {SPENT_BEFORE_VARIABLE}: what Meta's Usage "
            "page showed as spent before the ledger began. It has no default."
        )
    # A ledger that is named already is that one, or this is refused: never
    # replaced by the one that happens to be there.
    named = pinned_ledger(env)
    if named is not None:
        return named
    there = kept_file(Storage.under(default_data_dir(env)).ledger_db)
    if there is None:
        raise TellingConfigError(NO_LEDGER)
    return there


def paying(env: Mapping[str, str], label: str, make: PaidClient | None = None) -> EmberClient:
    """A live Ember client that pays under the scope `label`, one of `SCOPES`.

    Raises TellingConfigError when the budget is not one, there is no live Ember,
    or the ledger is not the one `BEACON_MOCK_EMBER_LEDGER` names, and
    EmberConfigError as `EmberClient.from_env` does."""
    scope = scope_of(env, label)
    check(env, make)
    ember = EmberClient.from_env(env, scope=scope) if make is None else make(scope)
    refusal = None
    if ember.mode != "live":
        refusal = _NO_LIVE_EMBER
    elif LEDGER_VARIABLE in env and ember.ledger_file() != env[LEDGER_VARIABLE]:
        # The file the client is open on, which is the one it pays in from here on.
        refusal = OTHER_LEDGER
    if refusal is not None:
        ember.close()
        raise TellingConfigError(refusal)
    return ember


_NO_LIVE_EMBER = (
    f"{MOCK_EMBER_VARIABLE}=live needs a live Ember: set REPLAY=0, MODEL_API_KEY, and "
    "EMBER_SPENT_BEFORE_USD. Its calls are sensitive ones and cannot be replayed."
)


def other_teller(began: Teller, now: Teller) -> str:
    """What is said to whoever starts a server on a mock store the other worked on."""
    return OTHER_TELLER.format(
        began=_AS[began], now=_WITH[now], variable=MOCK_EMBER_VARIABLE, how=_HOW[began]
    )


def main(env: Mapping[str, str], out: Callable[[str], None] = print) -> int:
    """`python -m beacon.telling`: look before a server is started to tell the
    invented story (`preflight`), and print the name of the ledger file a telling
    by Muse Ember would pay from, or nothing for a stand-in's. It makes nothing."""
    try:
        ledger = preflight(env)
    except (TellingConfigError, EmberConfigError) as error:
        print(str(error), file=sys.stderr)
        return 2
    if ledger is not None:
        out(ledger)
    return 0


if __name__ == "__main__":
    if sys.argv[1:]:
        print("python -m beacon.telling takes no arguments.", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(os.environ))
