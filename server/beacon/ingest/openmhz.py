"""Fire and EMS dispatch radio for San Francisco, from OpenMHz.

OpenMHz archives the calls of public-safety radio systems. Its API has no published
documentation; the shapes here are read from the source of the server it runs
(https://github.com/robotastic/trunk-server) and are unverified, because **Beacon has
never asked OpenMHz anything**: using its audio needs its permission, which Beacon
does not have yet. `beacon.ingest.setup` builds this poller for the real API only
when `BEACON_OPENMHZ_PERMITTED=1` says that permission has been given.

`GET https://api.openmhz.com/{system}/calls/newer?time=<ms>` answers with calls after
a moment, each with its talkgroup, its length, and the address of its audio. The
poller downloads a call's audio, has Ember hear it (`beacon.radio`), and stores one
row for the call. What follows from what the audio is:

- **Nothing a call said is stored as text, and its audio is not stored at all.** A
  row holds what `beacon.radio.Transmission` holds: tags from fixed lists, a hundred
  block, and units and streets that are on the lists the poller was given (`known`).
  With no list, a row names no unit and no street. The audio is held in memory, and
  only for as long as the call is being worked on.
- **A call is paid for once.** Before Ember is asked about a call, the call is
  claimed in the evidence store (`EvidenceStore.claim`), and a call that is claimed
  is never asked about again, by this process or another: not after a crash, not
  after its row could not be stored, not after an answer that was no form, and not
  when a listing names it twice. The claim is given up only when the request that
  costs money is known not to have been sent. The price of that rule is a call that
  is paid for and lost when the process dies between the answer and the row.
- **Only what is worth hearing is paid for.** The poller asks for the talkgroups it
  was given and no others, hears at most `MAX_CALLS_PER_POLL` calls a poll, and
  passes over a call shorter than `MIN_CALL_SECONDS` or longer than
  `MAX_CALL_SECONDS`, by the listing's word first and then by how long the audio
  really plays: every call is decoded before it is sent (`beacon.radio.audio`).
- **Trouble with Ember stops the poller, not the server.** When Ember refuses a call
  because of a spending stop, or fails on one, the poll ends there and the poller
  asks nothing for `HELD_INTERVAL_SECONDS`, counted from the failure and kept in the
  store, so that a restart does not shorten it. The same happens after
  `UNUSABLE_RUN_LIMIT` answers in a row that were no form, each of which was paid
  for; after that hold one more such answer holds it again. Calls that grew older
  than `MAX_AGE` in the meantime are passed over, so that lifting a stop does not buy
  a backlog. When the Ember ledger was replaced under the process, the poller stops
  for the life of the process: an operator has to compare Meta's Usage page with the
  ledger first.
- **A call that is listed late is still heard.** OpenMHz lists a call when its
  upload arrives, which is after the call was made, so each poll reads again from
  `OVERLAP` before the newest call it has finished with. The calls it has already
  taken are told apart by their ids and cost nothing. A call listed later than
  that is missed.
- **Shutting down admits nothing new.** `close` closes the poller's Ember client
  and its converter. A call that was being fetched, converted, or counted is
  dropped unpaid, and waits for the next run. A call that Ember's client had
  already admitted goes on: its paid request is sent, which can be a moment after
  `close` returns, and `drain` waits until its row is stored. Only a `drain` that
  answers True says that nothing more is sent.

The listing is read in pages (`_scan`), and what the poller concludes from them
rests on two assumptions about an API nobody has asked yet, which it keeps apart:

- **An answer holds the calls nearest after the moment asked about.** From that
  alone: how far a page reaches is the newest time among everything it lists,
  usable or not. Because a page can end in the middle of a moment that several
  calls share, that moment is asked for again, and calls later than it in the
  answer prove that all of its calls were shown. The listing has ended only when a
  page that asks for what is strictly after a moment comes back empty; a short
  page proves nothing.
- **An answer holds up to a fixed number of calls.** Nothing is concluded from
  this unless the poller is told the number (`page_size`), and for OpenMHz itself
  nobody knows it yet (`PAGE_SIZE`). It would settle one thing: whether the page
  of the listing's last moment, which holds only calls of that moment, holds all
  of them. Told the number, the poller takes a page with fewer calls to be whole.
  Not told, it cannot say that more calls of that moment are not hidden behind the
  page, and reports the poll as partial: every poll whose listing ends in a call,
  which for a busy system is every poll, until the number has been checked against
  a real answer. Guessing the number from the fullest answer seen was tried and
  dropped: an API whose answers shrink would hide a call and say nothing. A poll
  in which calls turned up after a page that should not have been full is partial
  as well.

A poll reads at most `SCAN_PAGES` pages from where the overlap begins, and when
that does not reach the end, `SCAN_PAGES` more from where it had got to before, so
a long overlap never keeps it from going forward. If OpenMHz answers with only the
newest calls instead, calls in between are missed in a busy minute: one of the
things to check with the first real answer.
"""

