"""Fire and EMS dispatch radio, from recorded calls on this machine.

**Beacon asks nobody for radio audio.** OpenMHz, which archives public-safety radio,
was asked whether Beacon may use its API and said not to, so Beacon has no client
for it and requests nothing from it (`beacon.ingest.http.fetch` refuses its
addresses for every poller). Beacon has no license from Broadcastify, and has no
client for it either. What is left is a recording somebody with the right to it has
put on this machine: a directory of calls as
[trunk-recorder](https://github.com/robotastic/trunk-recorder) leaves them, which is
what a person who records a trunked system with a software-defined radio has.

    <directory>/<system>/<year>/<month>/<day>/<talkgroup>-<start>_<freq>-call_<n>.json
    <directory>/<system>/<year>/<month>/<day>/<talkgroup>-<start>_<freq>-call_<n>.wav

One call is two files: a JSON file and its audio (WAV, or M4A or MP3 where
trunk-recorder was set to compress). What is known of them is read from
trunk-recorder's source (`call_concluder.cc`, v5.2.1), and **no recording of real
radio has been read by this code yet**: the mock scenario's calls and the tests' are
generated.

- **The name.** `<start>` is the second the call began, with its milliseconds after
  a dot in v5.2.1 (`1795809570.123`) and without in earlier versions; `<freq>` can
  be followed by a dot and a TDMA slot. A name says when the first transmission began, and the JSON
  can say a later start, because transmissions too short to keep are dropped after
  the name is made. So a call is found, ordered, and named in the evidence store by
  its files' name, and its time is the JSON's, which has to lie within
  `MAX_START_AFTER_NAME` of the name's.
- **The JSON.** Beacon reads `talkgroup`, `start_time` and `start_time_ms`,
  `call_length_ms` (or the rounded `call_length`), `emergency`, and `encrypted`, and
  nothing else: not the radios that transmitted (`srcList`), which are never kept.
- **The JSON is written before the audio.** So a call is ready only when its audio
  is there, decodes, and plays about as long as its JSON says (`_whole`). Until
  then it is not claimed and costs nothing, and is looked at again at a later poll.
  How close "about" has to be is a guess until a real recording has been read.
- **Talkgroup numbers are a system's own.** A reader is of one system, by the short
  name its recorder gives it, and reads no other system's directory: the same
  number is another channel there, and could be heard under the wrong EMS rule.

What a shelf of calls is decides what its rows are (`Directory`, `RecordedRadio.origin`):

- **A directory with no mark is taken to be recordings of real radio.** Its rows are
  `live`, and only a live Ember hears them. `beacon.ingest.setup` builds the poller
  for one only when `BEACON_RECORDINGS_PERMITTED=1` says the operator has the right
  to use the recordings.
- **A directory with the file `BEACON-GENERATED` at its top holds audio that was
  generated, and no radio.** Its rows are `mock` whoever hears them, so everything
  built on them says it is a stand-in's, and the live evidence store refuses them.
- **Anything else that lists calls is a stand-in**, as the mock scenario's is, whose
  calls are tones: `mock` rows, heard by a stand-in for Ember and never by a paid one.

A `Directory` is held open from when it is made: every listing and every file is
reached from that one descriptor, each directory opened from the one above it and
none through a symbolic link, so a directory put in its place later is not read.
Whether it is marked is looked at then, again at every listing, and again after
every file is read and before its bytes are handed on; a directory whose answer has
changed is not read any further.

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
- **Only what is ready and worth hearing is claimed.** The poller hears the
  talkgroups it was given and no others, and passes over a call that was
  encrypted, or is shorter than `MIN_CALL_SECONDS` or longer than
  `MAX_CALL_SECONDS` by its JSON. A call whose JSON cannot be read, or
  whose audio is not there, cannot be decoded, or does not play as long as its JSON
  says, is counted as unreadable and left unclaimed: its files may not all be
  written yet. Every call is decoded before it is sent (`beacon.radio.audio`).
- **Trouble with Ember stops the poller, not the server.** When Ember refuses a call
  because of a spending stop, or fails on one, the poll ends there and the poller
  asks nothing for `HELD_INTERVAL_SECONDS`, counted from the failure and kept in the
  store, so that a restart does not shorten it. The same happens after
  `UNUSABLE_RUN_LIMIT` answers in a row that were no form, each of which was paid
  for; after that hold one more such answer holds it again. When the Ember ledger
  was replaced under the process, the poller stops for the life of the process: an
  operator has to compare Meta's Usage page with the ledger first.
- **The listing is read in passes, a page at a time.** A page is the calls next
  after a place in the order of the second a call began, by its files' name, and
  then of that name: calls that share a second do not hide one another, and within
  a second they are in the order of their names, which is not the order of their
  milliseconds. A poll reads the audio of no more than `calls_per_poll` calls and
  no more than `MAX_PAGES` pages; one that has not reached the end of the listing
  by then is `partial`, and the next poll of this process goes on from that place,
  so every poll gets further. A day's directory with more entries than
  `_MAX_DAY_ENTRIES` fails the poll and is not read in part.
- **A pass is counted over all its polls** (`RecordedRadio.last_pass`, a
  `PassCount`). `PollReport.unreadable` is one poll's, and puts together two things
  a pass keeps apart: calls that were `unready`, which nobody claimed and a later
  pass looks at again, and calls that were `unusable`, which were claimed and
  Ember's answer came to nothing, and which are never asked about again. A poll
  that Ember's failure or the directory's ended adds nothing to the count: the
  pass goes on from before it, and what it stored or claimed is in the store.
- **A poller that follows a recorder hears what is new.** Its first pass begins
  `OVERLAP` ago and a call older than `MAX_AGE` is passed over, so that lifting a
  spending stop does not buy a backlog. Each later pass begins `OVERLAP` before the
  moment the one before ended, because a call's files turn up after the call: the
  calls it has taken are told apart by their names and cost nothing, and a call
  that was not ready is looked at again for as long as that reaches it. A call that
  becomes ready later than that is missed.
- **A poller that is given a backlog makes passes over everything there is**
  (`backlog=True`, which is how a day of recordings is run through): the oldest
  second first, however old. A call that is not ready is left unclaimed, and every
  later pass looks at it again: nothing gives such a call up, so a run over a
  backlog is over when a pass has ended with nothing `unready` in any of its polls
  (`last_pass.unready == 0`), or when its operator says so.
- **Shutting down admits nothing new.** `close` closes the poller's Ember client
  and its converter. A call that was being read, converted, or counted is dropped
  unpaid, and waits for the next run. A call that Ember's client had already
  admitted goes on: its paid request is sent, which can be a moment after `close`
  returns, and `drain` waits until its row is stored. Only a `drain` that answers
  True says that nothing more is sent.
"""

