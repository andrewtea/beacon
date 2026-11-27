"""Fire and EMS dispatch radio, from recorded calls on this machine.

**Beacon asks nobody for radio audio.** OpenMHz, which archives public-safety radio,
was asked whether Beacon may use its API and said not to, so Beacon has no client
for it and requests nothing from it, its recordings included. Broadcastify's terms
require its license for any programmatic use, which Beacon does not have, so there
is no client for it either. What is left is a recording somebody with the right to
it has put on this machine: a directory of calls as
[trunk-recorder](https://github.com/robotastic/trunk-recorder) leaves them, which is
what a person who records a trunked system with a software-defined radio has.

    <directory>/<system>/<year>/<month>/<day>/<talkgroup>-<start>_<freq>-call_<n>.json
    <directory>/<system>/<year>/<month>/<day>/<talkgroup>-<start>_<freq>-call_<n>.wav

One call is two files: its audio (WAV, or M4A or MP3 where trunk-recorder was set to
compress) and a JSON file trunk-recorder writes when the call is over. Of the JSON
Beacon reads `talkgroup`, `start_time` (seconds since 1970), `call_length` (or
`stop_time`), `emergency`, and `encrypted`, and nothing else: not the radios that
transmitted (`srcList`), which are never kept. The layout and the fields are read
from trunk-recorder's source and documentation, and **no recording of real radio has
been read by this code yet**: the mock scenario's calls and the tests' are generated.

What a directory holds decides what its rows are (`Directory`, `RecordedRadio.origin`):

- **A directory with no mark is taken to be recordings of real radio.** Its rows are
  `live`, and only a live Ember hears them. `beacon.ingest.setup` builds the poller
  for one only when `BEACON_RECORDINGS_PERMITTED=1` says the operator has the right
  to use the recordings: made them, or has the permission of whoever did. A download
  from a service whose terms forbid it is not that.
- **A directory with the file `BEACON-GENERATED` at its top holds audio that was
  generated, and no radio.** Its rows are `mock` whoever hears them, so everything
  built on them says it is a stand-in's, and the live evidence store refuses them.
- **Anything else that lists calls is a stand-in**, as the mock scenario's is, whose
  calls are tones: `mock` rows, heard by a stand-in for Ember and never by a paid one.

The poller reads the calls on the talkgroups it was given, has Ember hear each
(`beacon.radio`), and stores one row for the call. What follows from what the audio is:

- **Nothing a call said is stored as text, and its audio is not copied anywhere.** A
  row holds what `beacon.radio.Transmission` holds: tags from fixed lists, a hundred
  block, and units and streets that are on the lists the poller was given (`known`).
  With no list, a row names no unit and no street. The audio is read into memory,
  and held only for as long as the call is being worked on. The recording itself is
  the operator's, in the operator's directory, and Beacon neither changes nor
  deletes it.
- **A call is paid for once.** Before Ember is asked about a call, the call is
  claimed in the evidence store (`EvidenceStore.claim`) under its system and file
  name, and a call that is claimed is never asked about again, by this process or
  another: not after a crash, not after its row could not be stored, not after an
  answer that was no form. So a run over a day of recordings that is stopped goes
  on, when it is started again on the same store, with the calls it had not
  claimed. The claim is given up only when the request that costs money is known
  not to have been sent. The price of that rule is a call that is paid for and lost
  when the process dies between the answer and the row.
- **Only what is worth hearing is paid for.** The poller hears the talkgroups it was
  given and no others, at most `calls_per_poll` calls a poll, and passes over a call
  that was encrypted, or is shorter than `MIN_CALL_SECONDS` or longer than
  `MAX_CALL_SECONDS`, by its JSON first and then by how long the audio really
  plays: every call is decoded before it is sent (`beacon.radio.audio`).
- **Trouble with Ember stops the poller, not the server.** When Ember refuses a call
  because of a spending stop, or fails on one, the poll ends there and the poller
  asks nothing for `HELD_INTERVAL_SECONDS`, counted from the failure and kept in the
  store, so that a restart does not shorten it. The same happens after
  `UNUSABLE_RUN_LIMIT` answers in a row that were no form, each of which was paid
  for; after that hold one more such answer holds it again. When the Ember ledger
  was replaced under the process, the poller stops for the life of the process: an
  operator has to compare Meta's Usage page with the ledger first.
- **A poller that follows a recorder hears what is new, and one that is given a
  backlog hears all of it.** Following (the default), the first poll begins
  `OVERLAP` ago and a call older than `MAX_AGE` is passed over, so that lifting a
  spending stop does not buy a backlog. With `backlog=True`, which is how a day of
  recordings is run through, every call in the directory up to now is heard, oldest
  first, however old.
- **A call whose files turn up late is still heard.** trunk-recorder writes a call's
  JSON when the call is over, and a copy from another machine can be later still,
  so each poll reads again from `OVERLAP` before the newest call it has finished
  with. The calls it has already taken are told apart by their names and cost
  nothing. A call that turns up later than that is missed by a poller that follows.
  A JSON file that cannot be read is counted and not claimed, so one that was being
  written is read whole at a later poll.
- **Shutting down admits nothing new.** `close` closes the poller's Ember client
  and its converter. A call that was being read, converted, or counted is dropped
  unpaid, and waits for the next run. A call that Ember's client had already
  admitted goes on: its paid request is sent, which can be a moment after `close`
  returns, and `drain` waits until its row is stored. Only a `drain` that answers
  True says that nothing more is sent.
"""