import contextlib
import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import urlsplit

from beacon.ember import (
    BudgetExceededError,
    EmberCallError,
    EmberClient,
    EmberClosedError,
    EmberError,
    InputTooLargeError,
    LedgerReplacedError,
    ReplayMissError,
)
from beacon.errors import raise_bare
from beacon.evidence import Confidence, EvidenceStore, EvidenceStoreError, NewEvidence, Origin
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.runner import MAX_WAIT_SECONDS, PollerHeldError, PollReport
from beacon.radio import AudioError, ExtractionError, Transcoder, Transmission, extract_transmission
from beacon.radio.audio import MAX_SECONDS as MAX_AUDIO_SECONDS
from beacon.radio.audio import container, plain_wav, wav_seconds
from beacon.radio.vocabulary import Known

SOURCE = "openmhz"
API = "https://api.openmhz.com"
# San Francisco's P25 system, by the short name OpenMHz gives it.
SAN_FRANCISCO = "sfp25"
# How many calls a full answer of OpenMHz's holds. Nobody has asked it yet, so
# nobody knows, and a poll of the real API says so (the module's docstring). Set
# it once a real answer has shown the number and that it does not change.
PAGE_SIZE: int | None = None

INTERVAL_SECONDS = 30.0
# How long the poller leaves Ember alone after Ember refused a call or failed on one.
HELD_INTERVAL_SECONDS = 600.0
# This many answers in a row that were no form, and the poller is held as well.
UNUSABLE_RUN_LIMIT = 3

# How far behind the newest call it has finished with a poll asks again, for the
# calls whose upload reached OpenMHz late. The first poll asks this far back from
# now, and not for the archive.
OVERLAP = timedelta(minutes=5)
# A call older than this is not heard: nobody is tapping about it any more.
MAX_AGE = timedelta(minutes=15)
MAX_CALLS_PER_POLL = 20
# The most pages of the listing one scan reads. A poll makes one scan, or two.
SCAN_PAGES = 6
# A call shorter than this is a click of the microphone.
MIN_CALL_SECONDS = 1.0
MAX_CALL_SECONDS = float(MAX_AUDIO_SECONDS)
# A decoder gives a clip a few hundredths of a second more or less than its listing.
_LENGTH_SLACK_SECONDS = 0.5

_MAX_LIST_BYTES = 2_000_000
# Two minutes of M4A is a few hundred kilobytes, and of WAV at 16 kHz, 3.8 MB.
_MAX_AUDIO_BYTES = 4_000_000
_SYSTEM = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# The refusals after which the request that costs money is known not to have been
# sent: a spending stop, a client that only replays, and a client that was closed.
# A replaced ledger is a spending stop by its class and is not among them: it is
# also raised after a request that was sent could not be settled.
_NOT_SENT = (BudgetExceededError, ReplayMissError, EmberClosedError)
# How well Ember says it heard a call decides how far the row is trusted. Ember's
# own judgment of that is not measured yet, so nothing heard is more than `medium`.
_CONFIDENCE: dict[str, Confidence] = {"clear": "medium", "partial": "low", "unintelligible": "low"}