import contextlib
import errno
import heapq
import itertools
import json
import logging
import os
import re
import stat
from collections.abc import Callable, Collection, Iterator
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
# The most calls one page of a listing holds, and the most pages one poll reads. A
# poll that has not reached the end of the listing by then says it is partial, and
# the next goes on from where this one got to.
MAX_LISTED = 500
MAX_PAGES = 20
# A call shorter than this is a click of the microphone.
MIN_CALL_SECONDS = 1.0
MAX_CALL_SECONDS = float(MAX_AUDIO_SECONDS)
# How much later than its files' name a call's JSON may say it began: the name is
# of the first transmission, and the short ones are dropped after it is made.
MAX_START_AFTER_NAME = timedelta(hours=1)
# A decoder gives a clip a few hundredths of a second more or less than its length.
_LENGTH_SLACK_SECONDS = 0.5
# How far the audio's length may be from the JSON's and still be that call, whole:
# this much and a fiftieth of the length, and a second more when the JSON gives its
# length only in whole seconds. Nobody has compared the two on a real recording.
_ROUNDED_SLACK_SECONDS = 1.0

# A call's JSON lists every radio that transmitted and every frequency used.
_MAX_DETAILS_BYTES = 500_000
# Two minutes of M4A is a few hundred kilobytes, and of WAV at 16 kHz, 3.8 MB.
_MAX_AUDIO_BYTES = 4_000_000
# The most entries one day's directory may have: a day of a busy system is some
# thousands of calls, with two or three files each. One with more is not read.
_MAX_DAY_ENTRIES = 200_000
# trunk-recorder's short name for a system, which is the directory its calls are in.
_SYSTEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
# `<talkgroup>-<start>_<freq>-call_<n>`, as trunk-recorder names a call's files:
# the start in seconds, with milliseconds after a dot in v5.2.1 and without in
# earlier versions, and the frequency in hertz, with a TDMA slot after a dot where
# there is one.
_STEM = re.compile(r"(\d{1,8})-(\d{9,11})(?:\.\d{1,3})?_(\d{1,12}(?:\.\d{1,6})?)-call_(\d{1,9})")
_NUMBER = re.compile(r"\d{1,4}")
# The kinds of audio file a call can have, in the order they are looked for.
_AUDIO_SUFFIXES = (".wav", ".m4a", ".mp3")
# No recorder's own time is further from UTC than this, either way.
_ZONE_REACH = timedelta(hours=14)
# How well Ember says it heard a call decides how far the row is trusted. Ember's
# own judgment of that is not measured yet, so nothing heard is more than `medium`.
_CONFIDENCE: dict[str, Confidence] = {"clear": "medium", "partial": "low", "unintelligible": "low"}

