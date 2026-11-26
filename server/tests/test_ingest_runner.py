"""The runner: each poller when it is due, and one failing does not stop the rest."""

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.ingest import Clock, PollerStatus, PollReport, Runner, SourceError

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)


def seconds(count: float) -> datetime:
    return NOON + timedelta(seconds=count)


class FakePoller:
    """Records when it was polled, and fails when the next of `failures` is an error."""

    origin = "mock"

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


@pytest.mark.parametrize("asked", [1e300, 1e12, float("inf"), float("nan")])
def test_a_wait_too_long_to_be_a_date_does_not_stop_the_other_pollers(
    store: EvidenceStore, time: Time, asked: float
) -> None:
    limited, fine = FakePoller("limited", 30), FakePoller("fine", 30)
    limited.failures = [SourceError("limited", "answered HTTP 429", status=429, retry_after=asked)]
    runner = Runner([limited, fine], store, Clock(time))

    assert sorted(runner.step()) == ["fine", "limited"]

    time.moment = seconds(30)
    runner.step()
    # The longest any source is left alone is a week, whatever it asked for. A wait
    # that is no number at all counts as none.
    week = 7 * 86_400
    for count in (week - 1, week):
        time.moment = seconds(count)
        runner.step()
    # A second after its last poll, the other poller is not due again.
    assert fine.polled == [seconds(0), seconds(30), seconds(week - 1)]
    usable = asked == asked and asked != float("inf")
    assert limited.polled == (
        [seconds(0), seconds(week)] if usable else [seconds(0), seconds(30), seconds(week - 1)]
    )


def test_a_wait_of_most_of_a_day_is_kept_in_full(store: EvidenceStore, time: Time) -> None:
    # A quota that is spent for the day comes back at midnight, hours away.
    spent = FakePoller("opensky", 240)
    spent.failures = [SourceError("opensky", "answered HTTP 429", status=429, retry_after=80_000)]
    runner = Runner([spent], store, Clock(time))

    for count in (0, 6 * 3600, 79_999, 80_000):
        time.moment = seconds(count)
        runner.step()

    assert spent.polled == [seconds(0), seconds(80_000)]


def test_the_wait_a_source_asks_for_is_counted_from_when_it_answered(
    store: EvidenceStore, time: Time
) -> None:
    slow, limited = FakePoller("slow", 30), FakePoller("limited", 30)

    def take_a_minute(store: EvidenceStore, now: datetime) -> PollReport:
        time.moment += timedelta(seconds=60)
        return PollReport(added=0)

    def refuse_after_fifty_seconds(store: EvidenceStore, now: datetime) -> PollReport:
        # The third page of an answer, say: the refusal comes well into the poll.
        limited.polled.append(now)
        time.moment += timedelta(seconds=50)
        raise SourceError("limited", "answered HTTP 429", status=429, retry_after=600)

    slow.poll = take_a_minute  # type: ignore[method-assign]
    limited.poll = refuse_after_fifty_seconds  # type: ignore[method-assign]
    runner = Runner([slow, limited], store, Clock(time))
    # The step begins at 0, and the source says "in 600 seconds" at 110.
    runner.step()
    limited.poll = lambda store, now: limited.polled.append(now) or PollReport(added=0)  # type: ignore[method-assign]

    for count in (600, 709, 710):
        time.moment = seconds(count)
        runner.step()

    assert limited.polled == [seconds(0), seconds(710)]


def test_a_poller_that_failed_can_be_left_due_at_the_same_moment(
    store: EvidenceStore, time: Time
) -> None:
    down, fine = FakePoller("down", 30), FakePoller("fine", 30)
    down.failures = [SourceError("down", "could not be reached"), ValueError("still down")]
    runner = Runner([down, fine], store, Clock(time))

    first = runner.step(failed_stay_due=True)
    second = runner.step(failed_stay_due=True)
    third = runner.step(failed_stay_due=True)

    # Only the poller that failed polls again at that moment, until it succeeds.
    assert [sorted(results) for results in (first, second, third)] == [
        ["down", "fine"],
        ["down"],
        ["down"],
    ]
    assert runner.step(failed_stay_due=True) == {}
    assert (down.polled, fine.polled) == ([NOON, NOON, NOON], [NOON])
    assert runner.status()[0] == PollerStatus("down", NOON, True)


