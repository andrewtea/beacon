"""The recorded-radio poller, against generated tones: on a stand-in for a shelf of
recordings, and on directories of files as trunk-recorder leaves them. Nothing in
these tests is a recording."""

import base64
import io
import json
import logging
import os
import sqlite3
import threading
import time as system_time
import wave
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import (
    BudgetExceededError,
    EmberClient,
    EmberClosedError,
    EmberError,
    LedgerReplacedError,
    Pacer,
    SpendScope,
    Storage,
    fake_response,
)
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.ingest import Clock, Ingest, Runner, SourceError
from beacon.ingest import recordings as recordings_module
from beacon.ingest.http import SourceHttp
from beacon.ingest.recordings import (
    BEGINNING,
    GENERATED_MARK,
    HELD_INTERVAL_SECONDS,
    MAX_AGE,
    MAX_CALLS_PER_POLL,
    MAX_LISTED,
    MAX_START_AFTER_NAME,
    OVERLAP,
    UNUSABLE_RUN_LIMIT,
    Directory,
    Filed,
    PassCount,
    Place,
    RecordedRadio,
    filed_as,
    parse_details,
)
from beacon.ingest.runner import PollerHeldError, PollReport
from beacon.radio import (
    NOTHING_KNOWN,
    AudioError,
    ConversionStopped,
    ConversionUnavailable,
    Known,
)
from beacon.radio.audio import pcm_to_wav

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
FIRE, TACTICAL, EMS = 1201, 1203, 1301
# What the tests' recorder calls its system, and the frequency its calls are on.
SYSTEM = "sfd"
FREQUENCY = 851012500

PRIVATE = ("Jane", "Doe", "555-0100", "chest pain", "unconscious", "year old")
# The streets and the units these tests' radio may name.
KNOWN = Known.of(streets=["Hayes St", "Gough St"], units=["E36", "T6", "B2", "M52"])
# For a call whose audio file is not there.
GONE = b""


def seconds(count: float) -> datetime:
    return NOON + timedelta(seconds=count)


def tone(number: int, length: float = 1.0) -> bytes:
    """A WAV file no other call has, a second long unless it is said how long.
    Nothing in these tests is a recording."""
    return pcm_to_wav(number.to_bytes(2, "little") * round(16_000 * length))


def form(**changes: Any) -> dict[str, Any]:
    filled = {
        "clarity": "clear",
        "category": "fire",
        "status": "working",
        "details": ["working_fire"],
        "units": ["E36", "T6"],
        "alarm": None,
        "street": "Hayes St",
        "block": 400,
        "cross_street": None,
    }
    return filled | changes


def filed_at(number: int, at: datetime, talkgroup: int = FIRE) -> Filed:
    """A call's files as trunk-recorder v5.2.1 names them: by talkgroup, start
    with its milliseconds, frequency, and the recorder's count of calls."""
    start_s = int(at.timestamp())
    return Filed(
        system=SYSTEM,
        day=(str(at.year), str(at.month), str(at.day)),
        stem=f"{talkgroup}-{start_s}.{at.microsecond // 1000:03d}_{FREQUENCY}-call_{number}",
        talkgroup=talkgroup,
        start_s=start_s,
    )


def details_of(filed: Filed, length: float = 1.0, **changes: Any) -> bytes:
    """The JSON file trunk-recorder v5 writes for a call, with the radio that
    transmitted in it, which Beacon does not keep. The tests' tones play a second."""
    start_ms = round(float(filed.stem.split("-")[1].split("_")[0]) * 1000)
    return json.dumps(
        {
            "freq": FREQUENCY,
            "start_time": filed.start_s,
            "stop_time": filed.start_s + int(length) + 1,
            "start_time_ms": start_ms,
            "stop_time_ms": start_ms + round(length * 1000) + 1000,
            "emergency": 0,
            "encrypted": 0,
            "call_length": round(length),
            "call_length_ms": round(length * 1000),
            "talkgroup": filed.talkgroup,
            "talkgroup_tag": "Fire Dispatch",
            "audio_type": "digital",
            "short_name": filed.system,
            "freqList": [{"freq": FREQUENCY, "time": filed.start_s, "pos": 0.0, "len": length}],
            "srcList": [{"src": 4512345, "time": filed.start_s, "pos": 0.0, "tag": "Jane Doe"}],
        }
        | changes
    ).encode()


class Radio:
    """Stands in for a shelf of recordings and for Ember. `call` puts a call on the
    shelf: it is listed, its files are read, and Ember "hears" in it the form it is
    given."""

    def __init__(self) -> None:
        # What is on the shelf, by a call's reference: its place, its JSON file,
        # and its audio file, each of which may be an error that reading it raises.
        self.filed: dict[str, Filed] = {}
        self.json: dict[str, bytes | Exception] = {}
        self.clips: dict[str, bytes | Exception] = {}
        self.numbers: dict[int, str] = {}
        self.heard: dict[bytes, Any] = {}
        # What was asked of the shelf: each listing, by the place it was to come
        # after and the second it was to reach, and the audio files read.
        self.listings: list[tuple[Place, int]] = []
        self.downloads: list[str] = []
        self.read_details: list[str] = []
        # The Ember requests, by the audio each carried.
        self.asked: list[bytes] = []
        # What Ember's client refuses a call with before anything is sent, by its audio.
        self.refused: dict[bytes, Exception] = {}
        # What listing the shelf fails with, when it does.
        self.down: Exception | None = None

    def call(
        self,
        number: int,
        at: datetime,
        heard: Any = None,
        *,
        talkgroup: int = FIRE,
        length: float = 1.0,
        audio: bytes | Exception | None = None,
        **changes: Any,
    ) -> str:
        filed = filed_at(number, at, talkgroup)
        clip = tone(number) if audio is None else audio
        self.filed[filed.ref] = filed
        self.json[filed.ref] = details_of(filed, length, **changes)
        self.clips[filed.ref] = clip
        self.numbers[number] = filed.ref
        if isinstance(clip, bytes) and clip:
            self.heard[clip] = form() if heard is None else heard
        return filed.ref

    def ref(self, number: int) -> str:
        return self.numbers[number]

    @property
    def requests(self) -> list[Any]:
        """Everything that was asked of the shelf."""
        return [*self.listings, *self.read_details, *self.downloads]

    def listed(
        self, after: Place, upto_s: int, talkgroups: Collection[int], limit: int
    ) -> tuple[list[Filed], bool]:
        self.listings.append((after, upto_s))
        if self.down is not None:
            raise self.down
        found = sorted(
            (
                filed
                for filed in self.filed.values()
                if filed.place > after and filed.start_s <= upto_s and filed.talkgroup in talkgroups
            ),
            key=lambda filed: filed.place,
        )
        return found[:limit], len(found) > limit

    def details(self, call: Filed) -> bytes | None:
        self.read_details.append(call.ref)
        body = self.json.get(call.ref)
        if isinstance(body, Exception):
            raise body
        return body

    def audio(self, call: Filed) -> bytes | None:
        self.downloads.append(call.ref)
        clip = self.clips.get(call.ref)
        if isinstance(clip, Exception):
            raise clip
        return clip or None

    def ember(self, body: dict[str, Any]) -> dict[str, Any]:
        (message,) = body["input"]
        clip = base64.b64decode(message["content"][1]["input_audio"]["data"])
        self.asked.append(clip)
        heard = self.heard[clip]
        if isinstance(heard, Exception):
            raise heard
        if callable(heard):
            # A whole response, for an answer that is not shaped as the API's are.
            return heard()
        return fake_response(text=heard if isinstance(heard, str) else json.dumps(heard))


@pytest.fixture
def radio() -> Radio:
    return Radio()


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "mock")


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


@pytest.fixture(autouse=True)
def refusals(radio: Radio, monkeypatch: pytest.MonkeyPatch) -> None:
    """Has the Ember client refuse the calls `radio.refused` names, with the error
    it names: a fake client has no spending stop and no ledger to lose."""
    hear = recordings_module.extract_transmission

    def hearing(ember: EmberClient, audio: bytes, *args: Any, **options: Any) -> Any:
        if audio in radio.refused:
            raise radio.refused[audio]
        return hear(ember, audio, *args, **options)

    monkeypatch.setattr(recordings_module, "extract_transmission", hearing)


def build(radio: Radio, storage: Storage, responder: Any = None, **options: Any) -> RecordedRadio:
    return RecordedRadio(
        radio,
        EmberClient.fake(storage, radio.ember if responder is None else responder),
        **{"talkgroups": (FIRE, TACTICAL, EMS), "ems_talkgroups": (EMS,), "known": KNOWN} | options,
    )


@pytest.fixture
def wall() -> list[datetime]:
    """The real time, as a poller is told it, which a test moves."""
    return [NOON]


@pytest.fixture
def poller(radio: Radio, storage: Storage) -> RecordedRadio:
    return build(radio, storage)


def refs(store: EvidenceStore) -> list[str]:
    return [row.ref for row in store.search()]


def no_network() -> SourceHttp:
    """The client an `Ingest` closes, for pollers that ask the network nothing."""
    return SourceHttp(httpx2.MockTransport(lambda request: httpx2.Response(404)))