import contextlib
import errno
import json
import logging
import os
import re
import stat
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Protocol

from beacon.ember import (
    EmberClient,
    EmberClosedError,
    EmberError,
    InputTooLargeError,
)
from beacon.errors import raise_bare
from beacon.evidence import Confidence, EvidenceStore, EvidenceStoreError, NewEvidence, Origin
from beacon.ingest.http import SourceError
from beacon.ingest.paid import HELD_INTERVAL_SECONDS as HELD_INTERVAL_SECONDS
from beacon.ingest.paid import UNUSABLE_RUN_LIMIT as UNUSABLE_RUN_LIMIT
from beacon.ingest.paid import Hold, PaidDesk, read_hold
from beacon.ingest.runner import PollReport
from beacon.radio import (
    AudioError,
    ConversionUnavailable,
    ExtractionError,
    Transcoder,
    Transmission,
    extract_transmission,
)
from beacon.radio.audio import MAX_SECONDS as MAX_AUDIO_SECONDS
from beacon.radio.audio import container, plain_wav, wav_seconds
from beacon.radio.vocabulary import Known

SOURCE = "recordings"
# The label of the spending scope every paid radio call is paid under. A live Ember
# client that pays under any other is refused (`beacon.ingest.paid`).
SCOPE = "ingest:recordings"
# The file at the top of a directory whose audio was generated and is no radio.
GENERATED_MARK = "BEACON-GENERATED"

INTERVAL_SECONDS = 30.0

# How far behind the newest call it has finished with a poll reads again, for the
# calls whose files turned up late. The first poll of a poller that follows a
# recorder begins this far back from now, and not with the archive.
OVERLAP = timedelta(minutes=5)
# A poller that follows a recorder does not hear a call older than this: nobody is
# tapping about it any more.
MAX_AGE = timedelta(minutes=15)
MAX_CALLS_PER_POLL = 20
# The most calls one poll looks at. A poll with more to look at says it is partial,
# and the next goes on from where this one got to.
MAX_LISTED = 500
# A call shorter than this is a click of the microphone.
MIN_CALL_SECONDS = 1.0
MAX_CALL_SECONDS = float(MAX_AUDIO_SECONDS)
# A decoder gives a clip a few hundredths of a second more or less than its JSON says.
_LENGTH_SLACK_SECONDS = 0.5

