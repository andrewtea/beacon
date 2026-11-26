"""The OpenMHz radio poller, against hand-written answers and generated tones."""

import base64
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import BudgetExceededError, EmberClient, EmberError, Storage, fake_response
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.ingest import Clock, Runner, SourceError
from beacon.ingest import openmhz as openmhz_module
from beacon.ingest.http import SourceHttp
from beacon.ingest.openmhz import (
    HELD_INTERVAL_SECONDS,
    INTERVAL_SECONDS,
    MAX_CALLS_PER_POLL,
    OpenMhzRadio,
    parse_calls,
)
from beacon.radio import AudioError
from beacon.radio.audio import pcm_to_wav

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
FIRE, TACTICAL, EMS = 1201, 1203, 1301

PRIVATE = ("Jane", "Doe", "555-0100", "chest pain", "unconscious", "year old")


def seconds(count: float) -> datetime:
    return NOON + timedelta(seconds=count)


def tone(number: int) -> bytes:
    """A WAV file no other call has. Nothing in these tests is a recording."""
    return pcm_to_wav(number.to_bytes(2, "little") * 800)


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
            listed = [
                call
                for call in self.calls
                if datetime.fromisoformat(call["time"]).timestamp() * 1000 > after
                and call["talkgroupNum"] in wanted
            ]
            # Newest first, as its site lists them.
            return httpx2.Response(200, json={"calls": listed[::-1], "direction": "newer"})
        clip = self.audio.get(str(url), 404)
        if isinstance(clip, int):
            return httpx2.Response(clip, text=f"No audio for Jane Doe at {url}")
        return httpx2.Response(200, content=clip)

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


def build(radio: Radio, storage: Storage, **options: Any) -> OpenMhzRadio:
    return OpenMhzRadio(
        SourceHttp(httpx2.MockTransport(radio.http)),
        EmberClient.fake(storage, radio.ember),
        **{"talkgroups": (FIRE, TACTICAL, EMS), "ems_talkgroups": (EMS,)} | options,
    )


@pytest.fixture
def poller(radio: Radio, storage: Storage) -> OpenMhzRadio:
    return build(radio, storage)


def refs(store: EvidenceStore) -> list[str]:
    return [row.ref for row in store.search()]


def test_the_first_poll_asks_for_the_last_two_minutes_of_the_talkgroups_it_hears(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    report = poller.poll(store, NOON)

    (request,) = radio.requests
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "api.openmhz.com",
        "/sfp25/calls/newer",
    )
    two_minutes_ago = round((NOON - timedelta(minutes=2)).timestamp() * 1000)
    assert dict(request.url.params) == {
        "time": str(two_minutes_ago - 1),
        "filter-type": "talkgroup",
        "filter-code": "1201,1203,1301",
    }
    assert (report.added, report.unreadable, report.partial) == (0, 0, False)
    assert store.state("openmhz") == {}


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
            units=["M52", "Jane Doe"],
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
    runner = Runner([poller], store, Clock(lambda: NOON))

    with caplog.at_level(logging.DEBUG):
        results = runner.step()

    assert (results["openmhz"].added, results["openmhz"].unreadable) == (2, 2)
    medical, on_ems_channel = store.search()
    for row in (medical, on_ems_channel):
        assert (row.data["event"], row.data["status"], row.data["details"]) == (
            "medical response",
            "unknown",
            [],
        )
    assert (medical.place, medical.data["units"]) == ("1400 block of Hayes St", ["M52"])
    assert (medical.data["block"], medical.data["cross_street"]) == (1400, None)
    on_disk = "\n".join(
        path.read_bytes().decode("latin-1") for path in tmp_path.rglob("*") if path.is_file()
    )
    with sqlite3.connect(store.path) as db:
        stored = json.dumps(db.execute("SELECT * FROM evidence").fetchall())
        stored += json.dumps(db.execute("SELECT * FROM poller_state").fetchall())
    for word in PRIVATE:
        assert word not in on_disk
        assert word not in stored
        assert word not in caplog.text
    assert "1458" not in stored
    # The audio is not kept, as it came or as it was sent to Ember.
    assert tone(1)[:64].decode("latin-1") not in on_disk
    assert base64.b64encode(tone(1)).decode()[:200] not in on_disk
    # Nothing is on disk but the evidence store and the Ember ledger.
    assert {path.name for path in tmp_path.rglob("*") if path.is_file()} <= {
        "evidence.sqlite3",
        "evidence.sqlite3-shm",
        "evidence.sqlite3-wal",
        "ledger.sqlite3",
    }


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
    # Each poll asks from where the last one got to.
    assert [int(request.url.params["time"]) for request in radio.listings[1:]] == [
        round(seconds(-10).timestamp() * 1000) - 1,
        round(seconds(20).timestamp() * 1000) - 1,
    ]


def test_calls_of_the_same_millisecond_are_each_heard_once(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-30), talkgroup=TACTICAL)
    radio.call(3, seconds(-30), talkgroup=EMS)
    hear = openmhz_module.extract_transmission

    def stopped_after_the_first(ember: EmberClient, audio: bytes, *args: Any, **options: Any):
        if audio != tone(1):
            raise BudgetExceededError("The spending stop was reached.")
        return hear(ember, audio, *args, **options)

    # The first poll ends after one of the three.
    with monkeypatch.context() as patched:
        patched.setattr(openmhz_module, "extract_transmission", stopped_after_the_first)
        with pytest.raises(BudgetExceededError):
            poller.poll(store, NOON)
    assert refs(store) == ["call0001"]

    assert poller.poll(store, seconds(30)).added == 2

    assert sorted(refs(store)) == ["call0001", "call0002", "call0003"]
    assert radio.asked == [tone(1), tone(2), tone(3)]
    assert store.state("openmhz") == {
        "after_ms": round(seconds(-30).timestamp() * 1000),
        "done": ["call0001", "call0002", "call0003"],
    }


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


