"""Fire and EMS dispatch radio for San Francisco, from OpenMHz.

OpenMHz archives the calls of public-safety radio systems. Its API has no published
documentation; the shapes here are read from the source of the server it runs
(https://github.com/robotastic/trunk-server) and are unverified, because **Beacon has
never asked OpenMHz anything**: using its audio needs its permission, which Beacon
does not have yet. `beacon.ingest.setup` builds this poller for the real API only
when `BEACON_OPENMHZ_PERMITTED=1` says that permission has been given.

`GET https://api.openmhz.com/{system}/calls/newer?time=<ms>` answers with the calls
after a moment, each with its talkgroup, its length, and the address of its audio.
The poller downloads a call's audio, has Ember hear it (`beacon.radio`), and stores
one row for the call. Three things follow from what the audio is:

- **Nothing a call said is stored as text, and its audio is not stored at all.** A
  row holds what `beacon.radio.Transmission` holds: tags from fixed lists, unit ids,
  a street name, and a hundred block. The audio is held in memory, and only for as
  long as the call is being worked on.
- **Every call costs money**, and is paid for once. The poller asks for the
  talkgroups it was given and no others, passes over a call that is too short or too
  long to be worth hearing, hears at most `MAX_CALLS_PER_POLL` calls a poll, and
  writes down how far it has got after every call, with the call's row, so a call is
  not heard twice whatever happens after it. A call that failed after Ember was
  asked is not tried again: it may have been paid for. And when Ember itself fails
  on a call, the poll ends there and the poller waits `HELD_INTERVAL_SECONDS`: an
  Ember that fails on one call likely fails on the next, and each try can cost. (One
  gap is left: a process that dies after Ember answered and before the row is stored
  hears that one call again when it restarts.)
- **A stopped budget stops the poller, not the server.** When Ember refuses a call
  because of a spending stop, the poll ends there and the poller waits
  `HELD_INTERVAL_SECONDS` before it looks again. Calls that grew older than `MAX_AGE`
  in the meantime are passed over, so that lifting a stop does not buy a backlog.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from beacon.ember import (
    BudgetExceededError,
    EmberClient,
    EmberError,
    RateLimitHoldError,
    ReplayMissError,
)
from beacon.errors import raise_bare
from beacon.evidence import Confidence, EvidenceStore, NewEvidence, Origin
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.runner import PollReport
from beacon.radio import AudioError, ExtractionError, Transcoder, Transmission, extract_transmission
from beacon.radio.audio import MAX_SECONDS as MAX_AUDIO_SECONDS
from beacon.radio.audio import container

SOURCE = "openmhz"
API = "https://api.openmhz.com"
# San Francisco's P25 system, by the short name OpenMHz gives it.
SAN_FRANCISCO = "sfp25"

INTERVAL_SECONDS = 30.0
# How long the poller leaves Ember alone after a spending stop or a rate limit
# refused a call.
HELD_INTERVAL_SECONDS = 600.0

# The first poll asks for the calls of the last two minutes, not the archive.
LOOKBACK = timedelta(minutes=2)
# A call older than this is not heard: nobody is tapping about it any more.
MAX_AGE = timedelta(minutes=15)
MAX_CALLS_PER_POLL = 20
# A call shorter than this is a click of the microphone.
MIN_CALL_SECONDS = 1.0
MAX_CALL_SECONDS = float(MAX_AUDIO_SECONDS)

_MAX_LIST_BYTES = 2_000_000
# Two minutes of M4A is a few hundred kilobytes, and of WAV at 16 kHz, 3.8 MB.
_MAX_AUDIO_BYTES = 4_000_000
_SYSTEM = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# How well Ember says it heard a call decides how far the row is trusted. Ember's
# own judgment of that is not measured yet, so nothing heard is more than `medium`.
# The refusals Ember makes before anything is sent, so that nothing was paid: a
# spending stop, a rate limit that is being waited out, and a client that only
# replays, which cannot hear a call at all.
_NOT_SENT = (BudgetExceededError, RateLimitHoldError, ReplayMissError)
_CONFIDENCE: dict[str, Confidence] = {"clear": "medium", "partial": "low", "unintelligible": "low"}


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


class OpenMhzRadio:
    name = SOURCE

    def __init__(
        self,
        http: SourceHttp,
        ember: EmberClient,
        *,
        talkgroups: Collection[int],
        ems_talkgroups: Collection[int] = (),
        system: str = SAN_FRANCISCO,
        media_hosts: Collection[str] = (),
        transcoder: Transcoder | None = None,
    ) -> None:
        """`talkgroups` are the channels to hear, by OpenMHz's numbers, and
        `ems_talkgroups` the ones among them that carry EMS traffic only: every call
        on one is kept as a medical response, whatever Ember makes of it.
        `media_hosts` are hosts a call's audio may be on besides OpenMHz's own.
        `transcoder` converts a call that is not WAV or MP3; without one such a call
        is counted as unreadable."""
        if _SYSTEM.fullmatch(system) is None:
            raise ValueError("An OpenMHz system is named by its short name, such as sfp25.")
        if not talkgroups or not all(_is_talkgroup(number) for number in talkgroups):
            raise ValueError("The radio poller needs the numbers of the talkgroups to hear.")
        if not set(ems_talkgroups) <= set(talkgroups):
            raise ValueError("An EMS talkgroup is one of the talkgroups that are heard.")
        if (http.origin == "live") != ember.spends:
            # Real radio is not handed to a stand-in for Ember, whose answer would
            # be an invention about a real call, and a real Ember is not paid to
            # hear a stand-in's tones.
            raise ValueError("Real radio is heard by a live Ember, and a stand-in's by a fake one.")
        self._http = http
        self._ember = ember
        self._system = system
        self._talkgroups = frozenset(talkgroups)
        self._ems_talkgroups = frozenset(ems_talkgroups)
        self._media_hosts = frozenset(host.lower() for host in media_hosts)
        self._transcoder = transcoder
        self.interval_seconds = INTERVAL_SECONDS

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is where its client's answers
        come from and cannot be set."""
        return self._http.origin

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        after_ms, done = _progress(store.state(self.name), now)
        body = fetch(
            self._http,
            "GET",
            f"{API}/{self._system}/calls/newer",
            source=SOURCE,
            max_bytes=_MAX_LIST_BYTES,
            params={
                # A millisecond earlier, so that the calls of the very moment the
                # poller had got to are listed again: another call can share it.
                "time": str(after_ms - 1),
                "filter-type": "talkgroup",
                "filter-code": ",".join(str(number) for number in sorted(self._talkgroups)),
            },
        )
        calls, unreadable = parse_calls(
            read_json(body, SOURCE), self._talkgroups, self._media_hosts
        )
        # Oldest first, whichever way the answer was ordered. A call the answer
        # dates in the future is left for the poll after that moment.
        waiting = sorted(
            (
                call
                for call in calls
                if after_ms <= call.time_ms <= _ms(now) and call.ref not in done
            ),
            key=lambda call: (call.time_ms, call.ref),
        )
        added = 0
        self.interval_seconds = INTERVAL_SECONDS
        for call in waiting[:MAX_CALLS_PER_POLL]:
            row = None
            failure: EmberError | None = None
            if store.find(SOURCE, call.ref) is None and (
                MIN_CALL_SECONDS <= call.seconds <= MAX_CALL_SECONDS
            ):
                try:
                    heard = self._hear(call)
                except _NOT_SENT:
                    # Nothing was paid, and the call is still waiting at the next poll.
                    self.interval_seconds = HELD_INTERVAL_SECONDS
                    raise
                except EmberError as error:
                    # Ember was asked and failed. The call may have been paid for,
                    # so it is finished with, like one that was heard.
                    heard, failure = None, error
                if heard is None:
                    unreadable += 1
                else:
                    row = self._row(call, heard)
            done = [*done, call.ref] if call.time_ms == after_ms else [call.ref]
            after_ms = call.time_ms
            # The row and how far the poller has got are saved together, after
            # every call: a call that was paid for is never asked about again.
            stored = store.add(
                [] if row is None else [row],
                state=(self.name, {"after_ms": after_ms, "done": done}),
                now=now,
            )
            added += len(stored)
            if failure is not None:
                # The next call would likely fail the same way, and could cost.
                self.interval_seconds = HELD_INTERVAL_SECONDS
                raise failure
        return PollReport(
            added=added, unreadable=unreadable, partial=len(waiting) > MAX_CALLS_PER_POLL
        )

    def _hear(self, call: Call) -> Transmission | None:
        """What Ember heard in a call, or None for a call that is passed over for
        good: its audio is not there, is not audio Beacon can use, or Ember's
        answer about it was not a form.

        Raises SourceError when the audio could not be fetched for a reason that
        may pass, and a `beacon.ember.EmberError` when Ember refused the call or
        failed on it. Its text is fixed, because the call is a sensitive one."""
        try:
            audio = fetch(self._http, "GET", call.url, source=SOURCE, max_bytes=_MAX_AUDIO_BYTES)
        except SourceError as error:
            gone = error.status is not None and 400 <= error.status < 500
            if not gone or error.status in (408, 429):
                raise
            return None
        try:
            kind = container(audio)
            if kind == "m4a" and self._transcoder is not None:
                audio, kind = self._transcoder.to_wav(audio), "wav"
            if kind not in ("wav", "mp3"):
                return None
            return extract_transmission(
                self._ember, audio, kind, ems_channel=call.talkgroup in self._ems_talkgroups
            )
        except (AudioError, ExtractionError):
            # This call's own trouble: its audio, or what Ember made of it. Not
            # worth the price of asking again, and no reason to doubt the next call.
            return None

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


def _progress(saved: dict[str, Any], now: datetime) -> tuple[int, list[str]]:
    """How far the poller has got: the time of the last call it finished with, and
    the calls of that very millisecond it finished with. With nothing saved it
    begins `LOOKBACK` ago, and it never goes back further than `MAX_AGE`."""
    after_ms, done = saved.get("after_ms"), saved.get("done")
    if isinstance(after_ms, bool) or not isinstance(after_ms, int):
        return _ms(now - LOOKBACK), []
    oldest = _ms(now - MAX_AGE)
    if after_ms < oldest:
        return oldest, []
    return after_ms, [
        ref for ref in (done if isinstance(done, list) else []) if isinstance(ref, str)
    ]


def _is_talkgroup(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000_000


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)
