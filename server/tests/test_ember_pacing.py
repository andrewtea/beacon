"""The pacer: spacing requests under a rate limit, and holding off after a 429."""

from collections.abc import Callable

import pytest

from beacon.ember import Pacer, RateLimitHoldError
from beacon.ember.pacing import RATE_LIMIT_RPM


class Clock:
    """A clock that only moves when the pacer sleeps, or a test moves it. `during`
    runs in the middle of the next sleep, as another thread would."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.during: Callable[[], None] | None = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        during, self.during = self.during, None
        if during is not None:
            self.now += seconds / 2
            during()
            self.now += seconds / 2
        else:
            self.now += seconds


def pacer(clock: Clock, requests_per_minute: int = 60, **options) -> Pacer:
    # Jitter of 0.5 stretches every delay by half.
    return Pacer(requests_per_minute, clock=clock, sleep=clock.sleep, jitter=lambda: 0.5, **options)


def test_requests_are_spaced_to_the_rate_and_an_idle_spell_is_not_saved_up() -> None:
    clock = Clock()
    paced = pacer(clock, 60)  # one request a second

    assert paced.wait() == 0  # the first goes at once
    assert paced.wait() == pytest.approx(1)
    assert paced.wait() == pytest.approx(1)
    assert clock.sleeps == pytest.approx([1, 1])

    clock.now += 600  # ten idle minutes buy no burst
    assert paced.wait() == 0
    assert paced.wait() == pytest.approx(1)


def test_a_rate_limit_holds_every_request_for_a_delay_that_doubles() -> None:
    clock = Clock()
    paced = pacer(clock, RATE_LIMIT_RPM, max_retries=4, max_delay_seconds=5.0)

    waits = []
    for attempt in range(4):
        assert paced.back_off(attempt)
        waits.append(paced.wait())
    # 0.5 s doubling, each stretched by half, and never past the longest delay.
    assert waits == pytest.approx([0.75, 1.5, 3.0, 5.0])
    assert paced.rate_limited == 4


def test_retry_after_is_a_minimum_and_is_waited_out_in_full() -> None:
    clock = Clock()
    paced = pacer(clock, RATE_LIMIT_RPM, max_delay_seconds=30.0)

    assert paced.back_off(0, retry_after=12.0)
    assert paced.wait() == pytest.approx(12.0)  # not the 0.75 s the attempt alone asks for
    assert paced.back_off(1, retry_after=0.1)
    assert paced.wait() == pytest.approx(1.5)  # the header is a minimum, not the delay


def test_a_request_already_waiting_observes_a_cooldown_that_starts_meanwhile() -> None:
    clock = Clock()
    paced = pacer(clock, 60)
    assert paced.wait() == 0

    # The second request sleeps 1 s for its place. Half way through, another thread's
    # call is answered 429 and told to wait 3 s.
    clock.during = lambda: paced.back_off(0, retry_after=3.0)

    assert paced.wait() == pytest.approx(3.5)
    # It woke at its old place, found the cooldown, and slept to the cooldown's end.
    assert clock.sleeps == pytest.approx([1.0, 2.5])
    assert clock.now == pytest.approx(1003.5)


def test_requests_held_by_a_cooldown_do_not_all_go_when_it_ends() -> None:
    clock = Clock()
    paced = pacer(clock, 60)

    paced.back_off(0)
    assert paced.wait() == pytest.approx(0.75)
    # The one behind it is spaced again, not sent at the same moment.
    assert paced.wait() == pytest.approx(1.0)


def test_a_call_that_is_given_up_still_holds_the_other_threads() -> None:
    clock = Clock()
    paced = pacer(clock, RATE_LIMIT_RPM, max_retries=2, max_delay_seconds=30.0)

    # The call has used up its retries. It is not tried again, and the API is still
    # saying to slow down, so every other request waits the cooldown out.
    assert not paced.back_off(2)
    assert paced.wait() == pytest.approx(3.0)
    assert paced.rate_limited == 1


def test_a_wait_longer_than_the_longest_delay_is_refused_until_it_has_passed() -> None:
    clock = Clock()
    paced = pacer(clock, RATE_LIMIT_RPM, max_delay_seconds=30.0)

    # Neither cut short nor sat through. The call that was told to wait is stopped,
    # not handed back as a failure to try again, and nothing is sent.
    with pytest.raises(RateLimitHoldError, match="asked for a wait of 3600 s"):
        paced.back_off(0, retry_after=3600.0)
    assert paced.rate_limited == 1
    with pytest.raises(RateLimitHoldError, match="3600 s still to run"):
        paced.wait()
    clock.now += 3000
    with pytest.raises(RateLimitHoldError):
        paced.wait()
    assert clock.sleeps == []

    # Once what is left is a wait the pacer makes, requests are let through again,
    # after all of it.
    clock.now += 580
    assert paced.wait() == pytest.approx(20.0)


@pytest.mark.parametrize("requests_per_minute", [0, -1, RATE_LIMIT_RPM + 1, 2.5, True])
def test_a_pacer_cannot_be_set_past_metas_limit(requests_per_minute) -> None:
    with pytest.raises(ValueError, match="requests_per_minute"):
        Pacer(requests_per_minute)