def test_a_runner_can_be_told_how_the_last_polls_of_an_earlier_process_went(
    store: EvidenceStore, time: Time
) -> None:
    earlier = [PollerStatus("nws", seconds(-600), True), PollerStatus("gone", seconds(-600), False)]
    runner = Runner(
        [FakePoller("nws", 30), FakePoller("511", 30)], store, Clock(time), last=earlier
    )

    assert runner.status() == [
        PollerStatus("nws", seconds(-600), True),
        PollerStatus("511", None, None),
    ]
    # It is what is shown until the poller polls again, which it does at once.
    assert sorted(runner.step()) == ["511", "nws"]
    assert runner.status()[0] == PollerStatus("nws", NOON, True)


def test_a_poll_that_read_only_part_of_an_answer_is_logged_and_shown_as_partial(
    store: EvidenceStore, time: Time, caplog: pytest.LogCaptureFixture
) -> None:
    paged = FakePoller("511", 120)
    reports = [PollReport(added=3, partial=True), PollReport(added=0)]
    paged.poll = lambda store, now: reports.pop(0)  # type: ignore[method-assign]
    runner = Runner([paged], store, Clock(time))

    assert runner.step() == {"511": PollReport(added=3, partial=True)}

    # It is a poll that worked, and it may have missed what the source lists.
    assert runner.status() == [PollerStatus("511", NOON, True, partial=True)]
    assert "511's answer was not read to its end, so items it lists may be missing." in caplog.text
    time.moment = seconds(120)
    runner.step()
    assert runner.status() == [PollerStatus("511", seconds(120), True, partial=False)]


def test_a_runner_on_the_live_store_takes_no_poller_that_a_stand_in_feeds(
    tmp_path: Path, time: Time
) -> None:
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    real = FakePoller("nws", 30)
    real.origin = "live"  # type: ignore[misc]

    with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
        Runner([real, FakePoller("511", 30)], live, Clock(time))

    assert sorted(Runner([real], live, Clock(time)).step()) == ["nws"]


def test_a_poll_cut_short_by_stopping_is_not_reported_as_the_sources_failure(
    store: EvidenceStore, time: Time, caplog: pytest.LogCaptureFixture
) -> None:
    stop = threading.Event()
    asking, next_one = FakePoller("asking", 30), FakePoller("next", 30)

    def be_cut_short(store: EvidenceStore, now: datetime) -> PollReport:
        stop.set()
        raise SourceError("asking", "was not read, because Beacon is stopping")

    asking.poll = be_cut_short  # type: ignore[method-assign]
    runner = Runner([asking, next_one], store, Clock(time))

    with caplog.at_level(logging.DEBUG):
        results = runner.step(stop)

    assert list(results) == ["asking"]
    assert caplog.text == ""
    assert runner.status() == [PollerStatus("asking", None, None), PollerStatus("next", None, None)]


def test_the_interval_is_read_after_each_poll(store: EvidenceStore, time: Time) -> None:
    poller = FakePoller("511", 120)
    original = poller.poll

    def poll_three_pages(store: EvidenceStore, now: datetime) -> PollReport:
        # A poll that cost three requests asks for three intervals.
        poller.interval_seconds = 360
        return original(store, now)

    poller.poll = poll_three_pages  # type: ignore[method-assign]
    runner = Runner([poller], store, Clock(time))

    for count in (0, 120, 359, 360):
        time.moment = seconds(count)
        runner.step()

    assert poller.polled == [seconds(0), seconds(360)]


def test_no_poll_is_begun_once_the_runner_is_told_to_stop(store: EvidenceStore, time: Time) -> None:
    stop = threading.Event()
    first, second = FakePoller("first", 30), FakePoller("second", 30)
    original = first.poll

    def poll_and_stop(store: EvidenceStore, now: datetime) -> PollReport:
        stop.set()
        return original(store, now)

    first.poll = poll_and_stop  # type: ignore[method-assign]
    runner = Runner([first, second], store, Clock(time))

    assert list(runner.step(stop)) == ["first"]
    assert second.polled == []


def test_a_held_clock_stays_at_its_moment_until_it_is_let_go(time: Time) -> None:
    clock = Clock(time)

    clock.hold(seconds(-600))
    time.moment = seconds(5)
    assert clock.now() == seconds(-600)

    clock.hold(None)
    assert clock.now() == seconds(5)


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