def test_a_call_becomes_a_row_of_what_ember_heard_in_it_and_nothing_it_said(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    ref = radio.call(
        1,
        seconds(-30),
        form(details=["working_fire"], alarm=2),
        length=6.5,
        audio=tone(1, 6.5),
    )

    report = poller.poll(store, NOON)

    (row,) = store.search()
    assert report.added == 1
    assert (row.kind, row.source, row.ref) == ("radio", "recordings", ref)
    assert row.occurred_at == seconds(-30)
    # A place in words, no finer than a block. Nothing here knows where a block is.
    assert (row.place, row.point, row.until, row.url) == (
        "400 block of Hayes St",
        None,
        None,
        None,
    )
    # A stand-in for the network and a stand-in for Ember, and the row says both.
    assert (row.origin, row.model, row.stand_in) == ("mock", "fake", True)
    assert row.confidence == "medium"
    assert row.data == {
        "event": "fire",
        "category": "fire",
        "clarity": "clear",
        "status": "working",
        "details": ["working_fire"],
        "units": ["E36", "T6"],
        "alarm": 2,
        "street": "Hayes St",
        "block": 400,
        "cross_street": None,
        "system": SYSTEM,
        "talkgroup": FIRE,
        "seconds": 6.5,
        "emergency": False,
    }
    assert radio.asked == [tone(1, 6.5)]


def test_what_a_call_named_reaches_no_row_no_file_and_no_log(
    store: EvidenceStore,
    radio: Radio,
    poller: RecordedRadio,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    radio.call(
        1,
        seconds(-60),
        form(
            category="medical",
            status="on_scene",
            details=["rescue_in_progress"],
            # A reading and an age, each shaped like a unit, beside a real one.
            units=["M52", "Jane Doe", "BP 180", "Ann 72"],
            street="1458 Hayes Street",
            block=1458,
            cross_street="unconscious 74 year old, chest pain",
            transcript="Medic 52 to 1458 Hayes for Jane Doe, 555-0100",
        ),
    )
    # A call on the EMS channel, which Ember took for something else.
    radio.call(2, seconds(-50), form(category="service", status="en_route"), talkgroup=EMS)
    # A call Ember answered about in a sentence, and one whose audio is an error page.
    radio.call(3, seconds(-40), "Medic 52 is with Jane Doe, who is unconscious")
    radio.call(4, seconds(-30), audio=b"<html>Jane Doe, 555-0100</html>")
    # A house number spelled out, and a person's name, each shaped like a street.
    radio.call(
        5,
        seconds(-20),
        form(street="One Two Three Hayes St", block=None, cross_street="Jane Doe St"),
    )
    radio.call(6, seconds(-10), form(street="Hayes St", cross_street="Jane Doe St"))
    runner = Runner([poller], store, Clock(lambda: NOON))

    with caplog.at_level(logging.DEBUG):
        results = runner.step()

    assert (results["recordings"].added, results["recordings"].unreadable) == (4, 2)
    medical, on_ems_channel, no_street, no_cross_street = store.search()
    for row in (medical, on_ems_channel):
        assert (row.data["event"], row.data["status"], row.data["details"]) == (
            "medical response",
            "unknown",
            [],
        )
    assert (medical.place, medical.data["units"]) == ("1400 block of Hayes St", ["M52"])
    assert (medical.data["block"], medical.data["cross_street"]) == (1400, None)
    assert (no_street.place, no_street.data["street"], no_street.data["cross_street"]) == (
        None,
        None,
        None,
    )
    assert (no_cross_street.place, no_cross_street.data["cross_street"]) == (
        "400 block of Hayes St",
        None,
    )
    on_disk = "\n".join(
        path.read_bytes().decode("latin-1") for path in tmp_path.rglob("*") if path.is_file()
    )
    with sqlite3.connect(store.path) as db:
        stored = json.dumps(db.execute("SELECT * FROM evidence").fetchall())
        stored += json.dumps(db.execute("SELECT * FROM poller_state").fetchall())
        stored += json.dumps(db.execute("SELECT * FROM claims").fetchall())
    # Whole forms, as they were said and as their shape would be stored: a number as
    # short as 180 turns up by chance in a file's hashes.
    for word in (*PRIVATE, "BP180", "BP 180", "ANN72", "Ann 72", "One Two"):
        assert word not in on_disk
        assert word not in stored
        assert word not in caplog.text
    # What is kept of the calls holds no trace of the reading or the age.
    for number in ("180", "72"):
        assert number not in json.dumps([row.data for row in store.search()])
    assert "1458" not in stored
    # The audio is not kept, as it came or as it was sent to Ember.
    assert tone(1)[44:108].decode("latin-1") not in on_disk
    assert base64.b64encode(tone(1)).decode()[100:300] not in on_disk
    # Nothing is on disk but the evidence store and the Ember ledger.
    assert {path.name for path in tmp_path.rglob("*") if path.is_file()} <= {
        "evidence.sqlite3",
        "evidence.sqlite3-shm",
        "evidence.sqlite3-wal",
        "ledger.sqlite3",
    }


def test_with_no_list_of_streets_and_units_a_row_names_none(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    poller = build(radio, storage, known=NOTHING_KNOWN)
    radio.call(1, seconds(-30), form(alarm=2))

    poller.poll(store, NOON)

    (row,) = store.search()
    assert (row.place, row.data["units"], row.data["street"], row.data["block"]) == (
        None,
        [],
        None,
        None,
    )
    assert (row.data["event"], row.data["status"], row.data["alarm"]) == ("fire", "working", 2)


def test_a_call_that_is_listed_late_is_still_heard(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-10))
    assert poller.poll(store, NOON).added == 1

    # Its files turned up after a later call's did: it is first listed two
    # seconds behind where the poller had got to.
    radio.call(2, seconds(-12))
    report = poller.poll(store, seconds(30))

    assert (report.added, report.partial) == (1, False)
    assert refs(store) == [radio.ref(2), radio.ref(1)]
    assert radio.asked == [tone(1), tone(2)]


def test_calls_already_taken_take_no_place_among_the_calls_a_poll_may_hear(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    for number in range(1, MAX_CALLS_PER_POLL + 1):
        radio.call(number, seconds(-100 + number))
    assert poller.poll(store, NOON).added == MAX_CALLS_PER_POLL

    # Every one of those is listed again, with one new call behind them.
    radio.call(99, seconds(10))
    report = poller.poll(store, seconds(30))

    assert (report.added, report.partial) == (1, False)
    assert len(radio.asked) == MAX_CALLS_PER_POLL + 1


def test_a_call_too_short_or_too_long_to_be_worth_hearing_is_not_paid_for(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-50), length=0.4)
    radio.call(2, seconds(-40), length=0)
    radio.call(3, seconds(-30), length=121)
    radio.call(4, seconds(-20), length=1.0)

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 0)
    assert (refs(store), radio.downloads, radio.asked) == (
        [radio.ref(4)],
        [radio.ref(4)],
        [tone(4)],
    )
    # They are passed over for good, not looked at again.
    poller.poll(store, seconds(30))
    assert len(radio.asked) == 1


def three_minutes() -> bytes:
    """A WAV file that plays for three minutes, small enough to be fetched."""
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(1)
        clip.setframerate(8_000)
        clip.writeframes(b"\x80\x90" * 4_000 * 180)
    return out.getvalue()


def test_a_call_is_measured_by_its_audio_and_not_by_what_its_json_says(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    # Each a second long by its JSON. One plays for three minutes, and one for a
    # twentieth of a second.
    long = radio.call(1, seconds(-30), audio=three_minutes())
    short = radio.call(2, seconds(-20), audio=pcm_to_wav(b"\x02\x00" * 800))
    radio.call(3, seconds(-10))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 2)
    assert radio.asked == [tone(3)]
    # Neither is claimed or paid for: its audio is not the call its JSON describes.
    assert store.taken("recordings", [long, short]) == set()


@pytest.mark.parametrize(
    ("said", "played", "ready"),
    [
        # To the millisecond: half a second and a fiftieth of the length.
        ({"call_length_ms": 10_000}, 10.6, True),
        ({"call_length_ms": 10_000}, 9.4, True),
        ({"call_length_ms": 10_000}, 9.2, False),
        ({"call_length_ms": 10_000}, 10.8, False),
        # In whole seconds, as before v5: a second more.
        ({"call_length_ms": None, "call_length": 10}, 8.4, True),
        ({"call_length_ms": None, "call_length": 10}, 11.6, True),
        ({"call_length_ms": None, "call_length": 10}, 8.2, False),
        ({"call_length_ms": None, "call_length": 10}, 11.8, False),
    ],
)
def test_a_call_is_ready_when_its_audio_plays_about_as_long_as_its_json_says(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio, said: Any, played: float, ready: bool
) -> None:
    ref = radio.call(1, seconds(-30), audio=tone(1, played), **said)

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == ((1, 0) if ready else (0, 1))
    assert store.taken("recordings", [ref]) == ({ref} if ready else set())


def test_a_call_whose_audio_is_still_being_written_is_not_claimed_and_is_heard_when_it_is_whole(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    # trunk-recorder writes a call's JSON before its audio is all there.
    growing = radio.call(1, seconds(-30), length=6.0, audio=tone(1, 2.0))
    absent = radio.call(2, seconds(-25), audio=GONE)
    cut = radio.call(3, seconds(-20), length=4.0, audio=tone(3, 4.0)[:-64_000])
    radio.call(4, seconds(-10))

    report = poller.poll(store, NOON)

    # None of the three is claimed or sent, and none keeps the fourth from being heard.
    assert (report.added, report.unreadable, report.partial) == (1, 3, False)
    assert radio.asked == [tone(4)]
    assert store.taken("recordings", [growing, absent, cut]) == set()

    for number, ref, length in ((1, growing, 6.0), (2, absent, 1.0), (3, cut, 4.0)):
        radio.clips[ref] = tone(number, length)
        radio.heard[tone(number, length)] = form()
    report = poller.poll(store, seconds(30))

    assert (report.added, report.unreadable) == (3, 0)
    assert radio.asked == [tone(4), tone(1, 6.0), tone(2), tone(3, 4.0)]
    assert refs(store) == [growing, absent, cut, radio.ref(4)]


def test_ember_is_sent_the_samples_of_a_wav_call_and_nothing_else_the_file_carried(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    clean = tone(1)
    # The same call with a chunk of text after its samples, as a recorder can add.
    note = b"LIST" + (20).to_bytes(4, "little") + b"INFOINAM" + b"Jane Doe 555"
    padded = clean[:4] + (len(clean) - 8 + len(note)).to_bytes(4, "little") + clean[8:] + note
    radio.call(1, seconds(-30), audio=padded)
    radio.heard[clean] = form()

    assert poller.poll(store, NOON).added == 1

    assert radio.asked == [clean]


def test_a_spending_stop_ends_the_poll_and_holds_the_poller_without_losing_a_call(
    store: EvidenceStore, radio: Radio, storage: Storage, wall: list[datetime]
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    radio.refused[tone(2)] = BudgetExceededError("The spending stop was reached.")
    poller = build(radio, storage, wall_clock=lambda: wall[0])
    runner = Runner([poller], store, Clock(lambda: wall[0]))

    results = runner.step()

    # The call before the stop is stored, and the poller leaves Ember alone a while.
    assert isinstance(results["recordings"], BudgetExceededError)
    assert refs(store) == [radio.ref(1)]
    wall[0] = seconds(HELD_INTERVAL_SECONDS - 1)
    held = runner.step()["recordings"]
    assert isinstance(held, PollerHeldError)
    assert held.retry_after == 1.0
    # Held, it does not look at the shelf either.
    assert len(radio.listings) == 1
    assert runner.step() == {}

    # The stop is lifted. The call it refused was never sent, and is heard now.
    radio.refused.clear()
    wall[0] = seconds(HELD_INTERVAL_SECONDS + 30)
    assert runner.step()["recordings"].added == 1
    assert refs(store) == [radio.ref(1), radio.ref(2)]
    assert radio.asked == [tone(1), tone(2)]


def test_a_hold_is_counted_from_the_failure_and_outlasts_a_restart(
    store: EvidenceStore, radio: Radio, storage: Storage, wall: list[datetime]
) -> None:
    radio.call(1, seconds(-30), RuntimeError("the API dropped the call"))
    radio.call(2, seconds(-20))

    def failed_after_a_while(body: dict[str, Any]) -> dict[str, Any]:
        # The poll had been running four minutes when Ember failed.
        wall[0] = seconds(240)
        return radio.ember(body)

    poller = build(radio, storage, failed_after_a_while, wall_clock=lambda: wall[0])
    with pytest.raises(EmberError):
        poller.poll(store, NOON)

    assert (
        store.state("recordings")["held_until"] == seconds(240 + HELD_INTERVAL_SECONDS).isoformat()
    )
    # Beacon is started again: a new poller, and the same store.
    restarted = build(radio, storage, wall_clock=lambda: wall[0])
    wall[0] = seconds(240 + HELD_INTERVAL_SECONDS - 60)
    with pytest.raises(PollerHeldError) as held:
        restarted.poll(store, wall[0])
    assert held.value.retry_after == 60.0
    assert str(held.value) == (
        "The radio poller is leaving Ember alone for a while, after Ember refused or failed."
    )
    assert radio.asked == [tone(1)]

    wall[0] = seconds(240 + HELD_INTERVAL_SECONDS)
    assert restarted.poll(store, wall[0]).added == 1
    assert radio.asked == [tone(1), tone(2)]


def test_answers_that_are_no_form_several_times_running_hold_the_poller(
    store: EvidenceStore, radio: Radio, storage: Storage, wall: list[datetime]
) -> None:
    for number in range(1, 7):
        radio.call(number, seconds(-60 + number), "A sentence, and no form.")
    poller = build(radio, storage, wall_clock=lambda: wall[0])

    with pytest.raises(PollerHeldError) as held:
        poller.poll(store, NOON)

    # Three were paid for, and then no more.
    assert len(radio.asked) == UNUSABLE_RUN_LIMIT
    assert held.value.retry_after == HELD_INTERVAL_SECONDS
    assert "not filled-in forms" in str(held.value)
    with pytest.raises(PollerHeldError):
        poller.poll(store, seconds(30))
    assert len(radio.asked) == UNUSABLE_RUN_LIMIT

    # After the hold, one more such answer holds it again.
    wall[0] = seconds(HELD_INTERVAL_SECONDS)
    with pytest.raises(PollerHeldError):
        poller.poll(store, wall[0])
    assert len(radio.asked) == UNUSABLE_RUN_LIMIT + 1

    # An answer that is a form ends the run.
    wall[0] = seconds(2 * HELD_INTERVAL_SECONDS)
    radio.call(7, wall[0] - timedelta(seconds=10))
    report = poller.poll(store, wall[0])
    assert (report.added, refs(store)) == (1, [radio.ref(7)])
    assert store.state("recordings")["unusable_run"] == 0


def test_answers_that_are_not_shaped_as_answers_hold_the_poller_as_well(
    store: EvidenceStore, radio: Radio, storage: Storage, wall: list[datetime]
) -> None:
    def no_content() -> dict[str, Any]:
        # Completed, with its token counts, and a message with nothing in it.
        response = fake_response(text="{}")
        response["output"][0]["content"] = [None]
        return response

    for number in range(1, 7):
        radio.call(number, seconds(-60 + number), no_content)
    poller = build(radio, storage, wall_clock=lambda: wall[0])

    with pytest.raises(PollerHeldError) as held:
        poller.poll(store, NOON)

    # Each was paid for, as an answer in a sentence is, and counts the same.
    assert len(radio.asked) == UNUSABLE_RUN_LIMIT
    assert "not filled-in forms" in str(held.value)
    assert store.state("recordings")["unusable_run"] == UNUSABLE_RUN_LIMIT - 1
    assert store.state("recordings")["held_until"] is not None
    with pytest.raises(PollerHeldError):
        poller.poll(store, seconds(30))
    assert len(radio.asked) == UNUSABLE_RUN_LIMIT


def test_audio_that_cannot_be_used_does_not_count_against_ember(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    for number in range(1, 6):
        radio.call(number, seconds(-60 + number), audio=b"<html>Not audio</html>")
    radio.call(6, seconds(-10))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 5)
    assert store.state("recordings")["unusable_run"] == 0


def test_a_poll_hears_no_more_than_its_share_and_says_that_more_are_waiting(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    for number in range(1, MAX_CALLS_PER_POLL + 6):
        radio.call(number, seconds(-100 + number))

    first = poller.poll(store, NOON)

    assert (first.added, first.partial) == (MAX_CALLS_PER_POLL, True)
    # The oldest first, and the rest at the next poll.
    assert refs(store) == [radio.ref(number) for number in range(1, MAX_CALLS_PER_POLL + 1)]
    second = poller.poll(store, seconds(30))
    assert (second.added, second.partial) == (5, False)
    assert len(radio.asked) == MAX_CALLS_PER_POLL + 5


def test_a_call_ember_gave_no_form_for_is_passed_over_and_not_asked_about_again(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30), "Engine 36 is on scene with Jane Doe")
    radio.call(2, seconds(-20))

    report = poller.poll(store, NOON)

    # It may have been paid for. One request for it, and on to the next call.
    assert (report.added, report.unreadable) == (1, 1)
    assert refs(store) == [radio.ref(2)]
    poller.poll(store, seconds(30))
    assert radio.asked == [tone(1), tone(2)]


def test_a_call_whose_row_could_not_be_stored_is_not_paid_for_again(
    store: EvidenceStore, radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio.call(1, seconds(-30))
    poller = build(radio, storage)
    add = EvidenceStore.add

    def no_room(self: EvidenceStore, items: Any, **options: Any) -> Any:
        if list(items):
            raise sqlite3.OperationalError("database or disk is full")
        return add(self, items, **options)

    # Ember answers, and is paid, and the row cannot be written.
    with monkeypatch.context() as patched:
        patched.setattr(EvidenceStore, "add", no_room)
        with pytest.raises(sqlite3.OperationalError):
            poller.poll(store, NOON)
    assert (refs(store), radio.asked) == ([], [tone(1)])

    # Beacon is started again. The call is claimed, and is not asked about again.
    restarted = build(radio, storage)
    report = restarted.poll(EvidenceStore(store.path, "mock"), seconds(30))

    assert (report.added, radio.asked) == (0, [tone(1)])
    assert radio.downloads == [radio.ref(1)]


def test_a_call_another_process_claimed_is_left_to_it(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    other = EvidenceStore(store.path, "mock")
    assert other.claim("recordings", radio.ref(1), now=NOON) is True

    report = poller.poll(store, NOON)

    assert (report.added, refs(store), radio.asked) == (1, [radio.ref(2)], [tone(2)])


def test_when_ember_itself_fails_the_poll_ends_and_the_call_is_not_asked_about_again(
    store: EvidenceStore,
    radio: Radio,
    storage: Storage,
    wall: list[datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20), RuntimeError("the API dropped Jane Doe's call"))
    radio.call(3, seconds(-10))
    poller = build(radio, storage, wall_clock=lambda: wall[0])
    runner = Runner([poller], store, Clock(lambda: wall[0]))

    with caplog.at_level(logging.DEBUG):
        results = runner.step()

    # The call before it is stored. The one after it is not tried while Ember is
    # failing: an Ember that fails on one call likely fails on the next, at a price.
    assert isinstance(results["recordings"], EmberError)
    assert (refs(store), radio.asked) == ([radio.ref(1)], [tone(1), tone(2)])
    assert "The recordings poller failed (EmberError)." in caplog.text
    assert "Jane" not in caplog.text

    # After the wait, the call that failed is left alone and the next one is heard.
    wall[0] = seconds(HELD_INTERVAL_SECONDS)
    assert runner.step()["recordings"].added == 1
    assert refs(store) == [radio.ref(1), radio.ref(3)]
    assert radio.asked == [tone(1), tone(2), tone(3)]


def test_embers_failure_is_raised_with_nothing_under_it(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30), RuntimeError("the API dropped Jane Doe's call"))

    def poll_while_handling_another_error() -> None:
        try:
            raise KeyError("what the caller was handling")
        except KeyError:
            poller.poll(store, NOON)

    with pytest.raises(EmberError) as failure:
        poll_while_handling_another_error()

    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


def test_a_replaced_ledger_keeps_the_claim_and_stops_the_poller_for_the_process(
    store: EvidenceStore,
    radio: Radio,
    storage: Storage,
    wall: list[datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # It can be raised after the paid request was sent, so the call may be paid for.
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    radio.refused[tone(1)] = LedgerReplacedError("The ledger file was replaced.")
    poller = build(radio, storage, wall_clock=lambda: wall[0])

    with caplog.at_level(logging.ERROR), pytest.raises(LedgerReplacedError):
        poller.poll(store, NOON)

    assert "the Ember ledger was replaced" in caplog.text
    assert store.taken("recordings", [radio.ref(1), radio.ref(2)]) == {radio.ref(1)}
    # However long it waits, this process pays for nothing more.
    wall[0] = seconds(30 * 86_400)
    with pytest.raises(PollerHeldError, match="Compare Meta's Usage page with the ledger"):
        poller.poll(store, wall[0])
    assert radio.asked == []
    assert len(radio.listings) == 1


def test_a_poller_that_is_closed_sends_ember_nothing_and_gives_up_its_claim(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    radio.call(1, seconds(-30))
    poller = build(radio, storage)
    poller.close()

    with pytest.raises(EmberClosedError):
        poller.poll(store, NOON)

    # Nothing was sent, so the call is neither paid for nor lost, and the poller
    # is not held: the next run hears it.
    assert (radio.asked, store.taken("recordings", [radio.ref(1)])) == ([], set())
    assert store.state("recordings").get("held_until") is None
    assert poller.drain(0) is True
    assert build(radio, storage).poll(store, seconds(30)).added == 1


class Converter:
    """Stands in for ffmpeg: "converts" a clip to the tone of the same number."""

    def __init__(self) -> None:
        self.converted: list[tuple[bytes, str]] = []
        self.closed = False
        # Whether the host has no memory, process or thread for a conversion.
        self.unavailable = False

    def to_wav(self, audio: bytes, kind: str) -> bytes:
        self.converted.append((audio, kind))
        if self.closed:
            raise ConversionStopped("The converter is closed.")
        if b"broken" in audio:
            raise AudioError("A radio call could not be converted by ffmpeg.")
        if self.unavailable:
            raise ConversionUnavailable("The converter could not be run on this host just now.")
        return tone(audio[-1])

    def close(self) -> None:
        self.closed = True


def m4a(number: int, note: bytes = b"") -> bytes:
    return b"\x00\x00\x00\x1cftypM4A " + note + bytes([number])


def mp3(number: int) -> bytes:
    return b"ID3\x04\x00" + bytes([number])


def test_a_call_that_is_not_wav_is_converted_before_ember_hears_it(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    converter = Converter()
    poller = build(radio, storage, transcoder=converter)
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.call(2, seconds(-20), audio=m4a(2, b"broken"))
    # An MP3 is decoded too, and not sent as it came: nobody would know its length.
    radio.call(3, seconds(-10), audio=mp3(3))
    radio.heard[tone(1)] = radio.heard[tone(3)] = form()

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (2, 1)
    assert converter.converted == [(m4a(1), "m4a"), (m4a(2, b"broken"), "m4a"), (mp3(3), "mp3")]
    # Ember was handed the WAV, and only for the calls that converted.
    assert radio.asked == [tone(1), tone(3)]


def test_a_call_the_host_could_not_convert_just_now_is_not_passed_over(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    converter = Converter()
    poller = build(radio, storage, transcoder=converter)
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.heard[tone(1)] = form()
    converter.unavailable = True

    # The poll ends as it does when a source fails, which is tried again later.
    with pytest.raises(
        SourceError, match=r"^recordings calls could not be converted on this host just now\.$"
    ) as failure:
        poller.poll(store, NOON)

    assert (failure.value.__cause__, failure.value.__context__) == (None, None)
    # Nothing was asked of Ember, and the call was neither taken nor counted out.
    assert (radio.asked, store.taken("recordings", [radio.ref(1)])) == ([], set())
    converter.unavailable = False
    report = poller.poll(store, seconds(30))
    assert (report.added, report.unreadable) == (1, 0)
    assert refs(store) == [radio.ref(1)]


def test_a_conversion_called_off_by_closing_leaves_the_call_for_the_next_run(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    converter = Converter()
    poller = build(radio, storage, transcoder=converter)
    radio.call(1, seconds(-30), audio=m4a(1))
    poller.close()

    with pytest.raises(ConversionStopped):
        poller.poll(store, NOON)

    assert converter.closed is True
    assert (radio.asked, store.taken("recordings", [radio.ref(1)])) == ([], set())


def test_a_call_that_is_not_audio_beacon_can_decode_is_counted_and_not_sent(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    # With no converter only a WAV call can be sent, and a web page never can.
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.call(2, seconds(-20), audio=b"<html>Not found</html>")
    radio.call(3, seconds(-10), audio=b"")
    radio.call(4, seconds(-5), audio=mp3(4))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable, radio.asked) == (0, 4, [])


def test_how_well_a_call_was_heard_is_how_far_its_row_is_trusted(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30), form(clarity="clear"))
    radio.call(2, seconds(-20), form(clarity="partial"))
    radio.call(3, seconds(-10), form(clarity="unintelligible"))

    poller.poll(store, NOON)

    clear, partial, unheard = store.search()
    assert [row.confidence for row in (clear, partial, unheard)] == ["medium", "low", "low"]
    assert (unheard.data["event"], unheard.data["units"], unheard.place) == (
        "unintelligible",
        [],
        None,
    )


def test_a_call_dated_after_now_waits_for_the_poll_after_that_moment(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-10))
    radio.call(2, seconds(20))

    assert poller.poll(store, NOON).added == 1
    assert poller.poll(store, seconds(30)).added == 1
    assert refs(store) == [radio.ref(1), radio.ref(2)]


def test_a_poller_fed_by_a_stand_in_refuses_the_live_store_before_it_asks_anything(
    tmp_path: Path, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30))
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")

    with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
        poller.poll(live, NOON)

    assert (radio.requests, radio.asked, live.search()) == ([], [], [])


def ingest_of(store: EvidenceStore, poller: RecordedRadio, http: SourceHttp) -> Ingest:
    clock = Clock(lambda: NOON)
    return Ingest(store, Runner([poller], store, clock), clock, http, None, [poller])


def test_stopping_waits_for_a_paid_request_that_is_out_and_stores_its_answer(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    out, answer = threading.Event(), threading.Event()

    def slow(body: dict[str, Any]) -> dict[str, Any]:
        out.set()
        assert answer.wait(10)
        return radio.ember(body)

    http = no_network()
    poller = RecordedRadio(radio, EmberClient.fake(storage, slow), talkgroups=(FIRE,), known=KNOWN)
    ingest = ingest_of(store, poller, http)
    ingest.start()
    assert out.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(ingest.stop)
        # It does not return while the request is out, and the store stays open.
        system_time.sleep(0.3)
        assert not stopping.done()
        assert store.closed is False
        answer.set()
        assert stopping.result(10) is True

    # The answer that was paid for is stored. The next call was never sent.
    assert (refs(store), radio.asked) == ([radio.ref(1)], [tone(1)])
    assert store.taken("recordings", [radio.ref(1), radio.ref(2)]) == {radio.ref(1)}
    assert store.closed is True


def test_stopping_while_a_call_is_being_converted_sends_ember_nothing(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.heard[tone(1)] = form()

    class Slow:
        """A converter that is still at it when Beacon stops."""

        def __init__(self) -> None:
            self.began, self.closed = threading.Event(), threading.Event()

        def to_wav(self, audio: bytes, kind: str) -> bytes:
            self.began.set()
            assert self.closed.wait(10)
            raise ConversionStopped("The converter is closed.")

        def close(self) -> None:
            self.closed.set()

    converter = Slow()
    http = no_network()
    poller = RecordedRadio(
        radio,
        EmberClient.fake(storage, radio.ember),
        talkgroups=(FIRE,),
        known=KNOWN,
        transcoder=converter,
    )
    ingest = ingest_of(store, poller, http)
    ingest.start()
    assert converter.began.wait(10)

    assert ingest.stop() is True

    # Nothing was sent, then or after, and the call waits unclaimed for the next run.
    system_time.sleep(0.2)
    assert (radio.asked, refs(store)) == ([], [])
    assert store.taken("recordings", [radio.ref(1)]) == set()
    assert "beacon-ingest" not in [thread.name for thread in threading.enumerate()]


def test_a_stop_that_gives_up_on_a_paid_request_says_so_and_the_call_is_not_paid_for_again(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    out, answer = threading.Event(), threading.Event()

    def slow(body: dict[str, Any]) -> dict[str, Any]:
        out.set()
        assert answer.wait(10)
        return radio.ember(body)

    http = no_network()
    poller = RecordedRadio(radio, EmberClient.fake(storage, slow), talkgroups=(FIRE,), known=KNOWN)
    ingest = ingest_of(store, poller, http)
    ingest.start()
    assert out.wait(10)

    assert ingest.stop(timeout=0.1, paid_timeout=0.1) is False

    # The answer comes after the store was closed. It is not stored, and the call
    # stays claimed: a later run does not pay for it a second time.
    answer.set()
    assert poller.drain(10) is True
    assert (refs(store), radio.asked) == ([], [tone(1)])
    later = EvidenceStore(store.path, "mock")
    restarted = build(radio, storage)
    assert restarted.poll(later, seconds(30)).added == 1
    assert (refs(later), radio.asked) == ([radio.ref(2)], [tone(1), tone(2)])


def test_the_first_poll_looks_at_the_last_five_minutes_of_the_talkgroups_it_hears(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, NOON - OVERLAP - timedelta(seconds=1))
    radio.call(2, NOON - OVERLAP + timedelta(seconds=1))
    radio.call(3, seconds(-10), talkgroup=4242)

    report = poller.poll(store, NOON)

    start = int(NOON.timestamp())
    assert radio.listings == [((start - 300, ""), start)]
    assert (report.added, report.unreadable, report.partial) == (1, 0, False)
    # Only the call it may hear is read at all: not the older one, and not the one
    # on a talkgroup nobody asked for.
    assert (refs(store), radio.read_details) == ([radio.ref(2)], [radio.ref(2)])


def test_a_call_is_heard_once_however_often_it_is_listed(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-10))

    assert poller.poll(store, NOON).added == 2
    radio.call(3, seconds(20))
    assert poller.poll(store, seconds(30)).added == 1
    assert poller.poll(store, seconds(60)).added == 0

    assert refs(store) == [radio.ref(1), radio.ref(2), radio.ref(3)]
    assert radio.asked == [tone(1), tone(2), tone(3)]
    assert radio.downloads == [radio.ref(1), radio.ref(2), radio.ref(3)]
    # Each poll reads again from five minutes before where the one before got to,
    # and a call it has taken is told by its name: not even its JSON is read again.
    start = int(NOON.timestamp())
    assert radio.listings == [
        ((start - 300, ""), start),
        ((start - 300, ""), start + 30),
        ((start - 270, ""), start + 60),
    ]
    assert radio.read_details == [radio.ref(1), radio.ref(2), radio.ref(3)]


def test_calls_that_grew_old_while_the_poller_was_held_are_not_bought_as_a_backlog(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30))
    poller.poll(store, NOON)
    # Three hours of calls nobody heard, one a minute.
    for minute in range(1, 181):
        radio.call(1 + minute, seconds(60 * minute))

    report = poller.poll(store, seconds(180 * 60))

    # Only the last fifteen minutes are looked at: the calls of sixteen minutes.
    assert radio.listings[1][0] == (int((seconds(180 * 60) - MAX_AGE).timestamp()), "")
    assert report.added == 16
    assert refs(store)[1] == radio.ref(166)


def test_a_poller_given_a_backlog_hears_every_call_there_is_oldest_first(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    # A day of recordings, a week old, run through three calls at a poll.
    day = NOON - timedelta(days=7)
    for number in range(1, 8):
        radio.call(number, day + timedelta(hours=number))
    poller = build(radio, storage, backlog=True, calls_per_poll=3)

    reports = [poller.poll(store, NOON) for _ in range(4)]

    assert [(report.added, report.partial) for report in reports] == [
        (3, True),
        (3, True),
        (1, False),
        (0, False),
    ]
    assert refs(store) == [radio.ref(number) for number in range(1, 8)]
    assert len(radio.asked) == 7


def test_a_run_that_is_stopped_goes_on_without_paying_for_a_call_twice(
    tmp_path: Path, radio: Radio, storage: Storage
) -> None:
    day = NOON - timedelta(days=7)
    for number in range(1, 6):
        radio.call(number, day + timedelta(minutes=number))
    store = EvidenceStore(tmp_path / "run.sqlite3", "mock")
    build(radio, storage, backlog=True, calls_per_poll=2).poll(store, NOON)
    # The process dies after it claimed the third call and before its row: a call
    # that may have been paid for.
    assert store.claim("recordings", radio.ref(3), now=NOON)

    # The run is started again on the same store, with nothing in memory.
    again = build(radio, storage, backlog=True, calls_per_poll=2)
    resumed = EvidenceStore(tmp_path / "run.sqlite3", "mock")
    while again.poll(resumed, NOON).partial:
        pass

    # Every call was sent to Ember once at most, and the claimed one never.
    assert radio.asked == [tone(1), tone(2), tone(4), tone(5)]
    assert refs(resumed) == [radio.ref(1), radio.ref(2), radio.ref(4), radio.ref(5)]


def test_more_calls_than_a_poll_looks_at_are_left_for_the_next_and_none_is_skipped(
    store: EvidenceStore, radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recordings_module, "MAX_LISTED", 4)
    monkeypatch.setattr(recordings_module, "MAX_PAGES", 1)
    # Too short to be heard, so each is passed over: ten of them, and one call.
    for number in range(1, 11):
        radio.call(number, seconds(-200 + number), length=0.2)
    radio.call(11, seconds(-100))
    poller = build(radio, storage, calls_per_poll=4)

    reports = [poller.poll(store, NOON) for _ in range(3)]

    # Every poll gets further: it goes on from the place the one before reached,
    # though nothing of those calls was claimed.
    assert [(report.added, report.partial) for report in reports] == [
        (0, True),
        (0, True),
        (1, False),
    ]
    assert [after for after, _ in radio.listings] == [
        (int(seconds(-300).timestamp()), ""),
        radio.filed[radio.ref(4)].place,
        radio.filed[radio.ref(8)].place,
    ]
    assert radio.asked == [tone(11)]
    assert poller.poll(store, NOON).added == 0


def test_a_poll_reads_page_after_page_until_it_has_its_share_of_calls(
    store: EvidenceStore, radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recordings_module, "MAX_LISTED", 4)
    for number in range(1, 11):
        radio.call(number, seconds(-200 + number), length=0.2)
    radio.call(11, seconds(-100))
    poller = build(radio, storage, calls_per_poll=4)

    report = poller.poll(store, NOON)

    # Three pages of four, in one poll: what was passed over took none of its share.
    assert (report.added, report.partial, len(radio.listings)) == (1, False, 3)


def test_calls_that_began_in_the_same_second_do_not_hide_one_another(
    store: EvidenceStore, radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recordings_module, "MAX_LISTED", 4)
    day = NOON - timedelta(days=7)
    # More calls in one second than a page holds, and than a poll hears.
    for number in range(1, 12):
        radio.call(number, day, talkgroup=FIRE if number % 2 else TACTICAL)
    radio.call(12, day + timedelta(seconds=1))
    poller = build(radio, storage, backlog=True, calls_per_poll=4)

    reports = [poller.poll(store, NOON) for _ in range(3)]

    assert [(report.added, report.partial) for report in reports] == [
        (4, True),
        (4, True),
        (4, False),
    ]
    assert len(set(refs(store))) == len(radio.asked) == 12
    # A new process, with every call taken, reads to the end all the same.
    again = build(radio, storage, backlog=True, calls_per_poll=4)
    assert again.poll(store, NOON) == PollReport(added=0)


def test_a_poll_is_held_to_the_audio_it_reads_whatever_came_of_each_call(
    store: EvidenceStore, radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = radio.call(1, seconds(-60))
    whole = radio.json[broken]
    assert isinstance(whole, bytes)
    radio.json[broken] = whole[: len(whole) // 2]
    radio.call(2, seconds(-50))
    radio.call(3, seconds(-40), length=0.2)
    radio.call(4, seconds(-30))
    poller = build(radio, storage, calls_per_poll=1)

    report = poller.poll(store, NOON)

    # A JSON that could not be read cost no audio: the call after it is heard.
    # The one after that is passed over by its JSON, and the last waits.
    assert (report.added, report.unreadable, report.partial) == (1, 1, True)
    assert radio.downloads == [radio.ref(2)]
    assert radio.read_details == [radio.ref(number) for number in (1, 2, 3, 4)]

    # A call another process claims after its audio was read has had its audio read.
    radio.call(5, seconds(-20))
    claim = EvidenceStore.claim

    def lost(self: EvidenceStore, source: str, ref: str, **options: Any) -> bool:
        return ref != radio.ref(4) and claim(self, source, ref, **options)

    monkeypatch.setattr(EvidenceStore, "claim", lost)
    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable, report.partial) == (0, 0, True)
    assert radio.downloads == [radio.ref(2), radio.ref(4)]
    assert radio.asked == [tone(2)]


def test_a_pass_is_counted_over_all_its_polls_and_tells_unready_from_unusable(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    day = NOON - timedelta(days=7)
    silent = radio.call(1, day, audio=GONE)
    for number in (2, 3, 4):
        radio.call(number, day + timedelta(minutes=number))
    radio.call(5, day + timedelta(minutes=5), "Sorry, I could not make that out.")
    poller = build(radio, storage, backlog=True, calls_per_poll=2)
    assert poller.last_pass is None

    reports = [poller.poll(store, NOON) for _ in range(3)]

    # The call that was not ready is in the first poll only, and the pass's last
    # poll is a clean one: the pass, over all its polls, was not.
    assert [(report.added, report.unreadable, report.partial) for report in reports] == [
        (1, 1, True),
        (2, 0, True),
        (0, 1, False),
    ]
    assert poller.last_pass == PassCount(stored=3, unready=1, unusable=1)

    # The next pass looks again at the call nobody claimed, and not at the one
    # that was claimed and came to nothing, which is never asked about again.
    assert poller.poll(store, NOON) == PollReport(added=0, unreadable=1)
    assert poller.last_pass == PassCount(unready=1)
    radio.clips[silent] = tone(1)
    radio.heard[tone(1)] = form()
    assert poller.poll(store, NOON) == PollReport(added=1)
    assert poller.last_pass == PassCount(stored=1)
    assert radio.asked.count(tone(5)) == 1


def test_a_poll_that_fails_adds_nothing_to_its_passes_count(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    day = NOON - timedelta(days=7)
    radio.call(1, day, audio=GONE)
    failing = radio.call(2, day + timedelta(minutes=1), audio=SourceError("recordings", "failed"))
    poller = build(radio, storage, backlog=True)

    with pytest.raises(SourceError):
        poller.poll(store, NOON)
    radio.clips[failing] = tone(2)
    radio.heard[tone(2)] = form()
    report = poller.poll(store, NOON)

    # The pass went on from before the poll that failed, and counts each call once.
    assert (report.added, report.unreadable) == (1, 1)
    assert poller.last_pass == PassCount(stored=1, unready=1)


def test_a_backlog_is_gone_over_again_and_what_a_pass_could_not_use_is_counted_each_time(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    day = NOON - timedelta(days=7)
    broken = radio.call(1, day)
    whole = radio.json[broken]
    assert isinstance(whole, bytes)
    radio.json[broken] = whole[: len(whole) // 2]
    silent = radio.call(2, day + timedelta(minutes=1), audio=GONE)
    radio.call(3, day + timedelta(minutes=2))
    poller = build(radio, storage, backlog=True)

    reports = [poller.poll(store, NOON) for _ in range(2)]

    # Each pass says what it could not use, and claims none of it. Nothing gives a
    # call up: a run is over when a pass counts nothing unreadable.
    assert [(report.added, report.unreadable, report.partial) for report in reports] == [
        (1, 2, False),
        (0, 2, False),
    ]
    assert radio.read_details.count(broken) == 2
    assert store.taken("recordings", [broken, silent]) == set()

    radio.json[broken] = whole
    radio.clips[silent] = tone(2)
    radio.heard[tone(2)] = form()
    # By the process that saw them unready, or by one that is started later.
    assert poller.poll(store, NOON) == PollReport(added=2)
    assert build(radio, storage, backlog=True).poll(store, NOON) == PollReport(added=0)


def test_a_call_whose_json_cannot_be_read_yet_is_counted_and_read_when_it_is_whole(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    ref = radio.call(1, seconds(-30))
    whole = radio.json[ref]
    assert isinstance(whole, bytes)
    # trunk-recorder is still writing it.
    radio.json[ref] = whole[: len(whole) // 2]

    report = poller.poll(store, NOON)

    # Not claimed, and not sent: nothing says how long it is or whether it is encrypted.
    assert (report.added, report.unreadable) == (0, 1)
    assert (radio.asked, store.taken("recordings", [ref])) == ([], set())
    radio.json[ref] = whole
    assert poller.poll(store, seconds(30)).added == 1


def test_an_encrypted_call_is_passed_over_and_not_paid_for(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30), encrypted=1)
    radio.call(2, seconds(-20))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 0)
    assert (radio.asked, radio.downloads) == ([tone(2)], [radio.ref(2)])


def test_audio_that_is_not_there_is_waited_for_and_audio_that_failed_is_tried_again(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    absent = radio.call(1, seconds(-30), audio=GONE)
    failing = radio.call(
        2, seconds(-20), audio=SourceError("recordings", "has a call Beacon could not read")
    )
    radio.call(3, seconds(-10))

    with pytest.raises(SourceError, match="could not read"):
        poller.poll(store, NOON)

    # Nothing was asked of Ember for either, and the second is still waiting.
    assert (refs(store), radio.asked) == ([], [])
    radio.clips[failing] = tone(2)
    radio.heard[tone(2)] = form()
    report = poller.poll(store, seconds(30))
    # The first is still not there: it is counted again, and nobody has claimed it.
    assert (report.added, report.unreadable) == (2, 1)
    assert refs(store) == [radio.ref(2), radio.ref(3)]
    assert radio.downloads == [absent, failing, absent, failing, radio.ref(3)]
    assert store.taken("recordings", [absent]) == set()


def test_a_shelf_that_cannot_be_listed_is_the_sources_failure_and_nothing_is_heard(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    radio.call(1, seconds(-30))
    radio.down = SourceError("recordings", "is a directory Beacon could not list just now")

    with pytest.raises(SourceError, match=r"^recordings is a directory Beacon could not list"):
        poller.poll(store, NOON)

    assert (radio.asked, store.state("recordings")) == ([], {})


def test_a_stand_in_that_lists_what_was_not_asked_for_does_not_get_it_heard(
    store: EvidenceStore, radio: Radio, poller: RecordedRadio
) -> None:
    early, other = filed_at(1, seconds(60)), filed_at(2, seconds(-10), talkgroup=4242)
    radio.call(3, seconds(-20))
    listed = radio.listed
    radio.listed = lambda *asked: ([*listed(*asked)[0], early, other], False)  # type: ignore[method-assign]

    assert poller.poll(store, NOON).added == 1

    assert radio.read_details == [radio.ref(3)]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"talkgroups": ()}, "needs the numbers of the talkgroups"),
        ({"talkgroups": ("1201",)}, "needs the numbers of the talkgroups"),
        ({"talkgroups": (FIRE,), "ems_talkgroups": (EMS,)}, "one of the talkgroups that are heard"),
        ({"calls_per_poll": 0}, "A poll hears from 1 to"),
        ({"calls_per_poll": MAX_LISTED + 1}, "A poll hears from 1 to"),
        ({"calls_per_poll": True}, "A poll hears from 1 to"),
    ],
)
def test_a_poller_that_does_not_know_what_to_hear_cannot_be_made(
    radio: Radio, storage: Storage, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build(radio, storage, **options)


# What a file of trunk-recorder's says of a call.


def test_a_call_is_read_from_its_json_file_with_its_time_and_length() -> None:
    filed = filed_at(7, seconds(-30), TACTICAL)

    call = parse_details(details_of(filed, 6.5, emergency=1), filed)

    assert call is not None
    assert (call.ref, call.system, call.talkgroup) == (
        f"sfd/{TACTICAL}-{int(seconds(-30).timestamp())}.000_851012500-call_7",
        "sfd",
        TACTICAL,
    )
    assert (call.time, call.seconds, call.exact, call.emergency, call.encrypted) == (
        seconds(-30),
        6.5,
        True,
        True,
        False,
    )
    # Nothing of the radios that transmitted, or of anything else the file holds.
    assert "4512345" not in repr(call)
    assert "Jane" not in repr(call)


def test_a_call_is_read_as_trunk_recorder_wrote_it_before_it_kept_milliseconds() -> None:
    at = seconds(-30)
    # As v4 names and describes a call: whole seconds, in the name and in the JSON.
    filed = filed_as("sfd", ("2026", "11", "27"), f"{FIRE}-{int(at.timestamp())}_851012500-call_7")
    assert filed is not None
    body = json.loads(details_of(filed, 7.0))
    for key in ("start_time_ms", "stop_time_ms", "call_length_ms"):
        del body[key]

    call = parse_details(json.dumps(body).encode(), filed)

    assert call is not None
    assert (call.time, call.seconds, call.exact) == (at, 7.0, False)

    # With no length of its own, it is as long as its start and stop say.
    del body["call_length"]
    body["stop_time"] = filed.start_s + 9
    call = parse_details(json.dumps(body).encode(), filed)
    assert call is not None
    assert (call.seconds, call.exact) == (9.0, False)


def test_a_calls_time_is_its_jsons_which_can_be_later_than_its_files_name() -> None:
    # The name is of the first transmission, at 19:59:30.123. That one was too
    # short to keep, and the call's JSON begins with the next, 2.4 seconds on.
    at = seconds(-30) + timedelta(milliseconds=123)
    filed = filed_at(7, at)
    assert filed.stem == f"{FIRE}-{int(at.timestamp())}.123_851012500-call_7"
    later_ms = round(at.timestamp() * 1000) + 2_400

    call = parse_details(
        details_of(filed, start_time=later_ms // 1000, start_time_ms=later_ms), filed
    )

    assert call is not None
    assert call.time == at + timedelta(milliseconds=2_400)
    # It is still found, ordered, and named by its files' name.
    assert (call.ref, call.time_s) == (filed.ref, int(at.timestamp()))


@pytest.mark.parametrize(
    ("late_ms", "read"),
    [
        (-1_000, True),
        (-1_001, False),
        (round(MAX_START_AFTER_NAME.total_seconds() * 1000), True),
        (round(MAX_START_AFTER_NAME.total_seconds() * 1000) + 1, False),
    ],
)
def test_a_json_whose_start_is_not_near_its_files_name_is_another_calls(
    late_ms: int, read: bool
) -> None:
    filed = filed_at(7, seconds(-30))
    start_ms = filed.start_s * 1000 + late_ms

    call = parse_details(details_of(filed, start_time_ms=start_ms), filed)

    assert (call is not None) is read


@pytest.mark.parametrize(
    "changes",
    [
        {"talkgroup": TACTICAL},
        {"talkgroup": str(FIRE)},
        {"start_time_ms": None, "start_time": 1},
        {"start_time_ms": None, "start_time": None},
        {"start_time_ms": 1795809570.5, "start_time": None},
        {"call_length_ms": None, "call_length": None, "stop_time": None},
        {"call_length_ms": None, "call_length": "4", "stop_time": "soon"},
        {"call_length_ms": None, "call_length": -1, "stop_time": 0},
        {"call_length_ms": None, "call_length": float("nan"), "stop_time": float("inf")},
        {"emergency": "yes"},
        {"encrypted": 2},
    ],
)
def test_a_json_file_that_does_not_say_what_its_name_says_or_how_long_is_not_read(
    changes: dict[str, Any],
) -> None:
    filed = filed_at(7, seconds(-30))

    assert parse_details(details_of(filed, **changes), filed) is None


@pytest.mark.parametrize(
    "body", [b"", b"[]", b"null", b'"text"', b"{", b"\xff\xfe", b"[" * 100_000]
)
def test_a_file_that_is_no_json_object_is_not_read(body: bytes) -> None:
    assert parse_details(body, filed_at(7, seconds(-30))) is None


@pytest.mark.parametrize(
    "name",
    [
        # Before v5, and since: milliseconds in the start, and a TDMA slot.
        "1201-1795809570_851012500-call_7",
        "1201-1795809570.123_851012500-call_7",
        "1201-1795809570.123_851012500.1-call_7",
        "1201-1795809570_851012500.000000-call_12345",
        "0-1795809570.000_1-call_0",
    ],
)
def test_a_call_is_told_by_the_name_trunk_recorder_gives_its_files(name: str) -> None:
    filed = filed_as("sfd", ("2026", "11", "27"), name)

    assert filed is not None
    assert (filed.talkgroup in (1201, 0), filed.start_s, filed.ref) == (
        True,
        1795809570,
        f"sfd/{name}",
    )


@pytest.mark.parametrize(
    ("system", "name"),
    [
        ("sfd", "notes"),
        ("sfd", "1201-1795809570_851012500"),
        ("sfd", "1201-1795809570_851012500-call_7.bak"),
        ("sfd", "1201-17_851012500-call_7"),
        ("sfd", "1201-1795809570._851012500-call_7"),
        ("sfd", "1201-1795809570.1234_851012500-call_7"),
        ("sfd", "1201-1795809570.123.4_851012500-call_7"),
        ("sfd", "-1201-1795809570_851012500-call_7"),
        ("sfd", "1201-1795809570_851012500-call_7\n"),
        ("sfd", "Jane Doe-1795809570_851012500-call_7"),
        ("../sfd", "1201-1795809570_851012500-call_7"),
        ("", "1201-1795809570_851012500-call_7"),
        ("s f d", "1201-1795809570_851012500-call_7"),
    ],
)
def test_any_other_name_is_no_call(system: str, name: str) -> None:
    assert filed_as(system, ("2026", "11", "27"), name) is None


# A directory of recordings, as trunk-recorder leaves them.


def put(
    root: Path, filed: Filed, audio: bytes | None, *, suffix: str = ".wav", **changes: Any
) -> None:
    """Write a call's files where trunk-recorder would: tones, never a recording."""
    where = root.joinpath(filed.system, *filed.day)
    where.mkdir(parents=True, exist_ok=True)
    (where / f"{filed.stem}.json").write_bytes(details_of(filed, **changes))
    if audio is not None:
        (where / f"{filed.stem}{suffix}").write_bytes(audio)


@pytest.fixture
def calls(tmp_path: Path) -> Path:
    root = tmp_path / "calls"
    root.mkdir()
    return root


def test_a_directory_lists_the_calls_asked_for_by_their_files_names(calls: Path) -> None:
    first, second = filed_at(1, seconds(-30)), filed_at(2, seconds(-20), TACTICAL)
    put(calls, first, tone(1))
    put(calls, second, tone(2))
    put(calls, filed_at(3, seconds(-10), 4242), tone(3))
    put(calls, filed_at(4, seconds(-400)), tone(4))
    put(calls, filed_at(5, seconds(60)), tone(5))
    day = calls / "sfd" / "2026" / "11" / "27"
    (day / "notes.json").write_text("{}")
    (day / f"{FIRE}-{first.start_s}_1-call_9.wav").write_bytes(tone(9))
    shelf = Directory(calls, SYSTEM)

    start = int(NOON.timestamp())
    listed, more = shelf.listed((start - 300, ""), start, {FIRE, TACTICAL}, 10)

    assert (listed, more) == ([first, second], False)
    assert shelf.details(first) == details_of(first)
    assert shelf.audio(second) == tone(2)
    # Oldest first, no more than was asked for, and then what comes after a place.
    assert shelf.listed((start - 300, ""), start, {FIRE, TACTICAL}, 1) == ([first], True)
    assert shelf.listed(first.place, start, {FIRE, TACTICAL}, 1) == ([second], False)
    assert shelf.listed(second.place, start, {FIRE, TACTICAL}, 1) == ([], False)


def test_a_directory_is_listed_a_page_at_a_time_across_days_and_within_one_second(
    calls: Path,
) -> None:
    # Seven calls that began in one second, one a day before, and one a day after.
    same = [filed_at(number, seconds(-30), FIRE if number % 2 else TACTICAL) for number in range(7)]
    before = filed_at(8, seconds(-30) - timedelta(days=1))
    after = filed_at(9, seconds(-30) + timedelta(days=1))
    for filed in (*same, before, after):
        put(calls, filed, tone(1))
    shelf = Directory(calls, SYSTEM)

    pages, place, more = [], BEGINNING, True
    while more:
        page, more = shelf.listed(place, after.start_s, {FIRE, TACTICAL}, 3)
        pages.append(page)
        place = page[-1].place

    assert [len(page) for page in pages] == [3, 3, 3]
    assert [call for page in pages for call in page] == [
        before,
        *sorted(same, key=lambda call: call.stem),
        after,
    ]


def test_a_day_with_more_files_than_are_listed_fails_the_poll_and_is_not_read_in_part(
    calls: Path,
    store: EvidenceStore,
    radio: Radio,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (calls / GENERATED_MARK).write_text("Generated audio. No radio.")
    for number in (1, 2):
        put(calls, filed_at(number, seconds(-30 + number)), tone(number))
        radio.heard[tone(number)] = form()
    monkeypatch.setattr(recordings_module, "_MAX_DAY_ENTRIES", 3)
    poller = RecordedRadio(
        Directory(calls, SYSTEM),
        EmberClient.fake(storage, radio.ember),
        talkgroups=(FIRE,),
        known=KNOWN,
    )

    with pytest.raises(SourceError) as failure:
        poller.poll(store, NOON)

    # The calls it did reach are not heard as though they were all there are.
    assert str(failure.value) == (
        "recordings has a day with more files than Beacon lists, and is not read in part."
    )
    assert (failure.value.__context__, failure.value.__cause__) == (None, None)
    assert (radio.asked, refs(store)) == ([], [])
    monkeypatch.setattr(recordings_module, "_MAX_DAY_ENTRIES", 4)
    assert poller.poll(store, NOON).added == 2


def test_a_directory_is_read_for_one_system_and_no_other(calls: Path) -> None:
    ours = filed_at(1, seconds(-30))
    # Another system's call on the talkgroup of the same number: another channel.
    theirs = Filed("sfpd", ours.day, ours.stem, ours.talkgroup, ours.start_s)
    put(calls, ours, tone(1))
    put(calls, theirs, tone(2))
    shelf = Directory(calls, SYSTEM)

    start = int(NOON.timestamp())
    assert shelf.listed(BEGINNING, start, {FIRE}, 10) == ([ours], False)
    assert (shelf.details(theirs), shelf.audio(theirs)) == (None, None)
    assert Directory(calls, "sfpd").listed(BEGINNING, start, {FIRE}, 10) == ([theirs], False)
    # A system with no directory yet has no calls, which is no failure.
    assert Directory(calls, "later").listed(BEGINNING, start, {FIRE}, 10) == ([], False)


def test_a_directory_put_in_the_place_of_the_one_that_was_opened_is_not_read(
    calls: Path, tmp_path: Path
) -> None:
    first, second = filed_at(1, seconds(-30)), filed_at(2, seconds(-20))
    put(calls, first, tone(1))
    shelf = Directory(calls, SYSTEM)
    # The directory is moved away, and another is at its path: marked as generated,
    # where the one that was opened is taken for recordings of real radio.
    calls.rename(tmp_path / "moved")
    calls.mkdir()
    (calls / GENERATED_MARK).write_text("Generated audio. No radio.")
    put(calls, second, tone(2))

    start = int(NOON.timestamp())
    assert shelf.generated is False
    assert shelf.listed(BEGINNING, start, {FIRE}, 10) == ([first], False)
    assert (shelf.audio(first), shelf.audio(second)) == (tone(1), None)


def test_a_directory_whose_mark_changes_while_it_is_read_is_read_no_further(
    calls: Path, store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    mark = calls / GENERATED_MARK
    mark.write_text("Generated audio. No radio.")
    put(calls, filed_at(1, seconds(-30)), tone(1))
    radio.heard[tone(1)] = form()
    poller = RecordedRadio(
        Directory(calls, SYSTEM),
        EmberClient.fake(storage, radio.ember),
        talkgroups=(FIRE,),
        known=KNOWN,
    )
    unmarked_once = "mark as generated audio has changed since Beacon began to read it"

    # Unmarked now, which would make what a stand-in hears a real call's row; and
    # then something of the mark's name that says nothing either way.
    mark.unlink()
    with pytest.raises(SourceError, match=unmarked_once) as failure:
        poller.poll(store, NOON)
    mark.mkdir()
    with pytest.raises(SourceError, match=unmarked_once):
        poller.poll(store, NOON)

    assert (failure.value.__context__, failure.value.__cause__) == (None, None)
    assert (radio.asked, refs(store)) == ([], [])
    # And the other way: recordings that are marked after a reader began.
    mark.rmdir()
    recorded = Directory(calls, SYSTEM)
    mark.write_text("Generated audio. No radio.")
    with pytest.raises(SourceError, match=unmarked_once):
        recorded.listed(BEGINNING, int(NOON.timestamp()), {FIRE}, 10)


def test_a_directory_marked_between_its_listing_and_a_files_reading_hands_on_no_bytes(
    calls: Path,
    store: EvidenceStore,
    radio: Radio,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mark = calls / GENERATED_MARK
    filed = filed_at(1, seconds(-30))
    put(calls, filed, tone(1))
    recorded = Directory(calls, SYSTEM)
    start = int(NOON.timestamp())
    assert recorded.listed(BEGINNING, start, {FIRE}, 10) == ([filed], False)

    # Marked as generated after the listing: what is read now is not handed on as
    # a recording of real radio, which is what this reader was opened as.
    mark.write_text("Generated audio. No radio.")
    for read in (recorded.details, recorded.audio):
        with pytest.raises(SourceError, match="mark as generated audio has changed") as failure:
            read(filed)
        assert (failure.value.__context__, failure.value.__cause__) == (None, None)

    # And the other way, under a poller, with the change made while a file is read.
    generated = Directory(calls, SYSTEM)
    radio.heard[tone(1)] = form()
    poller = RecordedRadio(
        generated, EmberClient.fake(storage, radio.ember), talkgroups=(FIRE,), known=KNOWN
    )
    read_file = recordings_module._file

    def unmarked_meanwhile(directory: int, name: str, most: int) -> bytes | None:
        body = read_file(directory, name, most)
        if name.endswith(".wav"):
            mark.unlink()
        return body

    monkeypatch.setattr(recordings_module, "_file", unmarked_meanwhile)
    with pytest.raises(SourceError, match="mark as generated audio has changed"):
        poller.poll(store, NOON)

    # Nothing was claimed, and nothing was sent.
    assert (radio.asked, refs(store), store.taken("recordings", [filed.ref])) == ([], [], set())


def test_a_listing_opens_no_year_and_no_month_outside_the_days_it_is_of(
    calls: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filed = filed_at(1, seconds(-30))
    put(calls, filed, tone(1))
    for old in (("2019", "3", "5"), ("2026", "8", "1"), ("2031", "1", "1")):
        calls.joinpath(SYSTEM, *old).mkdir(parents=True)
    shelf = Directory(calls, SYSTEM)
    opened_as = recordings_module._opened
    entered: list[tuple[str, ...]] = []

    def watching(above: int, *names: str) -> Any:
        entered.append(names)
        return opened_as(above, *names)

    monkeypatch.setattr(recordings_module, "_opened", watching)
    start = int(NOON.timestamp())

    assert shelf.listed((start - 300, ""), start, {FIRE}, 10) == ([filed], False)
    # An old year that cannot be read just now would otherwise stop today's poll.
    assert entered == [(SYSTEM,), ("2026",), ("11",), ("2026", "11", "27")]


def test_a_mark_that_cannot_be_looked_at_is_not_taken_for_no_mark(
    calls: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (calls / GENERATED_MARK).write_text("Generated audio. No radio.")
    generated = Directory(calls, SYSTEM)
    looked = os.stat

    def refuse(path: Any, *args: Any, **options: Any) -> Any:
        if path == GENERATED_MARK:
            raise PermissionError(13, "Permission denied", str(path))
        return looked(path, *args, **options)

    monkeypatch.setattr(recordings_module.os, "stat", refuse)

    # Not when a directory is first opened, and not at a later listing.
    with pytest.raises(ValueError, match="It cannot be told whether the directory is marked"):
        Directory(calls, SYSTEM)
    with pytest.raises(SourceError, match="mark as generated audio has changed"):
        generated.listed(BEGINNING, int(NOON.timestamp()), {FIRE}, 10)


def test_a_call_is_found_whatever_day_the_recorders_own_time_put_it_under(calls: Path) -> None:
    # Eight in the evening in UTC is noon in San Francisco, and nine at night on
    # the 27th there is the 28th in UTC: a recorder files by its own time.
    late = filed_at(1, datetime(2026, 11, 28, 5, 0, tzinfo=UTC))
    filed_local = Filed(late.system, ("2026", "11", "27"), late.stem, late.talkgroup, late.start_s)
    put(calls, filed_local, tone(1))
    # And one whose directories are written with a zero, as some copies are.
    early = filed_at(2, datetime(2026, 3, 5, 12, 0, tzinfo=UTC))
    padded = calls / "sfd" / "2026" / "03" / "05"
    padded.mkdir(parents=True)
    (padded / f"{early.stem}.json").write_bytes(details_of(early))
    (padded / f"{early.stem}.wav").write_bytes(tone(2))
    shelf = Directory(calls, SYSTEM)

    (found,), _ = shelf.listed((late.start_s - 1, ""), late.start_s, {FIRE}, 10)
    (old,), _ = shelf.listed((early.start_s - 1, ""), early.start_s, {FIRE}, 10)

    assert (found, shelf.audio(found)) == (filed_local, tone(1))
    assert (shelf.details(old), shelf.audio(old)) == (details_of(early), tone(2))
    # A whole archive, from the first call there is.
    everything, _ = shelf.listed(BEGINNING, int(NOON.timestamp()) + 86_400, {FIRE}, 10)
    assert everything == [old, found]


def test_a_directory_reads_no_file_through_a_link_and_none_that_is_too_long(
    calls: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret.wav"
    secret.write_bytes(tone(9))
    linked, long, plain = (filed_at(number, seconds(-30 + number)) for number in (1, 2, 3))
    put(calls, linked, None)
    put(calls, long, tone(2))
    put(calls, plain, tone(3))
    day = calls / "sfd" / "2026" / "11" / "27"
    os.symlink(secret, day / f"{linked.stem}.wav")
    # A JSON file that is a link is not listed, and a system that is one is not entered.
    other = filed_at(4, seconds(-5))
    os.symlink(day / f"{plain.stem}.json", day / f"{other.stem}.json")
    elsewhere = tmp_path / "elsewhere"
    put(elsewhere, filed_at(5, seconds(-4)), tone(5))
    os.symlink(elsewhere / "sfd", calls / "linked")
    monkeypatch.setattr(recordings_module, "_MAX_AUDIO_BYTES", len(tone(3)))
    (day / f"{long.stem}.wav").write_bytes(tone(2) + b"\0")
    shelf = Directory(calls, SYSTEM)

    start = int(NOON.timestamp())
    listed, _ = shelf.listed((start - 300, ""), start, {FIRE}, 10)

    assert listed == [linked, long, plain]
    assert (shelf.audio(linked), shelf.audio(long), shelf.audio(plain)) == (None, None, tone(3))
    # A directory where a file should be is no file either.
    (day / f"{other.stem}.wav").mkdir()
    assert shelf.audio(other) is None


def test_a_call_in_another_container_is_found_when_there_is_no_wav(calls: Path) -> None:
    filed = filed_at(1, seconds(-30))
    put(calls, filed, m4a(1), suffix=".m4a")

    assert Directory(calls, SYSTEM).audio(filed) == m4a(1)


def test_a_directory_that_cannot_be_listed_fails_with_a_fixed_sentence(
    calls: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(calls, filed_at(1, seconds(-30)), tone(1))
    shelf = Directory(calls, SYSTEM)

    def refuse(path: Any) -> Any:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(recordings_module.os, "scandir", refuse)

    with pytest.raises(SourceError) as failure:
        shelf.listed(BEGINNING, int(NOON.timestamp()), {FIRE}, 10)

    # Nothing of the path, and nothing under the error.
    assert str(failure.value) == "recordings is a directory Beacon could not list just now."
    assert (failure.value.__context__, failure.value.__cause__) == (None, None)


def test_a_file_that_cannot_be_read_just_now_fails_the_poll_and_names_no_path(
    calls: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filed = filed_at(1, seconds(-30))
    put(calls, filed, tone(1))
    shelf = Directory(calls, SYSTEM)
    opened = os.open

    def refuse(path: Any, *args: Any, **options: Any) -> int:
        if str(path).endswith(".wav"):
            raise PermissionError(13, "Permission denied", str(path))
        return opened(path, *args, **options)

    monkeypatch.setattr(recordings_module.os, "open", refuse)

    with pytest.raises(SourceError) as failure:
        shelf.audio(filed)

    assert str(failure.value) == "recordings has a recorded call Beacon could not read just now."
    assert (failure.value.__context__, failure.value.__cause__) == (None, None)
    assert shelf.details(filed) == details_of(filed)


@pytest.mark.parametrize("where", ["calls", "", "missing"])
def test_a_directory_is_named_by_an_absolute_path_that_is_one(tmp_path: Path, where: str) -> None:
    (tmp_path / "file").write_text("no directory")
    path = Path(where) if where != "missing" else tmp_path / "file"

    with pytest.raises(ValueError, match="named by its absolute path"):
        Directory(path, SYSTEM)


def test_a_link_to_a_directory_is_not_opened_and_a_system_is_named_by_its_short_name(
    calls: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(calls)

    with pytest.raises(ValueError, match="named by its absolute path"):
        Directory(link, SYSTEM)
    for system in ("", "..", "sfd/2026", "s f d", "x" * 41):
        with pytest.raises(ValueError, match="named by its short name"):
            Directory(calls, system)


def live_ember(
    storage: Storage, label: str | None = "ingest:recordings", **options: Any
) -> EmberClient:
    scope = None if label is None else SpendScope(label, 250_000_000)
    return EmberClient.live(storage, api_key="test-key", scope=scope, **options)


def test_what_a_shelf_is_decides_whose_its_rows_are_and_who_may_hear_them(
    calls: Path, radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    fake = lambda: EmberClient.fake(storage, radio.ember)  # noqa: E731
    replay = lambda: EmberClient.replay_only(storage)  # noqa: E731
    refused = "Recorded radio is heard by a live Ember"

    # A directory with no mark is recordings of real radio: live rows, and only a
    # live Ember, which would otherwise invent what a real call said.
    recorded = Directory(calls, SYSTEM)
    assert (
        RecordedRadio(recorded, live_ember(storage), talkgroups=(FIRE,), known=KNOWN).origin
        == "live"
    )
    for ember in (fake(), replay()):
        with pytest.raises(ValueError, match=refused):
            RecordedRadio(recorded, ember, talkgroups=(FIRE,), known=KNOWN)

    # A stand-in's tones are a stand-in's whoever reads them, and are not paid for.
    assert build(radio, storage).origin == "mock"
    for ember in (live_ember(storage), replay()):
        with pytest.raises(ValueError, match=refused):
            RecordedRadio(radio, ember, talkgroups=(FIRE,), known=KNOWN)

    # A directory marked as generated holds no radio: mock rows whoever hears them,
    # and either may. A client that only replays hears nothing at all.
    (calls / GENERATED_MARK).write_text("Generated audio. No radio.")
    generated = Directory(calls, SYSTEM)
    for ember in (fake(), live_ember(storage)):
        poller = RecordedRadio(generated, ember, talkgroups=(FIRE,), known=KNOWN)
        assert poller.origin == "mock"
        with pytest.raises(AttributeError):
            poller.origin = "live"  # type: ignore[misc]
    with pytest.raises(ValueError, match=refused):
        RecordedRadio(generated, replay(), talkgroups=(FIRE,), known=KNOWN)
    # The mark is a file. A directory or a link of that name says nothing either
    # way, and a directory that cannot be told apart is not read as either.
    (calls / GENERATED_MARK).unlink()
    for unclear in ("directory", "link"):
        if unclear == "directory":
            (calls / GENERATED_MARK).mkdir()
        else:
            (calls / GENERATED_MARK).rmdir()
            (calls / GENERATED_MARK).symlink_to(calls / "sfd")
        with pytest.raises(ValueError, match="cannot be told whether the directory is marked"):
            Directory(calls, SYSTEM)
    # Only the reader of a directory itself is one of recordings: a class made
    # from it is a stand-in, however it reads.
    (calls / GENERATED_MARK).unlink()

    class Dressed(Directory):
        generated = False

    dressed = Dressed(calls, SYSTEM)
    assert RecordedRadio(dressed, fake(), talkgroups=(FIRE,), known=KNOWN).origin == "mock"
    with pytest.raises(ValueError, match=refused):
        RecordedRadio(dressed, live_ember(storage), talkgroups=(FIRE,), known=KNOWN)

    # A client that tries a call again after a 429 would send a claimed call twice.
    paced = live_ember(storage, pacer=Pacer(3000))
    with pytest.raises(ValueError, match="makes each call once: no pacer"):
        RecordedRadio(recorded, paced, talkgroups=(FIRE,), known=KNOWN)


def test_a_radio_poller_that_can_spend_pays_under_radios_own_budget(
    calls: Path, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")

    # With no budget of its own, and with the one set aside for reading notices.
    for label in (None, "ingest:notices"):
        with pytest.raises(ValueError, match="needs the spending scope ingest:recordings"):
            RecordedRadio(
                Directory(calls, SYSTEM),
                live_ember(storage, label),
                talkgroups=(FIRE,),
                known=KNOWN,
            )


def test_generated_calls_in_a_directory_are_heard_and_stored_as_a_stand_ins(
    calls: Path, store: EvidenceStore, radio: Radio, storage: Storage, tmp_path: Path
) -> None:
    (calls / GENERATED_MARK).write_text("Generated audio. No radio.")
    filed = filed_at(1, seconds(-30))
    put(calls, filed, tone(1, 6.5), length=6.5)
    radio.heard[tone(1, 6.5)] = form(alarm=2)
    poller = RecordedRadio(
        Directory(calls, SYSTEM),
        EmberClient.fake(storage, radio.ember),
        talkgroups=(FIRE,),
        known=KNOWN,
    )

    report = poller.poll(store, NOON)

    (row,) = store.search()
    assert report.added == 1
    assert (row.source, row.ref, row.occurred_at) == ("recordings", filed.ref, seconds(-30))
    assert (row.origin, row.model, row.stand_in) == ("mock", "fake", True)
    assert (row.data["system"], row.data["talkgroup"], row.data["seconds"]) == ("sfd", FIRE, 6.5)
    # The recording is the operator's: nothing in the directory is changed.
    day = calls / "sfd" / "2026" / "11" / "27"
    assert sorted(path.name for path in day.iterdir()) == [
        f"{filed.stem}.json",
        f"{filed.stem}.wav",
    ]
    assert (day / f"{filed.stem}.wav").read_bytes() == tone(1, 6.5)
    # And the live store takes none of it.
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
        poller.poll(live, NOON)


def test_a_file_is_read_only_under_the_directory_whatever_names_it_is_asked_by(
    calls: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    elsewhere = filed_at(1, seconds(-30))
    put(outside, elsewhere, tone(1))
    put(calls, filed_at(2, seconds(-20)), tone(2))
    shelf = Directory(calls, SYSTEM)
    # A day that has become a link to a directory outside, since it was listed.
    day = calls / "sfd" / "2026" / "11"
    os.symlink(outside.joinpath("sfd", "2026", "11", "27"), day / "28")
    linked = Filed("sfd", ("2026", "11", "28"), elsewhere.stem, FIRE, elsewhere.start_s)
    # And names no listing of a directory gives, which climb out of it.
    climbing = [
        Filed("..", ("outside", "sfd", "2026"), elsewhere.stem, FIRE, elsewhere.start_s),
        Filed("sfd", ("2026", "11", "../../../../outside/sfd/2026/11/27"), elsewhere.stem, FIRE, 1),
        Filed("sfd", ("2026", "11", "27"), f"../../../../../outside/{elsewhere.stem}", FIRE, 1),
        Filed("sfd", ("2026", "11", "27"), elsewhere.stem, TACTICAL, elsewhere.start_s),
    ]

    for filed in (linked, *climbing):
        assert (shelf.details(filed), shelf.audio(filed)) == (None, None)