_HELD = "The radio poller is leaving Ember alone for a while, after Ember refused or failed."
_UNUSABLE = (
    "The radio poller is leaving Ember alone for a while: its answers about calls were "
    "not filled-in forms, several times in a row."
)
_LEDGER_LOST = (
    "The radio poller has stopped: the Ember ledger was replaced while it ran, so what "
    "was spent is not known from it. Compare Meta's Usage page with the ledger, then "
    "start Beacon again."
)

logger = logging.getLogger("beacon.ingest")


@dataclass(frozen=True)
class Call:
    """One call in an OpenMHz answer."""

    ref: str
    talkgroup: int
    time: datetime
    seconds: float
    # Where its audio is.
    url: str
    # Whether a radio's emergency button was pressed during it.
    emergency: bool

    @property
    def time_ms(self) -> int:
        return round(self.time.timestamp() * 1000)


@dataclass
class _Progress:
    """What the poller remembers between polls, in the evidence store."""

    # The time of the newest call it has finished with.
    after_ms: int
    # Until when it leaves Ember alone, by the wall clock.
    held_until: datetime | None = None
    # How many of Ember's last answers in a row were no form.
    unusable_run: int = 0

    def as_state(self) -> dict[str, Any]:
        return {
            "after_ms": self.after_ms,
            "held_until": None if self.held_until is None else self.held_until.isoformat(),
            "unusable_run": self.unusable_run,
        }


