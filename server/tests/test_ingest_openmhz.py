"""The OpenMHz radio poller, against hand-written answers and generated tones."""

import base64
import io
import json
import logging
import sqlite3
import threading
import time as system_time
import wave
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
    Storage,
    fake_response,
)
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.ingest import Clock, Ingest, Runner, SourceError
from beacon.ingest import openmhz as openmhz_module
from beacon.ingest.http import SourceHttp
from beacon.ingest.openmhz import (
    HELD_INTERVAL_SECONDS,
    MAX_CALLS_PER_POLL,
    OVERLAP,
    SCAN_PAGES,
    UNUSABLE_RUN_LIMIT,
    OpenMhzRadio,
    parse_calls,
)
from beacon.ingest.runner import PollerHeldError
from beacon.radio import NOTHING_KNOWN, AudioError, ConversionStopped, Known
from beacon.radio.audio import pcm_to_wav

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
FIRE, TACTICAL, EMS = 1201, 1203, 1301

PRIVATE = ("Jane", "Doe", "555-0100", "chest pain", "unconscious", "year old")
# The streets and the units these tests' radio may name.
KNOWN = Known.of(streets=["Hayes St", "Gough St"], units=["E36", "T6", "B2", "M52"])


def seconds(count: float) -> datetime:
    return NOON + timedelta(seconds=count)


def ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def tone(number: int) -> bytes:
    """A WAV file no other call has, a second long. Nothing in these tests is a
    recording."""
    return pcm_to_wav(number.to_bytes(2, "little") * 16_000)


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


class Radio:
    """Stands in for OpenMHz and for Ember. `call` puts a call on the air: it is
    listed, its audio is served, and Ember "hears" in it the form it is given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.audio: dict[str, bytes | int] = {}
        self.heard: dict[bytes, Any] = {}
        self.requests: list[httpx2.Request] = []
        # The Ember requests, by the audio each carried.
        self.asked: list[bytes] = []
        # How many calls an answer lists at most: the ones nearest after the time asked.
        self.page_size = 1000
        # What Ember's client refuses a call with before anything is sent, by its audio.
        self.refused: dict[bytes, Exception] = {}
        # When a call was made, for a call whose own `time` field does not say.
        self.times: dict[str, datetime] = {}

    def call(
        self,
        number: int,
        at: datetime,
        heard: Any = None,
        *,
        talkgroup: int = FIRE,
        length: float = 4.0,
        audio: bytes | int | None = None,
        **changes: Any,
    ) -> str:
        ref = f"call{number:04d}"
        clip = tone(number) if audio is None else audio
        url = f"https://media.openmhz.com/sfp25/{ref}.m4a"
        self.calls.append(
            {
                "_id": ref,
                "talkgroupNum": talkgroup,
                "url": url,
                "filename": f"/sfp25/{ref}.m4a",
                "time": at.isoformat().replace("+00:00", "Z"),
                "srcList": [{"pos": 0, "src": 4512345}],
                "star": 0,
                "freq": 852_112_500,
                "len": length,
                "emergency": False,
                "patches": [],
            }
            | changes
        )
        self.audio[self.calls[-1]["url"]] = clip
        if isinstance(clip, bytes):
            self.heard[clip] = form() if heard is None else heard
        return ref

    def http(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "api.openmhz.com":
            after = int(url.params["time"])
            wanted = {int(code) for code in url.params["filter-code"].split(",")}
            listed = sorted(
                (
                    call
                    for call in self.calls
                    if ms(self.made(call)) > after and call["talkgroupNum"] in wanted
                ),
                key=self.made,
            )[: self.page_size]
            # Newest first, as its site lists them.
            return httpx2.Response(200, json={"calls": listed[::-1], "direction": "newer"})
        clip = self.audio.get(str(url), 404)
        if isinstance(clip, int):
            return httpx2.Response(clip, text=f"No audio for Jane Doe at {url}")
        return httpx2.Response(200, content=clip)

    def made(self, call: dict[str, Any]) -> datetime:
        """When a call was made, which is how the stand-in orders its listing,
        whatever the call's own `time` field was changed to say."""
        return self.times.get(call["_id"]) or datetime.fromisoformat(call["time"])

    def ember(self, body: dict[str, Any]) -> dict[str, Any]:
        (message,) = body["input"]
        clip = base64.b64decode(message["content"][1]["input_audio"]["data"])
        self.asked.append(clip)
        heard = self.heard[clip]
        if isinstance(heard, Exception):
            raise heard
        return fake_response(text=heard if isinstance(heard, str) else json.dumps(heard))

    @property
    def listings(self) -> list[httpx2.Request]:
        return [request for request in self.requests if request.url.host == "api.openmhz.com"]

    @property
    def downloads(self) -> list[str]:
        return [
            request.url.path.rsplit("/", 1)[-1].removesuffix(".m4a")
            for request in self.requests
            if request.url.host != "api.openmhz.com"
        ]


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
    hear = openmhz_module.extract_transmission

    def hearing(ember: EmberClient, audio: bytes, *args: Any, **options: Any) -> Any:
        if audio in radio.refused:
            raise radio.refused[audio]
        return hear(ember, audio, *args, **options)

    monkeypatch.setattr(openmhz_module, "extract_transmission", hearing)


