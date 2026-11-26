"""The runner: each poller when it is due, and one failing does not stop the rest."""

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beacon.evidence import EvidenceStore
from beacon.ingest import Clock, PollerStatus, PollReport, Runner, SourceError

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)


def seconds(count: float) -> datetime:
    return NOON + timedelta(seconds=count)


class FakePoller:
    """Records when it was polled, and fails when the next of `failures` is an error."""

    def __init__(self, name: str, interval_seconds: float) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.polled: list[datetime] = []
        self.failures: list[Exception | None] = []

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        self.polled.append(now)
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        return PollReport(added=len(self.polled))


class Time:
    def __init__(self) -> None:
        self.moment = NOON

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "mock")


@pytest.fixture
def time() -> Time:
    return Time()


def test_a_poller_runs_at_once_and_then_each_time_its_interval_has_passed(
    store: EvidenceStore, time: Time
) -> None:
    fast, slow = FakePoller("fast", 30), FakePoller("slow", 120)
    runner = Runner([fast, slow], store, Clock(time))

    stepped = []
    for count in (0, 29, 30, 59, 60, 90, 119, 120):
        time.moment = seconds(count)
        stepped.append(sorted(runner.step()))

    assert stepped == [["fast", "slow"], [], ["fast"], [], ["fast"], ["fast"], [], ["fast", "slow"]]
    assert fast.polled == [seconds(0), seconds(30), seconds(60), seconds(90), seconds(120)]
    assert slow.polled == [seconds(0), seconds(120)]


def test_a_source_that_cannot_be_read_does_not_stop_the_others_and_is_tried_again(
    store: EvidenceStore, time: Time, caplog: pytest.LogCaptureFixture
) -> None:
    down, fine = FakePoller("down", 30), FakePoller("fine", 30)
    down.failures = [SourceError("down", "answered HTTP 503", status=503)]
    runner = Runner([down, fine], store, Clock(time))

    results = runner.step()

    assert isinstance(results["down"], SourceError)
    assert results["fine"] == PollReport(added=1)
    assert "down answered HTTP 503." in caplog.text
    assert runner.status() == [
        PollerStatus("down", NOON, False),
        PollerStatus("fine", NOON, True),
    ]

    time.moment = seconds(30)
    assert runner.step() == {"down": PollReport(added=2), "fine": PollReport(added=2)}
    assert runner.status()[0] == PollerStatus("down", seconds(30), True)


def test_a_source_that_asks_for_a_longer_wait_than_the_interval_gets_it(
    store: EvidenceStore, time: Time
) -> None:
    limited, patient = FakePoller("limited", 30), FakePoller("patient", 300)
    limited.failures = [SourceError("limited", "answered HTTP 429", status=429, retry_after=600)]
    # A wait shorter than the interval does not bring the next poll forward.
    patient.failures = [SourceError("patient", "answered HTTP 429", status=429, retry_after=5)]
    runner = Runner([limited, patient], store, Clock(time))
    runner.step()

    for count in (30, 299, 300, 599, 600):
        time.moment = seconds(count)
        runner.step()

    assert limited.polled == [seconds(0), seconds(600)]
    assert patient.polled == [seconds(0), seconds(300), seconds(600)]


def test_an_unexpected_failure_is_logged_by_its_kind_and_nothing_of_its_text(
    store: EvidenceStore, time: Time, caplog: pytest.LogCaptureFixture
) -> None:
    radio, fine = FakePoller("radio", 30), FakePoller("fine", 30)
    radio.failures = [ValueError("Medic 52 to 1458 Hayes St for Jane Doe")]
    runner = Runner([radio, fine], store, Clock(time))

    with caplog.at_level(logging.DEBUG):
        results = runner.step()

    assert isinstance(results["radio"], ValueError)
    assert results["fine"] == PollReport(added=1)
    assert "The radio poller failed (ValueError)." in caplog.text
    for word in ("Medic", "Hayes", "Jane", "Traceback"):
        assert word not in caplog.text
    assert runner.status()[0] == PollerStatus("radio", NOON, False)


def test_a_poller_that_has_not_polled_yet_has_no_last_poll(
    store: EvidenceStore, time: Time
) -> None:
    runner = Runner([FakePoller("nws", 30)], store, Clock(time))

    assert runner.status() == [PollerStatus("nws", None, None)]


def test_two_pollers_cannot_share_a_name(store: EvidenceStore, time: Time) -> None:
    with pytest.raises(ValueError, match="same name"):
        Runner([FakePoller("nws", 30), FakePoller("nws", 60)], store, Clock(time))


def test_catching_up_polls_a_past_span_as_often_as_running_through_it_would_have(
    store: EvidenceStore, time: Time
) -> None:
    fast, slow = FakePoller("fast", 30), FakePoller("slow", 120)
    clock = Clock(time)
    runner = Runner([fast, slow], store, clock)
    time.moment = seconds(10_000)

    runner.catch_up(seconds(0), seconds(125))

    assert fast.polled == [seconds(0), seconds(30), seconds(60), seconds(90), seconds(120)]
    assert slow.polled == [seconds(0), seconds(120)]
    # The clock is let go afterwards, and the pollers are due by the real time.
    assert clock.now() == seconds(10_000)
    assert sorted(runner.step()) == ["fast", "slow"]


def test_catching_up_leaves_off_when_it_is_told_to_stop(store: EvidenceStore, time: Time) -> None:
    stop = threading.Event()
    poller = FakePoller("nws", 10)
    poller.failures = [None, None, SourceError("nws", "answered HTTP 500")]
    clock = Clock(time)
    runner = Runner([poller], store, clock)
    original = poller.poll

    def poll_and_stop(store: EvidenceStore, now: datetime) -> PollReport:
        if len(poller.polled) == 2:
            stop.set()
        return original(store, now)

    poller.poll = poll_and_stop  # type: ignore[method-assign]

    runner.catch_up(seconds(0), seconds(1000), stop)

    assert poller.polled == [seconds(0), seconds(10), seconds(20)]
    assert clock.now() == NOON


def test_running_sleeps_until_the_next_poller_is_due_and_ends_when_stopped(
    store: EvidenceStore, time: Time
) -> None:
    fast, slow = FakePoller("fast", 30), FakePoller("slow", 45)
    runner = Runner([fast, slow], store, Clock(time))
    stop = threading.Event()
    slept: list[float] = []

    def sleep(wait: float) -> None:
        slept.append(wait)
        time.moment += timedelta(seconds=wait)
        if len(slept) == 4:
            stop.set()

    runner.run(stop, sleep=sleep)

    # Due at 30 (fast), 45 (slow), 60 (fast), and 90 (both).
    assert slept == [30.0, 15.0, 15.0, 30.0]
    assert fast.polled == [seconds(0), seconds(30), seconds(60)]
    assert slow.polled == [seconds(0), seconds(45)]