# A call's JSON lists every radio that transmitted and every frequency used.
_MAX_DETAILS_BYTES = 500_000
# Two minutes of M4A is a few hundred kilobytes, and of WAV at 16 kHz, 3.8 MB.
_MAX_AUDIO_BYTES = 4_000_000
# The most entries of one day's directory that are looked at: a day of a busy
# system is some thousands of calls, with two or three files each.
_MAX_DAY_ENTRIES = 200_000
# The most directories of days one listing opens.
_MAX_DAYS = 800
# trunk-recorder's short name for a system, which is the directory its calls are in.
_SYSTEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
# `<talkgroup>-<start>_<freq>-call_<n>`, as trunk-recorder names a call's files.
_STEM = re.compile(r"(\d{1,8})-(\d{9,11})_(\d{1,12}(?:\.\d{1,6})?)-call_(\d{1,9})")
_NUMBER = re.compile(r"\d{1,4}")
# The kinds of audio file a call can have, in the order they are looked for.
_AUDIO_SUFFIXES = (".wav", ".m4a", ".mp3")
# How well Ember says it heard a call decides how far the row is trusted. Ember's
# own judgment of that is not measured yet, so nothing heard is more than `medium`.
_CONFIDENCE: dict[str, Confidence] = {"clear": "medium", "partial": "low", "unintelligible": "low"}

_UNREADABLE = "has a recorded call Beacon could not read just now"

logger = logging.getLogger("beacon.ingest")


@dataclass(frozen=True)
class Filed:
    """One call as a shelf lists it: where its files are, and what their name says."""

    system: str
    # The directories of its day under its system's, as they are named: the year,
    # the month, and the day, in the recorder's own time.
    day: tuple[str, str, str]
    # The name of its files, without what follows the last dot.
    stem: str
    talkgroup: int
    # When it began, in seconds since 1970, by its files' name.
    start_s: int

    @property
    def ref(self) -> str:
        """What names the call in the evidence store, with `SOURCE`."""
        return f"{self.system}/{self.stem}"