def build(radio: Radio, storage: Storage, responder: Any = None, **options: Any) -> OpenMhzRadio:
    return OpenMhzRadio(
        SourceHttp(httpx2.MockTransport(radio.http)),
        EmberClient.fake(storage, radio.ember if responder is None else responder),
        **{"talkgroups": (FIRE, TACTICAL, EMS), "ems_talkgroups": (EMS,), "known": KNOWN} | options,
    )


@pytest.fixture
def wall() -> list[datetime]:
    """The real time, as a poller is told it, which a test moves."""
    return [NOON]


@pytest.fixture
def poller(radio: Radio, storage: Storage) -> OpenMhzRadio:
    return build(radio, storage)


def refs(store: EvidenceStore) -> list[str]:
    return [row.ref for row in store.search()]


def test_the_first_poll_asks_for_the_last_five_minutes_of_the_talkgroups_it_hears(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    report = poller.poll(store, NOON)

    (request,) = radio.requests
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "api.openmhz.com",
        "/sfp25/calls/newer",
    )
    assert dict(request.url.params) == {
        "time": str(ms(NOON - OVERLAP) - 1),
        "filter-type": "talkgroup",
        "filter-code": "1201,1203,1301",
    }
    assert (report.added, report.unreadable, report.partial) == (0, 0, False)


def test_a_call_becomes_a_row_of_what_ember_heard_in_it_and_nothing_it_said(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    ref = radio.call(1, seconds(-30), form(details=["working_fire"], alarm=2), length=6.5)

    report = poller.poll(store, NOON)

    (row,) = store.search()
    assert report.added == 1
    assert (row.kind, row.source, row.ref) == ("radio", "openmhz", ref)
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
        "system": "sfp25",
        "talkgroup": FIRE,
        "seconds": 6.5,
        "emergency": False,
    }
    assert radio.asked == [tone(1)]


def test_what_a_call_named_reaches_no_row_no_file_and_no_log(
    store: EvidenceStore,
    radio: Radio,
    poller: OpenMhzRadio,
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

    assert (results["openmhz"].added, results["openmhz"].unreadable) == (4, 2)
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


def test_a_call_is_heard_once_however_often_it_is_listed(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-10))

    assert poller.poll(store, NOON).added == 2
    radio.call(3, seconds(20))
    assert poller.poll(store, seconds(30)).added == 1
    assert poller.poll(store, seconds(60)).added == 0

    assert refs(store) == ["call0001", "call0002", "call0003"]
    assert radio.asked == [tone(1), tone(2), tone(3)]
    assert radio.downloads == ["call0001", "call0002", "call0003"]
    # Each poll reads again from five minutes before the newest call it finished
    # with, and then asks for the moment of the newest call it was shown once more:
    # a page shorter than the one before shows that nothing follows.
    assert [int(request.url.params["time"]) for request in radio.listings] == [
        ms(NOON - OVERLAP) - 1,
        ms(seconds(-10)) - 1,
        ms(seconds(-10) - OVERLAP) - 1,
        ms(seconds(20)) - 1,
        ms(seconds(20) - OVERLAP) - 1,
        ms(seconds(20)) - 1,
    ]


