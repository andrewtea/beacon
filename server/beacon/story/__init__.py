"""Stories: what Ember writes about an incident, kept beside the incident engine.

    from beacon.story import open_stories

    desk = open_stories(os.environ, engine)  # in the server: request.app.state.stories
    desk.refresh_all(engine.snapshot())  # write the stories that are due
    found = desk.latest(incident)  # (story, current) or None

See `merge` for the Ember call and what is kept of its answer, `store` for where
stories are kept, `desk` for when one is paid for, and `stand_in` for the stand-in
that writes them with no key.
"""

from beacon.story.desk import INTERVAL, MAX_PER_PASS, MIN_LINES, SCOPE, StoryDesk
from beacon.story.merge import (
    LINE_KINDS,
    NOT_AN_ORDER,
    Claim,
    Draft,
    StoryError,
    basis_of,
    lines_of,
    merge_incident,
)
from beacon.story.setup import BUDGET_VARIABLE, StoryConfigError, open_stories
from beacon.story.stand_in import stand_in_writer
from beacon.story.store import Stories, StoriesError, Story

__all__ = [
    "BUDGET_VARIABLE",
    "INTERVAL",
    "LINE_KINDS",
    "MAX_PER_PASS",
    "MIN_LINES",
    "NOT_AN_ORDER",
    "SCOPE",
    "Claim",
    "Draft",
    "Stories",
    "StoriesError",
    "Story",
    "StoryConfigError",
    "StoryDesk",
    "StoryError",
    "basis_of",
    "lines_of",
    "merge_incident",
    "open_stories",
    "stand_in_writer",
]