class Shelf(Protocol):
    """Where recorded calls are: a directory on this machine (`Directory`), or a
    stand-in for one, as the mock scenario and the tests have."""

    def listed(
        self, after_s: int, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        """The calls on `talkgroups` that began after `after_s` and no later than
        `upto_s`, oldest first and at most `limit` of them, and whether there were
        more than that or more than could be looked at."""
        ...

    def details(self, call: Filed) -> bytes | None:
        """The call's JSON file, or None when it is gone or is no such file."""
        ...

    def audio(self, call: Filed) -> bytes | None:
        """The call's audio file, or None when there is none Beacon reads."""
        ...


class Directory:
    """Recorded calls in a directory on this machine, as trunk-recorder leaves them.

    Nothing in it is changed. A file is read only when it is a regular file under
    the directory that is reached through no symbolic link, and only up to a size a
    call's file has."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Recorded calls are in a directory, named by its absolute path.")
        self._root = root
        # Read once: a directory does not change what it is while it is read.
        self.generated = _is_file(root / GENERATED_MARK)

    def listed(
        self, after_s: int, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        # A day's directory is named in the recorder's own time zone, which is not
        # in its name: a call is looked for a day to either side of its own.
        first = _day_of(after_s) - timedelta(days=1)
        last = _day_of(upto_s) + timedelta(days=1)
        calls: list[Filed] = []
        cut = False
        opened = 0
        try:
            for system in _directories(self._root, _SYSTEM):
                for year in _directories(self._root / system, _NUMBER):
                    if not first.year <= int(year) <= last.year:
                        continue
                    for month in _directories(self._root / system / year, _NUMBER):
                        for day in _directories(self._root / system / year / month, _NUMBER):
                            when = _date(year, month, day)
                            if when is None or not first <= when <= last:
                                continue
                            opened += 1
                            if opened > _MAX_DAYS:
                                cut = True
                                continue
                            where = self._root / system / year / month / day
                            found, whole = _calls_of(
                                where, system, (year, month, day), after_s, upto_s, talkgroups
                            )
                            calls += found
                            cut = cut or not whole
        except OSError:
            # Not the error itself, which names a path and would be chained.
            pass
        else:
            calls.sort(key=lambda call: (call.start_s, call.system, call.stem))
            return calls[:limit], cut or len(calls) > limit
        raise_bare(SourceError(SOURCE, "is a directory Beacon could not list just now"))

    def details(self, call: Filed) -> bytes | None:
        return _read(self._root, call, ".json", _MAX_DETAILS_BYTES)

    def audio(self, call: Filed) -> bytes | None:
        for suffix in _AUDIO_SUFFIXES:
            audio = _read(self._root, call, suffix, _MAX_AUDIO_BYTES)
            if audio is not None:
                return audio
        return None


@dataclass(frozen=True)
class Call:
    """One recorded call, by what its JSON file says of it."""

    ref: str
    system: str
    talkgroup: int
    time: datetime
    seconds: float
    # Whether a radio's emergency button was pressed during it.
    emergency: bool
    # Whether it was encrypted, so that its audio is noise.
    encrypted: bool
    filed: Filed

    @property
    def time_s(self) -> int:
        return self.filed.start_s


@dataclass
class _Progress(Hold):
    """What the poller remembers between polls, in the evidence store: how it
    stands with Ember (`Hold`), and how far it has got."""

    # The start, in seconds since 1970, of the newest call it has finished with.
    after_s: int = 0

    def as_state(self) -> dict[str, Any]:
        return {"after_s": self.after_s} | self.hold_state()


class RecordedRadio:
    name = SOURCE
    interval_seconds = INTERVAL_SECONDS

    def __init__(
        self,
        shelf: Shelf,
        ember: EmberClient,
        *,
        talkgroups: Collection[int],
        known: Known,
        ems_talkgroups: Collection[int] = (),
        backlog: bool = False,
        calls_per_poll: int = MAX_CALLS_PER_POLL,
        transcoder: Transcoder | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`shelf` is where the calls are. `talkgroups` are the channels to hear,
        by the recorder's numbers, and `ems_talkgroups` the ones among them that
        carry EMS traffic only: every call on one is kept as a medical response,
        whatever Ember makes of it. `known` lists the units and the streets a row
        may name. `backlog` has every call on the shelf heard, however old, where a
        poller otherwise follows a recorder and passes over what is older than
        `MAX_AGE`; `calls_per_poll` is the most calls one poll hears. `transcoder`
        decodes a call that is M4A or MP3; without one only a WAV call is heard, and
        any other is counted as unreadable. `wall_clock` is the time a hold is
        counted by, which is the real time even while the mock scenario is played on
        a clock of its own.

        `ember` is closed with the poller (`close`), so it is a client of the
        poller's own."""
        if not talkgroups or not all(_is_talkgroup(number) for number in talkgroups):
            raise ValueError("The radio poller needs the numbers of the talkgroups to hear.")
        if not set(ems_talkgroups) <= set(talkgroups):
            raise ValueError("An EMS talkgroup is one of the talkgroups that are heard.")
        if isinstance(calls_per_poll, bool) or not 1 <= calls_per_poll <= MAX_LISTED:
            raise ValueError(f"A poll hears from 1 to {MAX_LISTED} calls.")
        self._shelf = shelf
        self._origin: Origin = _origin_of(shelf)
        hearers = _HEARERS[_kind_of(shelf)]
        if ember.mode not in hearers:
            # Real radio is not handed to a stand-in for Ember, whose answer would
            # be an invention about a real call, and a real Ember is not paid to
            # hear a stand-in's tones. Generated speech can be heard by either: a
            # run of it is paid for on purpose. A client that only replays can hear
            # nothing: a radio call is a sensitive one, and is never recorded.
            raise ValueError(
                "Recorded radio is heard by a live Ember, a stand-in's calls by a fake one, "
                "and generated audio by either."
            )
        if ember.retries:
            # Each try is a paid request, and a call is claimed for one.
            raise ValueError("The radio poller's Ember client makes each call once: no pacer.")
        self._ember = ember
        self._talkgroups = frozenset(talkgroups)
        self._ems_talkgroups = frozenset(ems_talkgroups)
        self._known = known
        self._backlog = backlog
        self._calls_per_poll = calls_per_poll
        self._transcoder = transcoder
        # What every poller that pays Ember keeps to: `beacon.ingest.paid`.
        self._paid = PaidDesk(
            ember, poller="radio", items="calls", scope=SCOPE, wall_clock=wall_clock
        )

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is what its shelf is and cannot
        be set: `live` only for a directory on this machine that is not marked as
        generated."""
        return self._origin

    def close(self) -> None:
        """Admit no more paid work, and call off a conversion that is running. A
        call the Ember client had already admitted still sends its paid request
        (`EmberClient.close`); `drain` waits for it."""
        self._paid.close()
        if self._transcoder is not None:
            self._transcoder.close()

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the call that is being worked on under
        a claim to be finished with, and say whether it has been: Ember has
        answered, and the row is stored. A shutdown waits for this before it closes
        the store, so that an answer that was paid for is not thrown away."""
        return self._paid.drain(timeout)

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        saved = store.state(self.name)
        progress = self._progress(saved, now)
        self._paid.check(progress)
        now_s = int(now.timestamp())
        # From `OVERLAP` before the newest call the poller has finished with. The
        # very first poll, which has nothing behind it to read again, begins at
        # that moment itself.
        begin_s = progress.after_s if not saved else progress.after_s - _seconds(OVERLAP)
        if not self._backlog:
            begin_s = max(begin_s, now_s - _seconds(MAX_AGE))
        filed, more_listed = self._shelf.listed(begin_s, now_s, self._talkgroups, MAX_LISTED)
        if more_listed and max((call.start_s for call in filed), default=0) <= progress.after_s:
            # The overlap alone holds more calls than a poll looks at. The rest of
            # it is not read this time, and the poll goes on from where the poller
            # had got to, so that it always moves forward: from the second of the
            # newest call it finished with, which other calls can share.
            begin_s = progress.after_s - 1
            filed, more_listed = self._shelf.listed(begin_s, now_s, self._talkgroups, MAX_LISTED)
        # A stand-in may list what it was not asked for; a call is never heard
        # before its time or on a talkgroup that is not heard.
        filed = [
            call
            for call in filed
            if begin_s < call.start_s <= now_s and call.talkgroup in self._talkgroups
        ]
        taken = store.taken(SOURCE, [call.ref for call in filed])
        unreadable = 0
        waiting: list[Call] = []
        for one in filed:
            if one.ref in taken:
                continue
            call = self._details(one)
            if call is None:
                # Not claimed: a file that was still being written is read whole
                # at a later poll, for as long as the overlap reaches it.
                unreadable += 1
            elif not call.encrypted and MIN_CALL_SECONDS <= call.seconds <= MAX_CALL_SECONDS:
                # A call that is encrypted, or too short or too long by its JSON,
                # is passed over here, and takes no place among the calls a poll
                # may hear.
                waiting.append(call)
        added = 0
        for call in waiting[: self._calls_per_poll]:
            # Free, and done before the claim: a call whose audio cannot be read
            # just now is not claimed, and waits for the next poll.
            audio = self._audio(call)
            if not store.claim(SOURCE, call.ref, now=now):
                # Another process took it between the listing and now.
                continue
            # From here to the row, a shutdown waits (`drain`). A shutdown that
            # falls between the claim and here finds a closed Ember, and at worst
            # the claim cannot be given up: the call is lost, and nothing was paid.
            with self._paid.working():
                heard = None
                if audio is not None:
                    heard = self._hear(store, call, audio, progress, now)
                progress.after_s = max(progress.after_s, call.time_s)
                if heard is None:
                    unreadable += 1
                    continue
                # The row and what the poller remembers are saved together.
                state = (self.name, progress.as_state())
                added += len(store.add([self._row(call, heard)], state=state, now=now))
        more_waiting = len(waiting) > self._calls_per_poll
        if not more_waiting:
            # Everything that was looked at is finished with, the calls passed over
            # included: up to now, or up to the last call listed when there were
            # more than a poll looks at.
            reached_s = max((call.start_s for call in filed), default=begin_s)
            progress.after_s = max(progress.after_s, reached_s if more_listed else now_s)
        store.add([], state=(self.name, progress.as_state()), now=now)
        return PollReport(added=added, unreadable=unreadable, partial=more_waiting or more_listed)

    def _progress(self, saved: dict[str, Any], now: datetime) -> _Progress:
        """What the poller saved last. With nothing saved, a poller that follows a
        recorder begins `OVERLAP` ago, so the first poll hears the calls of the last
        few minutes and not the archive, and it never goes back further than
        `MAX_AGE`; one that is given a backlog begins with the oldest call there is."""
        now_s = int(now.timestamp())
        after_s = saved.get("after_s")
        if isinstance(after_s, bool) or not isinstance(after_s, int) or after_s < 0:
            after_s = 0 if self._backlog else now_s - _seconds(OVERLAP)
        if not self._backlog:
            after_s = max(after_s, now_s - _seconds(MAX_AGE))
        held_until, unusable_run = read_hold(saved)
        return _Progress(after_s=after_s, held_until=held_until, unusable_run=unusable_run)

    def _details(self, filed: Filed) -> Call | None:
        """A call by its JSON file, or None when the file is gone or cannot be read."""
        body = self._shelf.details(filed)
        return None if body is None else parse_details(body, filed)

    def _audio(self, call: Call) -> bytes | None:
        """A call's audio as a WAV file Beacon made, or None for a call that is
        passed over for good: its audio is not there, is not audio Beacon can
        decode, or does not play for as long as a call that is worth hearing.

        Raises SourceError when the audio could not be read, or converted on this
        host, for a reason that may pass, and ConversionStopped when the poller was
        closed meanwhile."""
        audio = self._shelf.audio(call.filed)
        if audio is None:
            return None
        kind = container(audio)
        unavailable = False
        try:
            if kind == "wav":
                wav = plain_wav(audio)
            elif kind is not None and self._transcoder is not None:
                wav = self._transcoder.to_wav(audio, kind)
            else:
                return None
        except AudioError:
            return None
        except ConversionUnavailable:
            unavailable = True
        if unavailable:
            # The host could not run the converter. That is nothing about the
            # call, which is not passed over: the poll ends, and is made again.
            raise_bare(SourceError(SOURCE, "calls could not be converted on this host just now"))
        # How long it really plays, whatever its JSON said: that is what is paid for.
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
        held = None
        if unusable or heard is not None:
            held = self._paid.count_answer(progress, usable=heard is not None)
        if held is not None:
            progress.after_s = max(progress.after_s, call.time_s)
            store.add([], state=(self.name, progress.as_state()), now=now)
            raise_bare(held)
        if heard is None:
            progress.after_s = max(progress.after_s, call.time_s)
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
        if self._paid.not_sent(failure):
            # When the claim cannot be given up it stays, which loses the call and
            # pays for nothing.
            with contextlib.suppress(EvidenceStoreError):
                store.release(SOURCE, call.ref)
        else:
            progress.after_s = max(progress.after_s, call.time_s)
        if isinstance(failure, EmberClosedError):
            # Beacon is shutting down. That is no news about Ember.
            raise_bare(failure)
        self._paid.hold_after(failure, progress)
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
                "system": call.system,
                "talkgroup": call.talkgroup,
                "seconds": call.seconds,
                "emergency": call.emergency,
            },
        )