def test_a_call_that_is_listed_late_is_still_heard(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-10))
    assert poller.poll(store, NOON).added == 1

    # Its upload reached OpenMHz after a later call's did: it is first listed two
    # seconds behind where the poller had got to.
    radio.call(2, seconds(-12))
    report = poller.poll(store, seconds(30))

    assert (report.added, report.partial) == (1, False)
    assert refs(store) == ["call0002", "call0001"]
    assert radio.asked == [tone(1), tone(2)]


def test_calls_already_taken_take_no_place_among_the_calls_a_poll_may_hear(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    for number in range(1, MAX_CALLS_PER_POLL + 1):
        radio.call(number, seconds(-100 + number))
    assert poller.poll(store, NOON).added == MAX_CALLS_PER_POLL

    # Every one of those is listed again, with one new call behind them.
    radio.call(99, seconds(10))
    report = poller.poll(store, seconds(30))

    assert (report.added, report.partial) == (1, False)
    assert len(radio.asked) == MAX_CALLS_PER_POLL + 1


def test_a_listing_is_read_in_pages_until_one_shows_that_nothing_follows(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.page_size = 2
    for number in range(1, 6):
        radio.call(number, seconds(-60 + number))

    report = poller.poll(store, NOON)

    assert (report.added, report.partial) == (5, False)
    # Each page asks for the moment of the newest call of the page before again,
    # and the last, with one call where two would fit, ends the listing.
    assert [int(request.url.params["time"]) for request in radio.listings] == [
        ms(NOON - OVERLAP) - 1,
        ms(seconds(-58)) - 1,
        ms(seconds(-57)) - 1,
        ms(seconds(-56)) - 1,
        ms(seconds(-55)) - 1,
    ]


def test_a_listing_longer_than_a_poll_reads_is_partial_and_every_poll_moves_forward(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.page_size = 2
    for number in range(1, 25):
        radio.call(number, seconds(-60 + number))

    polls = [poller.poll(store, seconds(30 * count)) for count in range(4)]

    # A poll reads at most twelve pages, half of them from where it had got to
    # before, so reading the overlap again never keeps it from the calls after it.
    assert [(poll.added, poll.partial) for poll in polls] == [
        (13, True),
        (6, True),
        (5, True),
        (0, True),
    ]
    assert max(len(radio.listings[start : start + 12]) for start in range(0, 48, 12)) <= 12
    assert len(radio.listings) <= 4 * 2 * SCAN_PAGES
    assert refs(store) == [f"call{number:04d}" for number in range(1, 25)]
    assert len(radio.asked) == 24


def test_calls_that_share_a_moment_are_all_heard_though_a_page_ends_among_them(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.page_size = 4
    radio.call(1, seconds(-50))
    radio.call(2, seconds(-40))
    radio.call(3, seconds(-30))
    radio.call(4, seconds(-30), talkgroup=TACTICAL)
    radio.call(5, seconds(-30), talkgroup=EMS)
    radio.call(6, seconds(-20))

    report = poller.poll(store, NOON)

    # The first page ends inside the moment three calls share. Asking for that
    # moment again shows all three and the call after them, which proves that no
    # more of them are hidden.
    assert (report.added, report.partial) == (6, False)
    assert len(radio.asked) == 6


def test_a_page_full_of_one_moment_is_reported_as_partial(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.page_size = 2
    for number, talkgroup in enumerate((FIRE, TACTICAL, EMS, FIRE), start=1):
        radio.call(number, seconds(-30), talkgroup=talkgroup)

    report = poller.poll(store, NOON)

    # Two of the four are all the listing will show, and nothing says so but a
    # page as full as a page gets, of calls of one moment. That is not "all read".
    assert (report.added, report.partial) == (2, True)

    # A later call does not prove it either: it only shows that the page was full.
    radio.call(9, seconds(10))
    report = poller.poll(store, seconds(30))
    assert (report.added, report.partial) == (1, True)


def test_a_page_of_calls_beacon_cannot_use_does_not_hide_the_calls_after_it(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.page_size = 2
    elsewhere = "https://calls.example.org/sfp25/call.m4a"
    radio.call(1, seconds(-50), url=elsewhere)
    radio.call(2, seconds(-40), url=elsewhere)
    radio.call(3, seconds(-30))
    radio.call(4, seconds(-20))

    report = poller.poll(store, NOON)

    # How far a page reaches is read from the times of what it lists, usable or
    # not, and each unusable item is counted once however often it is listed.
    assert (report.added, report.unreadable, report.partial) == (2, 2, False)
    assert refs(store) == ["call0003", "call0004"]


def test_a_page_that_does_not_say_how_far_it_reaches_is_reported_as_partial(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.page_size = 1
    radio.call(1, seconds(-30), time="half past seven")
    radio.call(2, seconds(-20))
    # The stand-in lists by the time it was given, so the item with no time is first.
    radio.times = {"call0001": seconds(-30)}

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable, report.partial) == (0, 1, True)


def test_a_call_too_short_or_too_long_to_be_worth_hearing_is_not_paid_for(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-50), length=0.4)
    radio.call(2, seconds(-40), length=0)
    radio.call(3, seconds(-30), length=121)
    radio.call(4, seconds(-20), length=1.0)

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 0)
    assert (refs(store), radio.downloads, radio.asked) == (["call0004"], ["call0004"], [tone(4)])
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


def test_a_call_is_measured_by_its_audio_and_not_by_what_its_listing_says(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    # Listed as four seconds each. One plays for three minutes, and one for a
    # twentieth of a second.
    radio.call(1, seconds(-30), audio=three_minutes())
    radio.call(2, seconds(-20), audio=pcm_to_wav(b"\x02\x00" * 800))
    radio.call(3, seconds(-10))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 2)
    assert radio.asked == [tone(3)]
    # Neither is fetched again.
    poller.poll(store, seconds(30))
    assert radio.downloads == ["call0001", "call0002", "call0003"]


def test_ember_is_sent_the_samples_of_a_wav_call_and_nothing_else_the_file_carried(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
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
    assert isinstance(results["openmhz"], BudgetExceededError)
    assert refs(store) == ["call0001"]
    wall[0] = seconds(HELD_INTERVAL_SECONDS - 1)
    held = runner.step()["openmhz"]
    assert isinstance(held, PollerHeldError)
    assert held.retry_after == 1.0
    # Held, it asks OpenMHz nothing either.
    assert len(radio.listings) == 2
    assert runner.step() == {}

    # The stop is lifted. The call it refused was never sent, and is heard now.
    radio.refused.clear()
    wall[0] = seconds(HELD_INTERVAL_SECONDS + 30)
    assert runner.step()["openmhz"].added == 1
    assert refs(store) == ["call0001", "call0002"]
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

    assert store.state("openmhz")["held_until"] == seconds(240 + HELD_INTERVAL_SECONDS).isoformat()
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
    assert (report.added, refs(store)) == (1, ["call0007"])
    assert store.state("openmhz")["unusable_run"] == 0


def test_audio_that_cannot_be_used_does_not_count_against_ember(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    for number in range(1, 6):
        radio.call(number, seconds(-60 + number), audio=b"<html>Not audio</html>")
    radio.call(6, seconds(-10))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 5)
    assert store.state("openmhz")["unusable_run"] == 0


def test_calls_that_grew_old_while_the_poller_was_held_are_not_bought_as_a_backlog(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-30))
    poller.poll(store, NOON)
    # Three hours of calls nobody heard, one a minute.
    for minute in range(1, 181):
        radio.call(1 + minute, seconds(60 * minute))

    report = poller.poll(store, seconds(180 * 60))

    # Only the last fifteen minutes are asked for.
    asked_from = int(radio.listings[3].url.params["time"])
    assert asked_from == ms(seconds(165 * 60)) - 1
    assert report.added == 16
    assert refs(store)[1] == "call0166"


def test_a_poll_hears_no_more_than_its_share_and_says_that_more_are_waiting(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    for number in range(1, MAX_CALLS_PER_POLL + 6):
        radio.call(number, seconds(-100 + number))

    first = poller.poll(store, NOON)

    assert (first.added, first.partial) == (MAX_CALLS_PER_POLL, True)
    # The oldest first, and the rest at the next poll.
    assert refs(store) == [f"call{number:04d}" for number in range(1, MAX_CALLS_PER_POLL + 1)]
    second = poller.poll(store, seconds(30))
    assert (second.added, second.partial) == (5, False)
    assert len(radio.asked) == MAX_CALLS_PER_POLL + 5


def test_a_call_ember_gave_no_form_for_is_passed_over_and_not_asked_about_again(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-30), "Engine 36 is on scene with Jane Doe")
    radio.call(2, seconds(-20))

    report = poller.poll(store, NOON)

    # It may have been paid for. One request for it, and on to the next call.
    assert (report.added, report.unreadable) == (1, 1)
    assert refs(store) == ["call0002"]
    poller.poll(store, seconds(30))
    assert radio.asked == [tone(1), tone(2)]


def test_a_call_a_listing_names_twice_is_paid_for_once(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    # Twice in one answer, and Ember's answer about it is no form, so no row says
    # that it was asked about.
    radio.call(1, seconds(-30), "A sentence, and no form.")
    radio.calls.append(dict(radio.calls[0]))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (0, 1)
    assert radio.asked == [tone(1)]


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
    assert radio.downloads == ["call0001"]


def test_a_call_another_process_claimed_is_left_to_it(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    other = EvidenceStore(store.path, "mock")
    assert other.claim("openmhz", "call0001", now=NOON) is True

    report = poller.poll(store, NOON)

    assert (report.added, refs(store), radio.asked) == (1, ["call0002"], [tone(2)])


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
    assert isinstance(results["openmhz"], EmberError)
    assert (refs(store), radio.asked) == (["call0001"], [tone(1), tone(2)])
    assert "The openmhz poller failed (EmberError)." in caplog.text
    assert "Jane" not in caplog.text

    # After the wait, the call that failed is left alone and the next one is heard.
    wall[0] = seconds(HELD_INTERVAL_SECONDS)
    assert runner.step()["openmhz"].added == 1
    assert refs(store) == ["call0001", "call0003"]
    assert radio.asked == [tone(1), tone(2), tone(3)]


def test_embers_failure_is_raised_with_nothing_under_it(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
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
    assert store.taken("openmhz", ["call0001", "call0002"]) == {"call0001"}
    # However long it waits, this process pays for nothing more.
    wall[0] = seconds(30 * 86_400)
    with pytest.raises(PollerHeldError, match="Compare Meta's Usage page with the ledger"):
        poller.poll(store, wall[0])
    assert radio.asked == []
    assert len(radio.listings) == 2


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
    assert (radio.asked, store.taken("openmhz", ["call0001"])) == ([], set())
    assert store.state("openmhz").get("held_until") is None
    assert poller.drain(0) is True
    assert build(radio, storage).poll(store, seconds(30)).added == 1


def test_audio_that_is_gone_is_passed_over_and_audio_that_failed_is_tried_again(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-30), audio=404)
    radio.call(2, seconds(-20), audio=503)
    radio.call(3, seconds(-10))

    with pytest.raises(SourceError, match=r"^openmhz answered HTTP 503\.$"):
        poller.poll(store, NOON)

    # Nothing was asked of Ember for either, and the second is still waiting.
    assert (refs(store), radio.asked) == ([], [])
    radio.audio["https://media.openmhz.com/sfp25/call0002.m4a"] = tone(2)
    radio.heard[tone(2)] = form()
    report = poller.poll(store, seconds(30))
    assert (report.added, report.unreadable) == (2, 0)
    assert refs(store) == ["call0002", "call0003"]
    assert radio.downloads == ["call0001", "call0002", "call0002", "call0003"]


class Converter:
    """Stands in for ffmpeg: "converts" a clip to the tone of the same number."""

    def __init__(self) -> None:
        self.converted: list[tuple[bytes, str]] = []
        self.closed = False

    def to_wav(self, audio: bytes, kind: str) -> bytes:
        self.converted.append((audio, kind))
        if self.closed:
            raise ConversionStopped("The converter is closed.")
        if b"broken" in audio:
            raise AudioError("A radio call could not be converted by ffmpeg.")
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
    assert (radio.asked, store.taken("openmhz", ["call0001"])) == ([], set())


def test_a_call_that_is_not_audio_beacon_can_decode_is_counted_and_not_sent(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    # With no converter only a WAV call can be sent, and a web page never can.
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.call(2, seconds(-20), audio=b"<html>Not found</html>")
    radio.call(3, seconds(-10), audio=b"")
    radio.call(4, seconds(-5), audio=mp3(4))

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable, radio.asked) == (0, 4, [])


def test_how_well_a_call_was_heard_is_how_far_its_row_is_trusted(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
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


def answer(*calls: Any) -> dict[str, Any]:
    return {"calls": list(calls), "direction": "newer"}


def listed(**changes: Any) -> dict[str, Any]:
    call = {
        "_id": "65f1c0ffee",
        "talkgroupNum": FIRE,
        "url": "https://media.openmhz.com/sfp25/1201-1795723200.m4a",
        "time": "2026-11-27T20:00:00.000Z",
        "len": 4,
        "emergency": True,
    }
    return call | changes


def test_a_call_is_read_from_an_answer_with_its_time_length_and_address() -> None:
    (call,), unreadable = parse_calls(answer(listed()), {FIRE})

    assert unreadable == 0
    assert (call.ref, call.talkgroup, call.time, call.seconds, call.emergency) == (
        "65f1c0ffee",
        FIRE,
        NOON,
        4.0,
        True,
    )
    assert call.url == "https://media.openmhz.com/sfp25/1201-1795723200.m4a"


def test_a_call_on_a_talkgroup_that_is_not_heard_is_left_out_and_not_counted() -> None:
    assert parse_calls(answer(listed(talkgroupNum=4400)), {FIRE}) == ([], 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"_id": None},
        {"_id": "../../etc/passwd"},
        {"_id": "x" * 65},
        {"talkgroupNum": "1201"},
        {"talkgroupNum": True},
        {"time": "noon"},
        {"time": 1_795_723_200},
        # A time with no zone is no moment.
        {"time": "2026-11-27T20:00:00"},
        {"len": "4"},
        {"len": -1},
        {"len": float("inf")},
        {"len": True},
        {"url": None},
    ],
)
def test_a_call_beacon_cannot_read_is_counted(changes: dict[str, Any]) -> None:
    assert parse_calls(answer(listed(**changes), "not a call"), {FIRE}) == ([], 2)


@pytest.mark.parametrize(
    "url",
    [
        # Not HTTPS, another host, a host that only looks like OpenMHz's, a port, a
        # user, and addresses that point into this machine or its network.
        "http://media.openmhz.com/sfp25/call.m4a",
        "https://example.org/sfp25/call.m4a",
        "https://media.openmhz.com.example.org/call.m4a",
        "https://notopenmhz.com/call.m4a",
        "https://media.openmhz.com:8443/call.m4a",
        "https://user@media.openmhz.com/call.m4a",
        "https://127.0.0.1/call.m4a",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/hostname",
        "//media.openmhz.com/call.m4a",
        "https://[::1/call.m4a",
        "https://media.openmhz.com/" + "a" * 500,
    ],
)
def test_audio_is_fetched_only_from_openmhz_or_a_host_the_operator_named(url: str) -> None:
    assert parse_calls(answer(listed(url=url)), {FIRE}) == ([], 1)


def test_a_host_the_operator_named_may_serve_audio() -> None:
    elsewhere = "https://s3.example.org/openmhz/call.m4a"

    assert parse_calls(answer(listed(url=elsewhere)), {FIRE}) == ([], 1)
    (call,), unreadable = parse_calls(answer(listed(url=elsewhere)), {FIRE}, {"s3.example.org"})
    assert (call.url, unreadable) == (elsewhere, 0)
    for own in ("https://openmhz.com/a.m4a", "https://API.OpenMHz.com/a.m4a"):
        assert len(parse_calls(answer(listed(url=own)), {FIRE})[0]) == 1


@pytest.mark.parametrize("payload", [[], {"calls": None}, {"direction": "newer"}, "text", None])
def test_an_answer_that_is_not_a_calls_object_is_an_error(payload: Any) -> None:
    with pytest.raises(SourceError, match=r"^openmhz sent a reply Beacon cannot read\.$"):
        parse_calls(payload, {FIRE})


def test_a_call_dated_after_now_waits_for_the_poll_after_that_moment(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-10))
    radio.call(2, seconds(20))

    assert poller.poll(store, NOON).added == 1
    assert poller.poll(store, seconds(30)).added == 1
    assert refs(store) == ["call0001", "call0002"]


def test_a_listing_that_cannot_be_read_is_the_sources_failure(
    store: EvidenceStore, storage: Storage
) -> None:
    down = SourceHttp(httpx2.MockTransport(lambda request: httpx2.Response(502)))
    poller = OpenMhzRadio(
        down,
        EmberClient.fake(storage, lambda body: fake_response()),
        talkgroups=(FIRE,),
        known=KNOWN,
    )

    with pytest.raises(SourceError, match=r"^openmhz answered HTTP 502\.$"):
        poller.poll(store, NOON)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"talkgroups": ()}, "needs the numbers of the talkgroups"),
        ({"talkgroups": ("1201",)}, "needs the numbers of the talkgroups"),
        ({"talkgroups": (FIRE,), "ems_talkgroups": (EMS,)}, "one of the talkgroups that are heard"),
        ({"system": "SF P25/../x"}, "named by its short name"),
    ],
)
def test_a_poller_that_does_not_know_what_to_hear_cannot_be_made(
    radio: Radio, storage: Storage, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build(radio, storage, **options)


def test_real_radio_is_not_handed_to_a_stand_in_for_ember(
    radio: Radio, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = EmberClient.fake(storage, radio.ember)

    # The network, and an Ember that would invent what a real call said.
    with pytest.raises(ValueError, match="Real radio is heard by a live Ember"):
        OpenMhzRadio(SourceHttp(), fake, talkgroups=(FIRE,), known=KNOWN)
    with pytest.raises(ValueError, match="Real radio is heard by a live Ember"):
        OpenMhzRadio(
            SourceHttp(), EmberClient.replay_only(storage), talkgroups=(FIRE,), known=KNOWN
        )

    # And a stand-in's tones are not paid for.
    stand_in = SourceHttp(httpx2.MockTransport(radio.http))
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    live = EmberClient.live(storage, api_key="test-key")
    with pytest.raises(ValueError, match="a stand-in's by a fake one"):
        OpenMhzRadio(stand_in, live, talkgroups=(FIRE,), known=KNOWN)
    # A client that only replays hears nothing at all: a radio call is never recorded.
    with pytest.raises(ValueError, match="a stand-in's by a fake one"):
        OpenMhzRadio(stand_in, EmberClient.replay_only(storage), talkgroups=(FIRE,), known=KNOWN)
    # A client that tries a call again after a 429 would send a claimed call twice.
    paced = EmberClient.live(storage, api_key="test-key", pacer=Pacer(3000))
    with pytest.raises(ValueError, match="makes each call once: no pacer"):
        OpenMhzRadio(SourceHttp(), paced, talkgroups=(FIRE,), known=KNOWN)


def test_the_pollers_origin_is_its_clients_and_cannot_be_set(poller: OpenMhzRadio) -> None:
    assert poller.origin == "mock"
    with pytest.raises(AttributeError):
        poller.origin = "live"  # type: ignore[misc]


def test_a_poller_fed_by_a_stand_in_refuses_the_live_store_before_it_asks_anything(
    tmp_path: Path, radio: Radio, poller: OpenMhzRadio
) -> None:
    radio.call(1, seconds(-30))
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")

    with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
        poller.poll(live, NOON)

    assert (radio.requests, radio.asked, live.search()) == ([], [], [])


def ingest_of(store: EvidenceStore, poller: OpenMhzRadio, http: SourceHttp) -> Ingest:
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

    http = SourceHttp(httpx2.MockTransport(radio.http))
    poller = OpenMhzRadio(http, EmberClient.fake(storage, slow), talkgroups=(FIRE,), known=KNOWN)
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
    assert (refs(store), radio.asked) == (["call0001"], [tone(1)])
    assert store.taken("openmhz", ["call0001", "call0002"]) == {"call0001"}
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
    http = SourceHttp(httpx2.MockTransport(radio.http))
    poller = OpenMhzRadio(
        http,
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
    assert store.taken("openmhz", ["call0001"]) == set()
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

    http = SourceHttp(httpx2.MockTransport(radio.http))
    poller = OpenMhzRadio(http, EmberClient.fake(storage, slow), talkgroups=(FIRE,), known=KNOWN)
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
    assert (refs(later), radio.asked) == (["call0002"], [tone(1), tone(2)])