def test_a_spending_stop_ends_the_poll_and_holds_the_poller_without_losing_a_call(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20))
    hear = openmhz_module.extract_transmission
    stopped = [True]

    def at_the_stop(ember: EmberClient, audio: bytes, *args: Any, **options: Any):
        if stopped[0] and audio == tone(2):
            raise BudgetExceededError("The spending stop was reached.")
        return hear(ember, audio, *args, **options)

    monkeypatch.setattr(openmhz_module, "extract_transmission", at_the_stop)
    clock = [NOON]
    runner = Runner([poller], store, Clock(lambda: clock[0]))

    results = runner.step()

    # The call before the stop is stored, and the poller leaves Ember alone a while.
    assert isinstance(results["openmhz"], BudgetExceededError)
    assert refs(store) == ["call0001"]
    assert poller.interval_seconds == HELD_INTERVAL_SECONDS
    clock[0] = seconds(HELD_INTERVAL_SECONDS - 1)
    assert runner.step() == {}

    # The stop is lifted. The call it refused was never sent, and is heard now.
    stopped[0] = False
    clock[0] = seconds(HELD_INTERVAL_SECONDS)
    assert runner.step()["openmhz"].added == 1
    assert refs(store) == ["call0001", "call0002"]
    assert radio.asked == [tone(1), tone(2)]
    assert poller.interval_seconds == INTERVAL_SECONDS


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
    asked_from = int(radio.listings[-1].url.params["time"])
    assert asked_from == round(seconds(165 * 60).timestamp() * 1000) - 1
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


def test_when_ember_itself_fails_the_poll_ends_and_the_call_is_not_asked_about_again(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio, caplog: pytest.LogCaptureFixture
) -> None:
    radio.call(1, seconds(-30))
    radio.call(2, seconds(-20), RuntimeError("the API dropped Jane Doe's call"))
    radio.call(3, seconds(-10))
    clock = [NOON]
    runner = Runner([poller], store, Clock(lambda: clock[0]))

    with caplog.at_level(logging.DEBUG):
        results = runner.step()

    # The call before it is stored. The one after it is not tried while Ember is
    # failing: an Ember that fails on one call likely fails on the next, at a price.
    assert isinstance(results["openmhz"], EmberError)
    assert (refs(store), radio.asked) == (["call0001"], [tone(1), tone(2)])
    assert poller.interval_seconds == HELD_INTERVAL_SECONDS
    assert "The openmhz poller failed (EmberError)." in caplog.text
    assert "Jane" not in caplog.text

    # After the wait, the call that failed is left alone and the next one is heard.
    clock[0] = seconds(HELD_INTERVAL_SECONDS)
    assert runner.step()["openmhz"].added == 1
    assert refs(store) == ["call0001", "call0003"]
    assert radio.asked == [tone(1), tone(2), tone(3)]


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
        self.converted: list[bytes] = []

    def to_wav(self, m4a: bytes) -> bytes:
        self.converted.append(m4a)
        if b"broken" in m4a:
            raise AudioError("A radio call could not be converted by ffmpeg.")
        return tone(m4a[-1])


def m4a(number: int, note: bytes = b"") -> bytes:
    return b"\x00\x00\x00\x1cftypM4A " + note + bytes([number])


def test_an_m4a_call_is_converted_before_ember_hears_it(
    store: EvidenceStore, radio: Radio, storage: Storage
) -> None:
    converter = Converter()
    poller = build(radio, storage, transcoder=converter)
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.call(2, seconds(-20), audio=m4a(2, b"broken"))
    radio.heard[tone(1)] = form()

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable) == (1, 1)
    assert converter.converted == [m4a(1), m4a(2, b"broken")]
    # Ember was handed the WAV, and only for the call that converted.
    assert radio.asked == [tone(1)]


def test_a_call_that_is_not_audio_ember_takes_is_counted_and_not_sent(
    store: EvidenceStore, radio: Radio, poller: OpenMhzRadio
) -> None:
    # With no converter an M4A call cannot be sent, and a web page never can.
    radio.call(1, seconds(-30), audio=m4a(1))
    radio.call(2, seconds(-20), audio=b"<html>Not found</html>")
    radio.call(3, seconds(-10), audio=b"")

    report = poller.poll(store, NOON)

    assert (report.added, report.unreadable, radio.asked) == (0, 3, [])


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
        down, EmberClient.fake(storage, lambda body: fake_response()), talkgroups=(FIRE,)
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
        OpenMhzRadio(SourceHttp(), fake, talkgroups=(FIRE,))
    with pytest.raises(ValueError, match="Real radio is heard by a live Ember"):
        OpenMhzRadio(SourceHttp(), EmberClient.replay_only(storage), talkgroups=(FIRE,))

    # And a stand-in's tones are not paid for.
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    live = EmberClient.live(storage, api_key="test-key")
    with pytest.raises(ValueError, match="a stand-in's by a fake one"):
        OpenMhzRadio(SourceHttp(httpx2.MockTransport(radio.http)), live, talkgroups=(FIRE,))


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