def parse_details(body: bytes, filed: Filed) -> Call | None:
    """The call a JSON file of trunk-recorder's describes, or None when it is not
    one Beacon can read: no JSON object, no length, or a talkgroup or a start that
    is not what the file's name says. Nothing of it is kept but what `Call` holds."""
    try:
        said = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(said, dict):
        return None
    if said.get("talkgroup") != filed.talkgroup or said.get("start_time") != filed.start_s:
        return None
    if isinstance(said["talkgroup"], bool) or isinstance(said["start_time"], bool):
        return None
    seconds = said.get("call_length")
    if not _is_seconds(seconds):
        stop = said.get("stop_time")
        seconds = stop - filed.start_s if _is_seconds(stop) else None
    if not _is_seconds(seconds):
        return None
    emergency, encrypted = said.get("emergency", 0), said.get("encrypted", 0)
    if emergency not in (0, 1) or encrypted not in (0, 1):
        return None
    return Call(
        ref=filed.ref,
        system=filed.system,
        talkgroup=filed.talkgroup,
        time=datetime.fromtimestamp(filed.start_s, UTC),
        seconds=float(seconds),
        emergency=bool(emergency),
        encrypted=bool(encrypted),
        filed=filed,
    )


def filed_as(system: str, day: tuple[str, str, str], name: str) -> Filed | None:
    """The call a file's name is of, as trunk-recorder names the files of a call
    (`name` without what follows its last dot) in the directories of a system and
    a day, or None for any other names."""
    named = _STEM.fullmatch(name)
    if named is None or _SYSTEM.fullmatch(system) is None:
        return None
    if len(day) != 3 or not all(isinstance(part, str) and _NUMBER.fullmatch(part) for part in day):
        return None
    return Filed(system=system, day=day, stem=name, talkgroup=int(named[1]), start_s=int(named[2]))


