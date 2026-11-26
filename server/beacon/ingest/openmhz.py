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
  upload arrives, which is after the call was made, so each poll asks again for the
  last `OVERLAP` before the newest call it has finished with. The calls it has
  already taken are told apart by their ids and cost nothing. A call listed later
  than that is missed.
- **Shutting down sends nothing new.** `close` closes the poller's Ember client and
  its converter. After it, no paid request is sent that was not already out, even
  for a call whose tokens were being counted. A call that was being fetched or
  converted is dropped unclaimed, and waits for the next run. A call whose paid
  request is out is waited for (`drain`), until its row is stored.

The listing is read in pages, each asking for the calls after the newest of the page
before, until a page brings nothing newer. That is right whether the API answers
with the calls nearest after the moment asked about or with all of them. If it
answers with only the newest ones, calls in between are missed in a busy minute:
one of the things to check with the first real answer.
"""

import contextlib
import logging
import re
import threading
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
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
# The most pages of the listing one poll reads.
MAX_PAGES = 10
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
        transcoder: Transcoder | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`talkgroups` are the channels to hear, by OpenMHz's numbers, and
        `ems_talkgroups` the ones among them that carry EMS traffic only: every call
        on one is kept as a medical response, whatever Ember makes of it. `known`
        lists the units and the streets a row may name. `media_hosts` are hosts a
        call's audio may be on besides OpenMHz's own. `transcoder` decodes a call
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
        if ember.mode != ("live" if http.origin == "live" else "fake"):
            # Real radio is not handed to a stand-in for Ember, whose answer would
            # be an invention about a real call, and a real Ember is not paid to
            # hear a stand-in's tones. A client that only replays can hear neither:
            # a radio call is a sensitive one, and is never recorded.
            raise ValueError("Real radio is heard by a live Ember, and a stand-in's by a fake one.")
        self._http = http
        self._ember = ember
        self._system = system
        self._talkgroups = frozenset(talkgroups)
        self._ems_talkgroups = frozenset(ems_talkgroups)
        self._known = known
        self._media_hosts = frozenset(host.lower() for host in media_hosts)
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
        """Start no more paid work. When this returns no paid request is sent that
        was not already out, and a conversion that was running is called off."""
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
        progress = _progress(store.state(self.name), now)
        if progress.held_until is not None:
            left = (progress.held_until - self._wall_clock()).total_seconds()
            if left > 0:
                raise_bare(PollerHeldError(_HELD, left))
            progress.held_until = None
        calls, unreadable, whole = self._listing(progress.after_ms, now)
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
        if len(waiting) <= MAX_CALLS_PER_POLL and calls:
            # Everything listed is finished with, the calls passed over included.
            progress.after_ms = max(progress.after_ms, max(call.time_ms for call in calls))
        store.add([], state=(self.name, progress.as_state()), now=now)
        return PollReport(
            added=added,
            unreadable=unreadable,
            partial=len(waiting) > MAX_CALLS_PER_POLL or not whole,
        )

    def _listing(self, after_ms: int, now: datetime) -> tuple[list[Call], int, bool]:
        """The calls OpenMHz lists from `OVERLAP` before `after_ms` up to now, how
        many of its items could not be read, and whether the listing was read to
        its end. A call the answer dates after now is left for the poll after that
        moment, and one older than `MAX_AGE` is not asked for."""
        since_ms = max(after_ms - _ms_of(OVERLAP), _ms(now - MAX_AGE)) - 1
        calls: dict[str, Call] = {}
        unreadable = 0
        whole = False
        for _ in range(MAX_PAGES):
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
            page, bad = parse_calls(read_json(body, SOURCE), self._talkgroups, self._media_hosts)
            unreadable += bad
            newest = max((call.time_ms for call in page), default=since_ms)
            for call in page:
                if call.time_ms > since_ms:
                    calls.setdefault(call.ref, call)
            if newest <= since_ms:
                # Nothing after where this page began: the listing is read.
                whole = True
                break
            since_ms = newest
        return [call for call in calls.values() if call.time_ms <= _ms(now)], unreadable, whole

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


def parse_calls(
    payload: Any, talkgroups: Collection[int], media_hosts: Collection[str] = ()
) -> tuple[list[Call], int]:
    """The calls of a `/calls/newer` answer on the talkgroups that are heard, and
    how many of its calls could not be read. A call on another talkgroup is left out
    and not counted. A call whose audio is somewhere other than on OpenMHz's own
    hosts or `media_hosts`, over HTTPS, is counted as unreadable: its audio is not
    fetched from wherever an answer points.

    Raises SourceError when the answer is not a calls object."""
    listed = payload.get("calls") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    calls: list[Call] = []
    unreadable = 0
    for item in listed:
        talkgroup = item.get("talkgroupNum") if isinstance(item, dict) else None
        if _is_talkgroup(talkgroup) and talkgroup not in talkgroups:
            continue
        call = _call(item, talkgroup, media_hosts) if _is_talkgroup(talkgroup) else None
        if call is None:
            unreadable += 1
        else:
            calls.append(call)
    return calls, unreadable


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
    """What the poller saved last. With nothing saved it begins now, which has the
    first poll ask for the last `OVERLAP`, and it never goes back further than
    `MAX_AGE`."""
    after_ms, held, run = saved.get("after_ms"), saved.get("held_until"), saved.get("unusable_run")
    if isinstance(after_ms, bool) or not isinstance(after_ms, int):
        after_ms = _ms(now)
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