class OpenMhzRadio:
    name = SOURCE
    interval_seconds = INTERVAL_SECONDS

    def __init__(
        self,
        http: SourceHttp,
        ember: EmberClient,
        *,
        talkgroups: Collection[int],
        known: Known,
        ems_talkgroups: Collection[int] = (),
        system: str = SAN_FRANCISCO,
        media_hosts: Collection[str] = (),
        page_size: int | None = None,
        transcoder: Transcoder | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`talkgroups` are the channels to hear, by OpenMHz's numbers, and
        `ems_talkgroups` the ones among them that carry EMS traffic only: every call
        on one is kept as a medical response, whatever Ember makes of it. `known`
        lists the units and the streets a row may name. `media_hosts` are hosts a
        call's audio may be on besides OpenMHz's own. `page_size` is how many calls a
        full answer of the listing holds, for a source of which that is known; left
        out, a poll whose listing ends in a call is reported as partial. `transcoder` decodes a call
        that is M4A or MP3; without one only a WAV call is heard, and any other is
        counted as unreadable. `wall_clock` is the time a hold is counted by, which
        is the real time even while the mock scenario is played on a clock of its own.

        `ember` is closed with the poller (`close`), so it is a client of the
        poller's own."""
        if _SYSTEM.fullmatch(system) is None:
            raise ValueError("An OpenMHz system is named by its short name, such as sfp25.")
        if not talkgroups or not all(_is_talkgroup(number) for number in talkgroups):
            raise ValueError("The radio poller needs the numbers of the talkgroups to hear.")
        if not set(ems_talkgroups) <= set(talkgroups):
            raise ValueError("An EMS talkgroup is one of the talkgroups that are heard.")
        if page_size is not None and (isinstance(page_size, bool) or page_size < 1):
            raise ValueError("A full answer of the listing holds at least one call.")
        if ember.mode != ("live" if http.origin == "live" else "fake"):
            # Real radio is not handed to a stand-in for Ember, whose answer would
            # be an invention about a real call, and a real Ember is not paid to
            # hear a stand-in's tones. A client that only replays can hear neither:
            # a radio call is a sensitive one, and is never recorded.
            raise ValueError("Real radio is heard by a live Ember, and a stand-in's by a fake one.")
        if ember.retries:
            # Each try is a paid request, and a call is claimed for one.
            raise ValueError("The radio poller's Ember client makes each call once: no pacer.")
        self._http = http
        self._ember = ember
        self._system = system
        self._talkgroups = frozenset(talkgroups)
        self._ems_talkgroups = frozenset(ems_talkgroups)
        self._known = known
        self._media_hosts = frozenset(host.lower() for host in media_hosts)
        self._page_size = page_size
        self._transcoder = transcoder
        self._wall_clock = (lambda: datetime.now(UTC)) if wall_clock is None else wall_clock
        # Set when the Ember ledger was replaced under this process. Nothing is
        # paid for after that, until an operator has looked and started Beacon again.
        self._ledger_lost = False
        # How many claimed calls are being worked on, which is one or none: from
        # the claim until the call's row is stored or the call is given up.
        self._working = threading.Condition()
        self._claimed = 0

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is where its client's answers
        come from and cannot be set."""
        return self._http.origin

    def close(self) -> None:
        """Admit no more paid work, and call off a conversion that is running. A
        call the Ember client had already admitted still sends its paid request
        (`EmberClient.close`); `drain` waits for it."""
        self._ember.close()
        if self._transcoder is not None:
            self._transcoder.close()

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the call that is being worked on under
        a claim to be finished with, and say whether it has been: Ember has
        answered, and the row is stored. A shutdown waits for this before it closes
        the store, so that an answer that was paid for is not thrown away."""
        with self._working:
            return self._working.wait_for(lambda: self._claimed == 0, timeout)

    @contextlib.contextmanager
    def _claim_held(self) -> Iterator[None]:
        with self._working:
            self._claimed += 1
        try:
            yield
        finally:
            with self._working:
                self._claimed -= 1
                self._working.notify_all()

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        if self._ledger_lost:
            raise_bare(PollerHeldError(_LEDGER_LOST, MAX_WAIT_SECONDS))
        saved = store.state(self.name)
        progress = _progress(saved, now)
        if progress.held_until is not None:
            left = (progress.held_until - self._wall_clock()).total_seconds()
            if left > 0:
                raise_bare(PollerHeldError(_HELD, left))
            progress.held_until = None
        listing = self._listing(progress, now, first=not saved)
        calls = [call for call in listing.calls.values() if call.time_ms <= _ms(now)]
        unreadable = listing.unreadable
        taken = store.taken(SOURCE, [call.ref for call in calls])
        # Oldest first, whichever way the answer was ordered. A call that is too
        # short or too long by its listing is passed over here, and takes no place
        # among the calls a poll may hear.
        waiting = sorted(
            (
                call
                for call in calls
                if call.ref not in taken and MIN_CALL_SECONDS <= call.seconds <= MAX_CALL_SECONDS
            ),
            key=lambda call: (call.time_ms, call.ref),
        )
        added = 0
        for call in waiting[:MAX_CALLS_PER_POLL]:
            # Free, and done before the claim: a call whose audio cannot be
            # fetched just now is not claimed, and waits for the next poll.
            audio = self._audio(call)
            if not store.claim(SOURCE, call.ref, now=now):
                # Another process took it between the listing and now.
                continue
            # From here to the row, a shutdown waits (`drain`). A shutdown that
            # falls between the claim and here finds a closed Ember, and at worst
            # the claim cannot be given up: the call is lost, and nothing was paid.
            with self._claim_held():
                heard = None
                if audio is not None:
                    heard = self._hear(store, call, audio, progress, now)
                progress.after_ms = max(progress.after_ms, call.time_ms)
                if heard is None:
                    unreadable += 1
                    continue
                # The row and what the poller remembers are saved together.
                state = (self.name, progress.as_state())
                added += len(store.add([self._row(call, heard)], state=state, now=now))
        more_waiting = len(waiting) > MAX_CALLS_PER_POLL
        if not more_waiting:
            # Everything that was read is finished with, the calls passed over
            # included, as far as the listing was read forward and no later than now.
            progress.after_ms = max(progress.after_ms, min(listing.reached_ms, _ms(now)))
        store.add([], state=(self.name, progress.as_state()), now=now)
        return PollReport(
            added=added, unreadable=unreadable, partial=more_waiting or not listing.complete
        )

    def _listing(self, progress: _Progress, now: datetime, *, first: bool) -> "_Listing":
        """What OpenMHz lists from `OVERLAP` before the newest call the poller has
        finished with. The very first poll, which has nothing behind it to read
        again, begins at that moment, and nothing older than `MAX_AGE` is asked for."""
        after_ms = progress.after_ms
        begin_ms = after_ms if first else max(after_ms - _ms_of(OVERLAP), _ms(now - MAX_AGE))
        listing = _Listing(reached_ms=after_ms)
        whole, reached_ms = self._scan(listing, begin_ms - 1)
        if not whole:
            # The scan ran out of pages. When it stopped short of where the poller
            # had got to before, the rest of the overlap is not read this time, and
            # the poll goes on from where it had got to: it always moves forward.
            listing.complete = reached_ms >= after_ms
            # From that moment itself, whose calls a page may have cut short.
            whole, reached_ms = self._scan(listing, max(reached_ms, after_ms) - 1)
        listing.complete = listing.complete and whole and not listing.cut
        listing.reached_ms = max(after_ms, reached_ms)
        return listing

    def _scan(self, listing: "_Listing", since_ms: int) -> tuple[bool, int]:
        """Read up to `SCAN_PAGES` pages of the calls after `since_ms` into
        `listing`. Returns whether the listing was read to its end, and the moment
        up to which everything it lists was read. The module's docstring says what
        is concluded from a page, and on which assumption."""
        # Everything the listing has up to here was shown, or is flagged as cut.
        reached_ms = since_ms
        # A moment the last page reached, whose calls the next page asks for again.
        boundary_ms: int | None = None
        # How many calls the page held that asked for a moment again and was shown
        # no later call. The next page asks what is strictly after that moment.
        tied: int | None = None
        for _ in range(SCAN_PAGES):
            asked_ms = reached_ms if boundary_ms is None else boundary_ms - 1
            items = self._page(asked_ms)
            listing.take(items, asked_ms)
            newest_ms = max(
                (item.time_ms for item in items if item.time_ms is not None), default=None
            )
            if boundary_ms is not None:
                if newest_ms is not None and newest_ms > boundary_ms:
                    # Later calls follow, so every call of that moment was shown.
                    boundary_ms = newest_ms
                else:
                    reached_ms, boundary_ms, tied = boundary_ms, None, len(items)
                continue
            if not items:
                # Nothing is after that moment: the listing ends here. The page of
                # the moment itself may have been full, with more calls behind it.
                listing.cut = listing.cut or self._may_be_full(tied)
                return True, reached_ms
            if newest_ms is None or newest_ms <= asked_ms:
                # Items, and none that says it is later: no telling how far it reaches.
                return False, reached_ms
            # Calls follow a page that showed only one moment. Either that page
            # was full, and more of the moment's calls may be hidden behind it, or
            # these were listed since. Nothing here can tell which.
            listing.cut = listing.cut or tied is not None
            boundary_ms, tied = newest_ms, None
        # Out of pages. A moment that was not asked for again is not read yet.
        return False, reached_ms if boundary_ms is None else boundary_ms - 1

    def _may_be_full(self, tied: int | None) -> bool:
        """Whether a page that held `tied` calls, all of one moment, can have been
        as full as an answer gets, with more calls of that moment behind it. An
        empty page hides nothing. Of any other, only a known `page_size` can say."""
        if not tied:
            return False
        return self._page_size is None or tied >= self._page_size

    def _page(self, since_ms: int) -> "list[Listed]":
        """One answer: what OpenMHz lists after a moment, on the talkgroups heard."""
        body = fetch(
            self._http,
            "GET",
            f"{API}/{self._system}/calls/newer",
            source=SOURCE,
            max_bytes=_MAX_LIST_BYTES,
            params={
                "time": str(since_ms),
                "filter-type": "talkgroup",
                "filter-code": ",".join(str(number) for number in sorted(self._talkgroups)),
            },
        )
        return parse_items(read_json(body, SOURCE), self._talkgroups, self._media_hosts)

    def _audio(self, call: Call) -> bytes | None:
        """A call's audio as a WAV file Beacon made, or None for a call that is
        passed over for good: its audio is not there, is not audio Beacon can
        decode, or does not play for as long as a call that is worth hearing.

        Raises SourceError when the audio could not be fetched for a reason that
        may pass, and ConversionStopped when the poller was closed meanwhile."""
        try:
            audio = fetch(self._http, "GET", call.url, source=SOURCE, max_bytes=_MAX_AUDIO_BYTES)
        except SourceError as error:
            gone = error.status is not None and 400 <= error.status < 500
            if not gone or error.status in (408, 429):
                raise
            return None
        kind = container(audio)
        try:
            if kind == "wav":
                wav = plain_wav(audio)
            elif kind is not None and self._transcoder is not None:
                wav = self._transcoder.to_wav(audio, kind)
            else:
                return None
        except AudioError:
            return None
        # How long it really plays, whatever the listing said: that is what is paid for.
        played = wav_seconds(wav)
        longest = MAX_CALL_SECONDS + _LENGTH_SLACK_SECONDS
        if played is None or not MIN_CALL_SECONDS - _LENGTH_SLACK_SECONDS <= played <= longest:
            return None
        return wav

    def _hear(
        self, store: EvidenceStore, call: Call, wav: bytes, progress: _Progress, now: datetime
    ) -> Transmission | None:
        """What Ember heard in a call that is claimed, or None when its answer came
        to nothing: it was no form, or the call is more than Ember takes. The call
        is finished with either way, and what the poller remembers is saved.

        Raises what Ember raised when it refused the call or failed on it, with the
        poller held, and PollerHeldError when answers that are no form have run on."""
        heard: Transmission | None = None
        unusable = False
        failure: EmberError | None = None
        try:
            heard = extract_transmission(
                self._ember,
                wav,
                "wav",
                known=self._known,
                ems_channel=call.talkgroup in self._ems_talkgroups,
            )
        except ExtractionError:
            # Ember answered, and was paid, and the answer is no form.
            unusable = True
        except InputTooLargeError:
            # Refused before anything was paid, and it would be refused again.
            pass
        except EmberError as error:
            failure = error
        if failure is not None:
            self._failed(store, call, progress, failure, now)
        if unusable:
            progress.unusable_run += 1
        elif heard is not None:
            progress.unusable_run = 0
        if progress.unusable_run >= UNUSABLE_RUN_LIMIT:
            # After the hold, one more answer of the kind holds the poller again.
            progress.unusable_run = UNUSABLE_RUN_LIMIT - 1
            progress.after_ms = max(progress.after_ms, call.time_ms)
            progress.held_until = self._wall_clock() + timedelta(seconds=HELD_INTERVAL_SECONDS)
            store.add([], state=(self.name, progress.as_state()), now=now)
            raise_bare(PollerHeldError(_UNUSABLE, HELD_INTERVAL_SECONDS))
        if heard is None:
            progress.after_ms = max(progress.after_ms, call.time_ms)
            store.add([], state=(self.name, progress.as_state()), now=now)
        return heard

    def _failed(
        self,
        store: EvidenceStore,
        call: Call,
        progress: _Progress,
        failure: EmberError,
        now: datetime,
    ) -> NoReturn:
        """End the poll after Ember refused a call or failed on it. The claim on
        the call is given up only when the paid request is known not to have been
        sent; otherwise the call may have been paid for, and is finished with."""
        not_sent = isinstance(failure, _NOT_SENT) and not isinstance(failure, LedgerReplacedError)
        if not_sent:
            # When the claim cannot be given up it stays, which loses the call and
            # pays for nothing.
            with contextlib.suppress(EvidenceStoreError):
                store.release(SOURCE, call.ref)
        else:
            progress.after_ms = max(progress.after_ms, call.time_ms)
        if isinstance(failure, EmberClosedError):
            # Beacon is shutting down. That is no news about Ember.
            raise_bare(failure)
        if isinstance(failure, LedgerReplacedError):
            self._ledger_lost = True
            logger.error("%s", _LEDGER_LOST)
        asked = failure.retry_after if isinstance(failure, EmberCallError) else None
        hold = max(HELD_INTERVAL_SECONDS, min(asked or 0.0, MAX_WAIT_SECONDS))
        # Counted from now, when Ember failed, and kept: a restart does not end it.
        progress.held_until = self._wall_clock() + timedelta(seconds=hold)
        with contextlib.suppress(EvidenceStoreError):
            store.add([], state=(self.name, progress.as_state()), now=now)
        # Nothing under it: not the error this was caught as, nor one the caller
        # of `poll` may be handling.
        raise_bare(failure)

    def _row(self, call: Call, heard: Transmission) -> NewEvidence:
        return NewEvidence(
            kind="radio",
            source=SOURCE,
            ref=call.ref,
            occurred_at=call.time,
            # In words, and no finer than a block. Nothing here knows where on the
            # map a block is, so a radio row has no point.
            place=heard.place,
            confidence=_CONFIDENCE[heard.clarity],
            origin=self.origin,
            model=heard.heard_by,
            data=heard.as_data()
            | {
                "system": self._system,
                "talkgroup": call.talkgroup,
                "seconds": call.seconds,
                "emergency": call.emergency,
            },
        )


@dataclass(frozen=True)
class Listed:
    """One item of an OpenMHz answer, whatever could be made of it."""

    # Names the item within a poll: its id, or a digest of it when it has none.
    key: str
    # When it says it was made, if it says so readably. How far a page reaches is
    # read from this, for an item Beacon cannot use as for one it can.
    time_ms: int | None
    # The call, when the item is one on a talkgroup that is heard and can be read.
    call: Call | None
    # Whether it counts as unreadable. An item on another talkgroup does not.
    unreadable: bool


@dataclass
class _Listing:
    """What one poll read of the listing."""

    # The moment the listing was read up to, going forward.
    reached_ms: int
    calls: dict[str, Call] = field(default_factory=dict)
    unreadable: int = 0
    _unreadable_keys: set[str] = field(default_factory=set)
    # Whether calls may be hidden behind a page that was full of one moment.
    cut: bool = False
    # Whether everything from the start of the overlap to the listing's end was
    # read, with nothing possibly hidden.
    complete: bool = True

    def take(self, items: "list[Listed]", asked_ms: int) -> None:
        """Add a page's calls. A page that asks for a moment again lists items
        again, and each call and each unreadable item is taken once."""
        for item in items:
            if item.call is not None:
                if item.call.time_ms > asked_ms:
                    self.calls.setdefault(item.call.ref, item.call)
            elif item.unreadable and item.key not in self._unreadable_keys:
                self._unreadable_keys.add(item.key)
                self.unreadable += 1


def parse_items(
    payload: Any, talkgroups: Collection[int], media_hosts: Collection[str] = ()
) -> list[Listed]:
    """Every item of a `/calls/newer` answer. An item on a talkgroup that is heard
    is a call when it can be read, and unreadable when it cannot. A call whose
    audio is somewhere other than on OpenMHz's own hosts or `media_hosts`, over
    HTTPS, is unreadable: its audio is not fetched from wherever an answer points.
    An item on another talkgroup is neither.

    Raises SourceError when the answer is not a calls object."""
    listed = payload.get("calls") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    items: list[Listed] = []
    for item in listed:
        fields = item if isinstance(item, dict) else {}
        talkgroup = fields.get("talkgroupNum")
        heard = not _is_talkgroup(talkgroup) or talkgroup in talkgroups
        call = None
        if heard and _is_talkgroup(talkgroup):
            call = _call(fields, talkgroup, media_hosts)
        ref = fields.get("_id")
        items.append(
            Listed(
                key=ref if isinstance(ref, str) and _CALL_ID.fullmatch(ref) else _digest(item),
                time_ms=_time_ms(fields.get("time")),
                call=call,
                unreadable=heard and call is None,
            )
        )
    return items


def parse_calls(
    payload: Any, talkgroups: Collection[int], media_hosts: Collection[str] = ()
) -> tuple[list[Call], int]:
    """The calls of a `/calls/newer` answer on the talkgroups that are heard, and
    how many of its items could not be read. See `parse_items`.

    Raises SourceError when the answer is not a calls object."""
    items = parse_items(payload, talkgroups, media_hosts)
    calls = [item.call for item in items if item.call is not None]
    return calls, sum(item.unreadable for item in items)


def _time_ms(time: Any) -> int | None:
    """When an item says it was made, or None when it does not say so readably."""
    if not isinstance(time, str) or len(time) > 40:
        return None
    try:
        moment = datetime.fromisoformat(time)
    except ValueError:
        return None
    return None if moment.utcoffset() is None else _ms(moment)


def _digest(item: Any) -> str:
    """A name for an item that has no usable id, the same each time it is listed."""
    try:
        text = json.dumps(item, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(item)
    return "#" + hashlib.sha256(text.encode()).hexdigest()[:32]


def _call(item: dict[str, Any], talkgroup: int, media_hosts: Collection[str]) -> Call | None:
    ref, time, seconds, url = item.get("_id"), item.get("time"), item.get("len"), item.get("url")
    if not isinstance(ref, str) or _CALL_ID.fullmatch(ref) is None:
        return None
    if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not 0 <= seconds < 1e6:
        return None
    if (
        not isinstance(time, str)
        or not isinstance(url, str)
        or not _is_audio_address(url, media_hosts)
    ):
        return None
    try:
        moment = datetime.fromisoformat(time)
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    return Call(
        ref=ref,
        talkgroup=talkgroup,
        time=moment.astimezone(UTC),
        seconds=float(seconds),
        url=url,
        emergency=item.get("emergency") is True,
    )


def _is_audio_address(url: str, media_hosts: Collection[str]) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        has_port = parts.port is not None
    except ValueError:
        return False
    if parts.scheme != "https" or has_port or parts.username is not None or len(url) > 500:
        return False
    return host == "openmhz.com" or host.endswith(".openmhz.com") or host in media_hosts


def _progress(saved: dict[str, Any], now: datetime) -> _Progress:
    """What the poller saved last. With nothing saved it begins `OVERLAP` ago, so
    the first poll hears the calls of the last few minutes and not the archive, and
    it never goes back further than `MAX_AGE`."""
    after_ms, held, run = saved.get("after_ms"), saved.get("held_until"), saved.get("unusable_run")
    if isinstance(after_ms, bool) or not isinstance(after_ms, int):
        after_ms = _ms(now - OVERLAP)
    held_until = None
    if isinstance(held, str):
        with contextlib.suppress(ValueError):
            held_until = datetime.fromisoformat(held)
        if held_until is not None and held_until.utcoffset() is None:
            held_until = None
    return _Progress(
        after_ms=max(after_ms, _ms(now - MAX_AGE)),
        held_until=held_until,
        unusable_run=run
        if isinstance(run, int) and not isinstance(run, bool) and 0 <= run <= UNUSABLE_RUN_LIMIT
        else 0,
    )


def _is_talkgroup(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000_000


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _ms_of(span: timedelta) -> int:
    return round(span.total_seconds() * 1000)
