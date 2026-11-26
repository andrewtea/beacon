"""Opens the story desk for an incident engine, as the environment asks."""

import logging
from collections.abc import Mapping

from beacon.ember import EmberClient, SpendScope, Storage
from beacon.ember.client import default_data_dir
from beacon.ember.pricing import parse_usd
from beacon.incident import Engine
from beacon.story.desk import SCOPE, StoryDesk
from beacon.story.stand_in import stand_in_writer
from beacon.story.store import Stories

BUDGET_VARIABLE = "BEACON_STORY_BUDGET_USD"

logger = logging.getLogger("beacon.story")


class StoryConfigError(Exception):
    """The environment asks for a story desk that cannot be built."""


def open_stories(env: Mapping[str, str], engine: Engine) -> StoryDesk | None:
    """The desk that writes the stories of `engine`'s incidents, or None when
    stories are not written.

    On the mock store a stand-in for Ember writes them, which costs nothing, and
    they go to `data/story/mock.sqlite3`. On the live store they are written by a
    live Ember under a budget of their own, `BEACON_STORY_BUDGET_USD`, and go to
    `data/story/live.sqlite3`; with no budget set none is written, which the log
    says, because every story is paid for.

    Raises StoryConfigError when a budget is set that is not a dollar amount, or
    is set without a live Ember, and StoriesError when the file was begun as the
    other mode's."""
    data_dir = default_data_dir(env)
    if engine.mode == "mock":
        ember = EmberClient.fake(Storage.under(data_dir), stand_in_writer)
        return StoryDesk(ember, Stories(data_dir / "story" / "mock.sqlite3", "mock"))
    budget = env.get(BUDGET_VARIABLE, "").strip()
    if not budget:
        logger.warning(
            "No budget for stories (%s): no incident gets a written account, and "
            "each is shown as its lines.",
            BUDGET_VARIABLE,
        )
        return None
    try:
        scope = SpendScope(SCOPE, parse_usd(budget))
    except ValueError:
        scope = None
    if scope is None:
        raise StoryConfigError(
            f"{BUDGET_VARIABLE} is the most, in dollars, that writing stories may cost "
            "over every run, such as 0.25."
        )
    ember = EmberClient.from_env(env, scope=scope)
    if ember.mode != "live":
        raise StoryConfigError(
            f"{BUDGET_VARIABLE} is set, and a story is written by a live Ember and "
            "cannot be replayed: set REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD."
        )
    return StoryDesk(ember, Stories(data_dir / "story" / "live.sqlite3", "live"))