# Who may hear the calls of each kind of shelf, by the Ember client's mode.
_HEARERS = {
    "recorded": ("live",),
    "generated": ("live", "fake"),
    "stand-in": ("fake",),
}


def _kind_of(shelf: Shelf) -> str:
    if not isinstance(shelf, Directory):
        return "stand-in"
    return "generated" if shelf.generated else "recorded"


def _origin_of(shelf: Shelf) -> Origin:
    """`live` for a directory on this machine that is not marked as generated, and
    `mock` for everything else: what a stand-in lists is not a recording however it
    is read, and there is no way to say otherwise."""
    return "live" if _kind_of(shelf) == "recorded" else "mock"


def _calls_of(
    where: Path,
    system: str,
    day: tuple[str, str, str],
    after_s: int,
    upto_s: int,
    talkgroups: Collection[int],
) -> tuple[list[Filed], bool]:
    """The calls of one day's directory that are asked for, by their JSON files'
    names, and whether every entry of the directory was looked at."""
    calls: list[Filed] = []
    looked = 0
    with os.scandir(where) as entries:
        for entry in entries:
            looked += 1
            if looked > _MAX_DAY_ENTRIES:
                return calls, False
            if not entry.name.endswith(".json"):
                continue
            filed = filed_as(system, day, entry.name[:-5])
            if filed is None or filed.talkgroup not in talkgroups:
                continue
            if not after_s < filed.start_s <= upto_s:
                continue
            if entry.is_file(follow_symlinks=False):
                calls.append(filed)
    return calls, True