_UNLISTED = "is a directory Beacon could not list just now"
_UNREADABLE = "has a recorded call Beacon could not read just now"
_TOO_MANY = "has a day with more files than Beacon lists, and is not read in part"
_CHANGED = (
    "is a directory whose mark as generated audio has changed since Beacon began to "
    "read it. Start Beacon again"
)
# Before every call there can be: the place a listing of everything begins after.
BEGINNING: "Place" = (0, "")

logger = logging.getLogger("beacon.ingest")

# Where a call stands in a listing: the second it began, by its files' name, and
# that name. A listing is in this order, and a page of one is asked for by the
# place it comes after.
Place = tuple[int, str]


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

    @property
    def place(self) -> Place:
        return (self.start_s, self.stem)


class Shelf(Protocol):
    """Where one system's recorded calls are: a directory on this machine
    (`Directory`), or a stand-in for one, as the mock scenario and the tests have."""

    def listed(
        self, after: Place, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        """The calls on `talkgroups` whose place is after `after` and that began no
        later than `upto_s`, in order and at most `limit` of them, and whether
        there are more.

        Raises SourceError when the calls cannot be listed, or not all of them."""
        ...

    def details(self, call: Filed) -> bytes | None:
        """The call's JSON file, or None when it is gone or is no such file."""
        ...

    def audio(self, call: Filed) -> bytes | None:
        """The call's audio file, or None when there is none Beacon reads."""
        ...


class Directory:
    """One system's recorded calls in a directory on this machine, as
    trunk-recorder leaves them.

    Nothing in it is changed. The directory is opened once, here, and held open:
    everything is read from that descriptor on, each directory from the one above
    it and none through a symbolic link, and a file only when it is a regular file
    and no longer than a call's file is."""

    def __init__(self, root: Path, system: str) -> None:
        """`root` is the directory trunk-recorder captures into, and `system` the
        short name of the one system whose calls are read.

        Raises ValueError when `root` is not the absolute path of a directory that
        can be opened (a symbolic link to one is not taken), when `system` is no
        short name, or when it cannot be told whether the directory is marked as
        generated audio."""
        if _SYSTEM.fullmatch(system) is None:
            raise ValueError("A recorder's system is named by its short name.")
        descriptor = -1
        if root.is_absolute():
            with contextlib.suppress(OSError):
                descriptor = os.open(root, _DIRECTORY)
        if descriptor < 0:
            raise ValueError("Recorded calls are in a directory, named by its absolute path.")
        self._root = descriptor
        self._system = system
        marked = _marked(descriptor)
        if marked is None:
            os.close(descriptor)
            self._root = -1
            raise ValueError(
                f"It cannot be told whether the directory is marked {GENERATED_MARK}: "
                "the mark is a regular file at its top, or is not there."
            )
        # Whether its audio was generated, and is no radio.
        self.generated = marked

    def __del__(self) -> None:
        if getattr(self, "_root", -1) >= 0:
            with contextlib.suppress(OSError):
                os.close(self._root)

    @property
    def system(self) -> str:
        return self._system

    def listed(
        self, after: Place, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        if _marked(self._root) is not self.generated:
            # What its rows are was decided by the mark, and is not decided again.
            raise_bare(SourceError(SOURCE, _CHANGED))
        problem = _UNLISTED
        try:
            return self._listed(after, upto_s, talkgroups, limit)
        except _TooMany:
            problem = _TOO_MANY
        except OSError:
            # Not the error itself, which names a path and would be chained.
            pass
        raise_bare(SourceError(SOURCE, problem))

    def _listed(
        self, after: Place, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        # A day's directory is named in the recorder's own time zone, which is not
        # in its name: a call is looked for a day to either side of its own.
        first = _day_of(after[0]) - timedelta(days=1)
        last = _day_of(upto_s) + timedelta(days=1)
        found: list[Filed] = []
        with _opened(self._root, self._system) as system:
            if system < 0:
                return [], False
            for when, names in _days(system, first, last):
                if len(found) > limit and _earliest_in(when) > found[-1].start_s:
                    # Nothing in this day's directory or a later one comes before
                    # the calls that are already more than a page.
                    break
                with _opened(system, *names) as day:
                    if day < 0:
                        continue
                    # Kept to a page and one more while the day is gone through,
                    # whatever a day holds.
                    of_day = _calls_of(day, self._system, names, after, upto_s, talkgroups)
                    found = heapq.nsmallest(
                        limit + 1, itertools.chain(found, of_day), key=lambda call: call.place
                    )
        return found[:limit], len(found) > limit

    def details(self, call: Filed) -> bytes | None:
        return self._read(call, ".json", _MAX_DETAILS_BYTES)

    def audio(self, call: Filed) -> bytes | None:
        for suffix in _AUDIO_SUFFIXES:
            audio = self._read(call, suffix, _MAX_AUDIO_BYTES)
            if audio is not None:
                return audio
        return None

    def _read(self, call: Filed, suffix: str, most: int) -> bytes | None:
        """One of a call's files, or None when it is not there, is no regular file,
        is reached through a symbolic link, or is longer than `most`.

        Raises SourceError when it is there and could not be read, which may pass,
        and when the directory's mark is not what it was when it was opened."""
        if call.system != self._system or filed_as(call.system, call.day, call.stem) != call:
            # Not a call a listing of this directory gives.
            return None
        failed = False
        body: bytes | None = None
        try:
            with _opened(self._root, call.system, *call.day) as day:
                if day >= 0:
                    body = _file(day, f"{call.stem}{suffix}", most)
        except OSError:
            # Not the error itself, which names a path: it is not chained, and what
            # is raised is raised outside this handler.
            failed = True
        if failed:
            raise_bare(SourceError(SOURCE, _UNREADABLE))
        if body is not None and _marked(self._root) is not self.generated:
            # Marked, or unmarked, since the listing: these bytes are not handed
            # on as what the directory was taken for when it was opened.
            raise_bare(SourceError(SOURCE, _CHANGED))
        return body


@dataclass(frozen=True)
class Call:
    """One recorded call, by what its JSON file says of it."""

    ref: str
    system: str
    talkgroup: int
    # When it began, by its JSON, which can be later than its files' name says.
    time: datetime
    seconds: float
    # Whether the JSON gave its length to the millisecond, or in whole seconds.
    exact: bool
    # Whether a radio's emergency button was pressed during it.
    emergency: bool
    # Whether it was encrypted, so that its audio is noise.
    encrypted: bool
    filed: Filed

    @property
    def time_s(self) -> int:
        """Where it stands in a listing, which is by its files' name."""
        return self.filed.start_s


@dataclass
class _Progress(Hold):
    """What the poller remembers between polls, in the evidence store: how it
    stands with Ember (`Hold`), and how far it has got."""

    # The start, in seconds since 1970 and by its files' name, of the newest call a
    # poller that follows a recorder has finished with.
    after_s: int = 0

    def as_state(self) -> dict[str, Any]:
        return {"after_s": self.after_s} | self.hold_state()


@dataclass(frozen=True)
class PassCount:
    """What one pass over the listing came to, over the polls of it that ended. A
    poll that was ended by a failure, Ember's or the directory's, adds nothing: the
    pass goes on from before that poll, and what it stored or claimed is in the
    evidence store."""

    # Calls Ember heard, each a row.
    stored: int = 0
    # Calls whose JSON or audio could not be used as it was. Nobody claimed them,
    # and a later pass looks at each again.
    unready: int = 0
    # Calls that were claimed, and Ember's answer came to nothing: it was no form,
    # or the call is more than Ember takes. They are never asked about again.
    unusable: int = 0


# What `RecordedRadio._take` answers for a call whose audio it read.
_AUDIO_READ = frozenset({"unready", "lost", "nothing", "stored"})


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
        """`shelf` is where one system's calls are. `talkgroups` are the channels
        to hear, by that system's numbers, and `ems_talkgroups` the ones among them
        that carry EMS traffic only: every call on one is kept as a medical
        response, whatever Ember makes of it. `known` lists the units and the
        streets a row may name. `backlog` has the poller make passes over every
        call on the shelf, however old, where it otherwise follows a recorder and
        passes over what is older than `MAX_AGE`; `calls_per_poll` is the most
        calls whose audio one poll reads, whatever comes of them, and so the most
        it has Ember hear.
        `transcoder` decodes a call that is M4A or MP3; without one only a WAV
        call is heard, and any other is counted as unreadable. `wall_clock` is the
        time a hold is counted by, which is the real time even while the mock
        scenario is played on a clock of its own.

        `ember` is closed with the poller (`close`), so it is a client of the
        poller's own."""
        if not talkgroups or not all(_is_talkgroup(number) for number in talkgroups):
            raise ValueError("The radio poller needs the numbers of the talkgroups to hear.")
        if not set(ems_talkgroups) <= set(talkgroups):
            raise ValueError("An EMS talkgroup is one of the talkgroups that are heard.")
        if isinstance(calls_per_poll, bool) or not 1 <= calls_per_poll <= MAX_LISTED:
            raise ValueError(f"A poll hears from 1 to {MAX_LISTED} calls.")
        self._shelf = shelf
        kind = _kind_of(shelf)
        self._origin: Origin = "live" if kind == "recorded" else "mock"
        if ember.mode not in _HEARERS[kind]:
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
        # How far the pass over the listing that is under way has got, or None
        # when the next poll begins one. It is not kept: a new process begins a
        # pass, and what was claimed costs it nothing.
        self._pass_at: Place | None = None
        # What the pass under way has come to in the polls that ended, and what
        # the last pass that ended came to.
        self._counted = PassCount()
        self._last_pass: PassCount | None = None
        # What every poller that pays Ember keeps to: `beacon.ingest.paid`.
        self._paid = PaidDesk(
            ember, poller="radio", items="calls", scope=SCOPE, wall_clock=wall_clock
        )

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is what its shelf is and cannot
        be set: `live` only for a `Directory` on this machine that is not marked as
        generated."""
        return self._origin

    @property
    def last_pass(self) -> PassCount | None:
        """What the last pass over the listing that ended came to, over all its
        polls, or None when none has ended in this process."""
        return self._last_pass

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
        # A pass over the listing goes on where the poll before left it, so that
        # every poll gets further, and a new one begins when that one has ended.
        if self._pass_at is not None:
            after = self._pass_at
        elif self._backlog:
            after = BEGINNING
        else:
            # From `OVERLAP` before where the last pass got to. The very first
            # poll, which has nothing behind it to read again, begins where its
            # progress does.
            behind = 0 if not saved else _seconds(OVERLAP)
            after = (progress.after_s - behind, "")
        if not self._backlog:
            # Never further back than `MAX_AGE`, however long a pass has taken.
            after = max(after, (now_s - _seconds(MAX_AGE), ""))
        stored = unready = unusable = read = 0
        ended = False
        for _ in range(MAX_PAGES):
            page, more = self._shelf.listed(after, now_s, self._talkgroups, MAX_LISTED)
            # In order, after the place asked about, and nothing else: a stand-in
            # may list what it was not asked for, and a call is never heard before
            # its time or on a talkgroup that is not heard.
            page = sorted(
                (
                    call
                    for call in page
                    if call.place > after
                    and call.start_s <= now_s
                    and call.talkgroup in self._talkgroups
                ),
                key=lambda call: call.place,
            )
            taken = store.taken(SOURCE, [call.ref for call in page])
            full = False
            for filed in page:
                if filed.ref not in taken:
                    outcome = self._take(
                        store, filed, progress, now, may_read=read < self._calls_per_poll
                    )
                    if outcome == "full":
                        # Its audio would be one more than a poll reads: this call
                        # and the ones after it wait for the next poll.
                        full = True
                        break
                    # What a poll is held to is the audio it reads, whatever came
                    # of the call: a call that was passed over by its JSON, or
                    # whose JSON could not be read, cost a small file's reading.
                    read += outcome in _AUDIO_READ
                    stored += outcome == "stored"
                    unready += outcome in ("unread", "unready")
                    unusable += outcome == "nothing"
                after = filed.place
            if full or not more or not page:
                ended = not full and not more
                break
        # At the end of a pass the next begins again, with what was not ready.
        self._pass_at = None if ended else after
        self._counted = PassCount(
            stored=self._counted.stored + stored,
            unready=self._counted.unready + unready,
            unusable=self._counted.unusable + unusable,
        )
        if ended:
            self._last_pass, self._counted = self._counted, PassCount()
        if not self._backlog:
            # Everything up to here was looked at: up to now, when the listing was
            # read to its end.
            progress.after_s = max(progress.after_s, now_s if ended else after[0])
        store.add([], state=(self.name, progress.as_state()), now=now)
        return PollReport(added=stored, unreadable=unready + unusable, partial=not ended)

    def _take(
        self,
        store: EvidenceStore,
        filed: Filed,
        progress: _Progress,
        now: datetime,
        *,
        may_read: bool,
    ) -> str:
        """Look at one call that is not taken, and have Ember hear it when it is
        ready and worth hearing. Says what came of it. With no audio read: `unread`
        when its JSON cannot be read, `passed` when its JSON says it is not worth
        hearing or not yet its time, and `full` when its audio would have been
        read and `may_read` says the poll reads no more. With its audio read
        (`_AUDIO_READ`): `unready` when the audio cannot be used as it is, `lost`
        when another process claimed the call meanwhile, and, for a call Ember was
        asked about, `stored` or `nothing`. A call that was `unread` or `unready`
        is left unclaimed.

        Raises what `_audio` and `_hear` raise, which ends the poll."""
        call = self._details(filed)
        if call is None:
            return "unread"
        if call.encrypted or not MIN_CALL_SECONDS <= call.seconds <= MAX_CALL_SECONDS:
            return "passed"
        if call.time > now:
            return "passed"
        if not may_read:
            return "full"
        # Free, and done before the claim: a call whose audio is not whole yet is
        # not claimed, and is looked at again.
        audio = self._audio(call)
        if audio is None:
            return "unready"
        if not store.claim(SOURCE, call.ref, now=now):
            # Another process took it between the listing and now.
            return "lost"
        # From here to the row, a shutdown waits (`drain`). A shutdown that falls
        # between the claim and here finds a closed Ember, and at worst the claim
        # cannot be given up: the call is lost, and nothing was paid.
        with self._paid.working():
            said = self._hear(store, call, audio, progress, now)
            progress.after_s = max(progress.after_s, call.time_s)
            if said is None:
                return "nothing"
            # The row and what the poller remembers are saved together.
            state = (self.name, progress.as_state())
            store.add([self._row(call, said)], state=state, now=now)
        return "stored"

    def _progress(self, saved: dict[str, Any], now: datetime) -> _Progress:
        """What the poller saved last. With nothing saved, a poller that follows a
        recorder begins `OVERLAP` ago, so the first poll hears the calls of the last
        few minutes and not the archive, and it never goes back further than
        `MAX_AGE`. A poller with a backlog keeps no place between runs."""
        now_s = int(now.timestamp())
        after_s = saved.get("after_s")
        if isinstance(after_s, bool) or not isinstance(after_s, int) or after_s < 0:
            after_s = now_s - _seconds(OVERLAP)
        if not self._backlog:
            after_s = max(after_s, now_s - _seconds(MAX_AGE))
        held_until, unusable_run = read_hold(saved)
        return _Progress(after_s=after_s, held_until=held_until, unusable_run=unusable_run)

    def _details(self, filed: Filed) -> Call | None:
        """A call by its JSON file, or None when the file is gone or cannot be read."""
        body = self._shelf.details(filed)
        return None if body is None else parse_details(body, filed)

    def _audio(self, call: Call) -> bytes | None:
        """A call's audio as a WAV file Beacon made, or None when it is not there
        to be heard as it is: its audio is missing, is not audio Beacon can decode,
        or does not play as long as the call's JSON says, which is so of a file
        that is still being written.

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
        # How long it really plays: that is what is paid for.
        played = wav_seconds(wav)
        longest = MAX_CALL_SECONDS + _LENGTH_SLACK_SECONDS
        if played is None or not MIN_CALL_SECONDS - _LENGTH_SLACK_SECONDS <= played <= longest:
            return None
        return wav if _whole(played, call) else None

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
    one Beacon can read: no JSON object, no length, a talkgroup that is not the one
    its files' name gives, or a start that is before the name's or more than
    `MAX_START_AFTER_NAME` after it. Nothing of it is kept but what `Call` holds."""
    try:
        said = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(said, dict):
        return None
    talkgroup = said.get("talkgroup")
    if isinstance(talkgroup, bool) or talkgroup != filed.talkgroup:
        return None
    start = _start(said)
    late = None if start is None else start - filed.start_s
    # A second of leave before the name's start: the name's is cut to the second.
    if late is None or not -1 <= late <= MAX_START_AFTER_NAME.total_seconds():
        return None
    exact = _is_count(said.get("call_length_ms"))
    seconds = said["call_length_ms"] / 1000 if exact else said.get("call_length")
    if not _is_seconds(seconds):
        stop = said.get("stop_time")
        seconds = stop - start if _is_seconds(stop) else None
    if not _is_seconds(seconds):
        return None
    emergency, encrypted = said.get("emergency", 0), said.get("encrypted", 0)
    if emergency not in (0, 1) or encrypted not in (0, 1):
        return None
    return Call(
        ref=filed.ref,
        system=filed.system,
        talkgroup=filed.talkgroup,
        time=datetime.fromtimestamp(start, UTC),
        seconds=float(seconds),
        exact=exact,
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
    """What a shelf's calls are. Only `Directory` itself reads a directory on this
    machine: anything else, a class made from it included, is a stand-in, whose
    calls are no recording however they are read, and there is no way to say
    otherwise."""
    if type(shelf) is not Directory:
        return "stand-in"
    return "generated" if shelf.generated else "recorded"


def _whole(played: float, call: Call) -> bool:
    """Whether audio that plays for `played` seconds is the call its JSON
    describes, all of it."""
    slack = _LENGTH_SLACK_SECONDS + call.seconds / 50
    if not call.exact:
        slack += _ROUNDED_SLACK_SECONDS
    return abs(played - call.seconds) <= slack


def _start(said: dict[str, Any]) -> float | None:
    """When a call's JSON says it began, in seconds since 1970: to the millisecond
    where it says so, and else to the second."""
    start_ms, start = said.get("start_time_ms"), said.get("start_time")
    if _is_count(start_ms):
        return start_ms / 1000
    return float(start) if _is_count(start) else None


class _TooMany(Exception):
    """A day's directory has more entries than are looked at."""


# How a directory is opened: for reading, as a directory, and not through a link.
_DIRECTORY = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
# What opening fails with when a file, or a directory on the way to it, is not
# there, is no directory, or is a symbolic link.
_NOT_THERE = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)


@contextlib.contextmanager
def _opened(above: int, *names: str) -> Iterator[int]:
    """A descriptor for the directory `names` lead to from the open directory
    `above`, each opened from the one before it and none through a symbolic link,
    or -1 when one of them is not there or is no directory. It is closed after.

    Raises OSError for any other failure."""
    descriptor = -1
    try:
        for name in names:
            below = os.open(name, _DIRECTORY, dir_fd=above if descriptor < 0 else descriptor)
            if descriptor >= 0:
                os.close(descriptor)
            descriptor = below
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        descriptor = -1
        if error.errno not in _NOT_THERE:
            raise
        yield -1
        return
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _marked(root: int) -> bool | None:
    """Whether the open directory has the mark of generated audio at its top: True
    for a regular file of that name, False when nothing has the name, and None
    when it cannot be told, which is any other thing of that name and any failure
    to look."""
    try:
        found = os.stat(GENERATED_MARK, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True if stat.S_ISREG(found.st_mode) else None


def _days(system: int, first: date, last: date) -> list[tuple[date, tuple[str, str, str]]]:
    """The directories of the days from `first` to `last` under a system's open
    directory, in the order of their dates, each with the names that lead to it. A
    year or a month outside those days is not opened. Raises OSError."""
    days: list[tuple[date, tuple[str, str, str]]] = []
    for year in _directories(system):
        if not first.year <= int(year) <= last.year:
            continue
        with _opened(system, year) as of_year:
            for month in _directories(of_year) if of_year >= 0 else []:
                if (
                    not (first.year, first.month)
                    <= (int(year), int(month))
                    <= (
                        last.year,
                        last.month,
                    )
                ):
                    continue
                with _opened(of_year, month) as of_month:
                    for day in _directories(of_month) if of_month >= 0 else []:
                        when = _date(year, month, day)
                        if when is not None and first <= when <= last:
                            days.append((when, (year, month, day)))
    return sorted(days)


def _directories(where: int) -> list[str]:
    """The names of the directories in an open directory that are numbers, and are
    no symbolic links. Raises OSError when it cannot be listed."""
    with os.scandir(where) as entries:
        return [
            entry.name
            for entry in entries
            if _NUMBER.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
        ]


def _calls_of(
    day: int,
    system: str,
    names: tuple[str, str, str],
    after: Place,
    upto_s: int,
    talkgroups: Collection[int],
) -> Iterator[Filed]:
    """The calls of one day's open directory that are asked for, by their JSON
    files' names, one at a time: nothing here holds a day's calls.

    Raises _TooMany when the directory has more entries than are looked at: a part
    of it is not an answer, and OSError when it cannot be listed."""
    looked = 0
    with os.scandir(day) as entries:
        for entry in entries:
            looked += 1
            if looked > _MAX_DAY_ENTRIES:
                raise _TooMany
            if not entry.name.endswith(".json"):
                continue
            filed = filed_as(system, names, entry.name[:-5])
            if filed is None or filed.talkgroup not in talkgroups:
                continue
            if filed.place <= after or filed.start_s > upto_s:
                continue
            if entry.is_file(follow_symlinks=False):
                yield filed


def _file(directory: int, name: str, most: int) -> bytes | None:
    """The bytes of a file in an open directory, or None when it is not there, is
    a symbolic link or no regular file, or is longer than `most`. Raises OSError
    when it is there and cannot be read."""
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except OSError as error:
        if error.errno in _NOT_THERE:
            return None
        raise
    try:
        # Looked at before it is read: a directory opens, and is no file.
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb") as file:
            descriptor = -1
            body = file.read(most + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return body if len(body) <= most else None


def _earliest_in(day: date) -> int:
    """The earliest second a call filed under a day can have begun, wherever its
    recorder keeps its clock."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return int((midnight - _ZONE_REACH).timestamp())


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


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**14


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
