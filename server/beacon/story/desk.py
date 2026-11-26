"""The story desk: which incidents get a story written, when, and paid for once.

`merge_incident` costs money each time it is asked. The desk decides when it is:

- **An incident that is going on and has something to tell**: not a medical
  response, still open, with at least `MIN_LINES` lines Ember would be given.
- **Once for each basis.** The lines Ember would be given are named by a digest
  (`basis_of`), which is claimed in the stories file before Ember is asked. The same
  lines are never paid for twice, by this process or another.
- **No more often than `interval` for one incident**, and no more than
  `MAX_PER_PASS` incidents in one pass, so a busy hour is written about at a pace
  somebody set and not at the pace of the radio.
- **Under a budget of its own**: a live client pays under the `SpendScope` labelled
  `SCOPE`, and is refused without it.

What follows once Ember has been asked is `beacon.ingest.paid.PaidDesk`'s, the same
as for the pollers that pay: a claim is given up only when the paid request is known
not to have been sent, trouble with Ember holds the desk for a while, a replaced
ledger stops it for the life of the process, and shutting down admits nothing new
and waits for the answer that is out.
"""

import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from beacon.ember import EmberClient, EmberClosedError, EmberError, InputTooLargeError
from beacon.errors import raise_bare
from beacon.incident import Incident, Snapshot
from beacon.ingest.paid import Hold, PaidDesk, read_hold
from beacon.ingest.runner import PollerHeldError
from beacon.radio.vocabulary import MEDICAL_EVENT
from beacon.redact import Names
from beacon.story.merge import Draft, StoryError, basis_of, lines_of, merge_incident
from beacon.story.store import Stories, StoriesError, Story

# The label of the spending scope stories are paid under.
SCOPE = "story:merge"
# An incident with fewer lines than this says all it has to say in its lines.
MIN_LINES = 3
# How long after one story of an incident was asked for the next one may be.
INTERVAL = timedelta(minutes=2)
# How many stories one pass over the incidents asks for at most.
MAX_PER_PASS = 3

logger = logging.getLogger("beacon.story")


class StoryDesk:
    """Writes stories for the incidents of one engine, into one stories file."""

    def __init__(
        self,
        ember: EmberClient,
        stories: Stories,
        names: Names,
        *,
        interval: timedelta = INTERVAL,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`ember` is the desk's own client and is closed with it. A live one pays
        under the scope `SCOPE`. `wall_clock` is the time a hold is counted by."""
        self._paid = PaidDesk(
            ember, poller="story", items="incidents", scope=SCOPE, wall_clock=wall_clock
        )
        self._ember = ember
        self._stories = stories
        self._names = names
        self._interval = interval

    @property
    def stories(self) -> Stories:
        return self._stories

    def latest(self, incident: Incident) -> tuple[Story, bool] | None:
        """The story of an incident that was written last, and whether it was
        written from the lines the incident has now. None when it has none."""
        story = self._stories.latest(incident.id)
        return None if story is None else (story, story.basis == basis_of(incident))

    @staticmethod
    def wanted(incident: Incident) -> bool:
        """Whether an incident is one a story is written for."""
        if incident.category == MEDICAL_EVENT or not incident.open:
            return False
        return len(lines_of(incident)[0]) >= MIN_LINES

    def refresh(self, incident: Incident, now: datetime) -> Story | None:
        """Have the story of an incident written, when one is wanted and its lines
        have not been written about. Returns the story that was written, or None
        when none was: it is not wanted, its basis is already claimed, the
        incident's last story is younger than the interval, or Ember's answer came
        to nothing.

        Raises PollerHeldError while the desk is leaving Ember alone, what Ember
        raised when it refused or failed, and StoriesError when the stories file is
        closed."""
        if not self.wanted(incident):
            return None
        basis = basis_of(incident)
        latest = self._stories.latest(incident.id)
        if latest is not None and latest.basis == basis:
            return None
        hold = Hold(*read_hold(self._stories.state()))
        self._paid.check(hold)
        if not self._stories.claim(incident.id, basis, now, self._interval):
            return None
        # From here to the story's row, a shutdown waits (`drain`).
        with self._paid.working():
            draft: Draft | None = None
            answered = False
            failure: EmberError | None = None
            try:
                draft = merge_incident(self._ember, incident, self._names)
                answered = True
            except StoryError:
                # Ember answered, and was paid, and the answer is no form.
                answered = True
            except InputTooLargeError:
                # Refused before anything was paid, and it would be refused again:
                # the basis stays claimed.
                pass
            except EmberError as error:
                failure = error
            if failure is not None:
                self._failed(incident.id, basis, hold, failure)
            held = self._paid.count_answer(hold, usable=draft is not None) if answered else None
            story = None
            if draft is not None:
                # The story and what the desk remembers are saved together.
                story = self._stories.write(
                    incident.id,
                    basis,
                    draft,
                    stand_in=incident.stand_in,
                    now=now,
                    state=hold.hold_state(),
                )
            else:
                self._stories.save_state(hold.hold_state())
        if held is not None:
            raise_bare(held)
        return story

    def refresh_all(self, snapshot: Snapshot) -> int:
        """Have stories written for the incidents of a reading that want one, the
        ones heard from last first, at most `MAX_PER_PASS` of them, and say how
        many were written. It ends early, without raising, when the desk is held,
        when Ember refused or failed, and when the stories file is closed."""
        written = 0
        wanted = sorted(
            (incident for incident in snapshot.incidents if self.wanted(incident)),
            key=lambda incident: incident.updated_at,
            reverse=True,
        )
        for incident in wanted:
            if written >= MAX_PER_PASS:
                break
            failed: str | None = None
            try:
                if self.refresh(incident, snapshot.as_of) is not None:
                    written += 1
            except (PollerHeldError, EmberClosedError, StoriesError):
                # Held, or shutting down: neither is news.
                break
            except EmberError as error:
                # Only the kind of error: the desk is held now, and says so itself.
                failed = type(error).__name__
            if failed is not None:
                logger.warning("A story could not be written (%s).", failed)
                break
        return written

    def close(self) -> None:
        """Admit no more paid work. A story Ember had already been asked for is
        still paid for; `drain` waits for it."""
        self._paid.close()

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the story that is being written to be
        stored or given up, and say whether it has been."""
        return self._paid.drain(timeout)

    def stop(self, paid_timeout: float) -> bool:
        """Close the desk, wait for the story that is out, and close the stories
        file. Says whether nothing paid is still out. It blocks."""
        self.close()
        drained = self.drain(paid_timeout)
        self._stories.close()
        return drained

    def _failed(self, incident_id: str, basis: str, hold: Hold, failure: EmberError) -> None:
        """End a refresh after Ember refused an incident or failed on one. The
        claim is given up only when the paid request is known not to have been
        sent; otherwise it may have been paid for, and the basis is finished with."""
        if self._paid.not_sent(failure):
            with contextlib.suppress(StoriesError):
                self._stories.release(incident_id, basis)
        if isinstance(failure, EmberClosedError):
            # Beacon is shutting down. That is no news about Ember.
            raise_bare(failure)
        self._paid.hold_after(failure, hold)
        with contextlib.suppress(StoriesError):
            self._stories.save_state(hold.hold_state())
        raise_bare(failure)