def _directories(where: Path, named: re.Pattern[str]) -> list[str]:
    """The names of the directories in `where` that are named so, and are no
    symbolic links. Raises OSError when `where` cannot be listed."""
    with os.scandir(where) as entries:
        return sorted(
            entry.name
            for entry in entries
            if named.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
        )


# What opening a file fails with when it, or a directory on the way to it, is not
# there, is no directory, or is a symbolic link.
_NOT_THERE = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)


def _read(root: Path, call: Filed, suffix: str, most: int) -> bytes | None:
    """One of a call's files, or None when it is not there, is no regular file, is
    reached through a symbolic link, or is longer than `most`: a call with such a
    file is passed over. Each directory on the way is opened from the one above
    it, and none through a link, so the file read is under `root`.

    Raises SourceError when it is there and could not be read, which may pass."""
    if filed_as(call.system, call.day, call.stem) != call:
        # Not a name a listing of a directory gives.
        return None
    failed = False
    descriptor = -1
    body: bytes | None = None
    try:
        descriptor = _open_under(root, (call.system, *call.day), f"{call.stem}{suffix}")
        # Looked at before it is read: a directory opens, and is no file.
        if descriptor >= 0 and stat.S_ISREG(os.fstat(descriptor).st_mode):
            with os.fdopen(descriptor, "rb") as file:
                descriptor = -1
                body = file.read(most + 1)
    except OSError:
        # Not the error itself, which names a path: it is not chained, and what is
        # raised is raised outside this handler.
        failed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if failed:
        raise_bare(SourceError(SOURCE, _UNREADABLE))
    return body if body is not None and len(body) <= most else None


def _open_under(root: Path, directories: tuple[str, ...], name: str) -> int:
    """A descriptor for the file `name` in the directories under `root`, or -1 when
    it or one of them is not there or is a symbolic link. Raises OSError for any
    other failure."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    where = os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        for directory in directories:
            below = os.open(directory, flags | os.O_DIRECTORY, dir_fd=where)
            os.close(where)
            where = below
        return os.open(name, flags | os.O_NONBLOCK, dir_fd=where)
    except OSError as error:
        if error.errno in _NOT_THERE:
            return -1
        raise
    finally:
        os.close(where)


def _is_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _day_of(moment_s: int) -> date:
    try:
        return datetime.fromtimestamp(moment_s, UTC).date()
    except (OverflowError, OSError, ValueError):
        return date.max - timedelta(days=1) if moment_s > 0 else date.min + timedelta(days=1)


def _is_seconds(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and value == value
        and 0 <= value < 1e11
    )


def _is_talkgroup(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000_000


def _seconds(span: timedelta) -> int:
    return round(span.total_seconds())
