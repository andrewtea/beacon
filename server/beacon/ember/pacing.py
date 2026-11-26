"""Pacing for a run that makes many live calls: stay under Meta's request limit, and
hold off when the API answers 429 anyway.

A client has no pacer unless it is given one, and then it neither waits nor retries.
A run that makes many calls at once, such as the scale run, gives its client a
`Pacer`, which every thread of the run shares:

- `wait` is called before each HTTP request, the token count and the call itself
  alike. It gives the request a place in the minute, so together the threads stay
  under a number of requests per minute.
- `back_off` is called after the API answers 429. It starts a cooldown that every
  thread's `wait` observes, including a thread that was already waiting: the delay
  doubles with each attempt, carries jitter, and is at least the response's
  `Retry-After`. It answers whether the call may be tried again.

A `Retry-After` longer than the pacer's longest delay is not cut short and not slept
through. The cooldown is set for all of it, `back_off` raises `RateLimitHoldError` to
the call that was told to wait, and until the cooldown has passed `wait` raises it to
every other. So a run stops instead of sending or sleeping for an hour, and the call
is not taken for one that failed and can be tried again at once.

Only a 429 is retried. Each retry is a new call to the client: it is counted,
reserved against the spending stop, and written to the ledger like any other.

Limits are from https://dev.meta.ai/docs/pricing-rate-limits, checked 2026-11-25.
"""

import random
import threading
import time
from collections.abc import Callable

from beacon.ember.errors import RateLimitHoldError

# Meta's limit for muse-ember-1.0, per team. Its other limit is 4,000,000 tokens per
# minute, which the pacer does not count.
RATE_LIMIT_RPM = 3000
# One Ember call is two requests, the token count and the call itself, so this is 300
# calls a minute. At that rate a call can carry 13,000 tokens before the token limit
# is the nearer one. A run whose calls are larger than that sets a lower rate.
DEFAULT_RPM = 600


class Pacer:
    """Shared by every thread of one run. `clock`, `sleep`, and `jitter` are for tests."""

    def __init__(
        self,
        requests_per_minute: int = DEFAULT_RPM,
        *,
        max_retries: int = 5,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if (
            not isinstance(requests_per_minute, int)
            or isinstance(requests_per_minute, bool)
            or not 1 <= requests_per_minute <= RATE_LIMIT_RPM
        ):
            raise ValueError(
                f"requests_per_minute must be a whole number from 1 to {RATE_LIMIT_RPM}, "
                f"Meta's limit, not {requests_per_minute!r}."
            )
        if max_retries < 0 or not 0 < base_delay_seconds <= max_delay_seconds:
            raise ValueError("The retry settings must be positive, with base under max.")
        self.requests_per_minute = requests_per_minute
        self.max_retries = max_retries
        self._interval = 60.0 / requests_per_minute
        self._base = base_delay_seconds
        self._max = max_delay_seconds
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._lock = threading.Lock()
        # The earliest moment the next request may take as its place.
        self._next = clock()
        # No request is sent before this moment: the API said to hold off.
        self._cooldown_until = self._next
        self._rate_limited = 0

    @property
    def rate_limited(self) -> int:
        """How many times the API answered 429 to a call paced here."""
        with self._lock:
            return self._rate_limited

    def wait(self) -> float:
        """Block until one request may be sent, and return the seconds waited. An idle
        minute is not saved up: after a pause the next request goes at once and the
        ones behind it are spaced again.

        A cooldown that starts while this waits is waited out too, and the request
        then takes a new place behind it. Raises RateLimitHoldError when a cooldown
        has longer to run than the pacer's longest delay."""
        waited = 0.0
        place: float | None = None
        while True:
            with self._lock:
                now = self._clock()
                hold = self._cooldown_until - now
                if hold > self._max:
                    raise RateLimitHoldError(
                        f"The API asked for a wait with {hold:.0f} s still to run, longer "
                        f"than the {self._max:.0f} s a paced client waits. Nothing is sent "
                        "until it has passed."
                    )
                if place is None or place < self._cooldown_until:
                    # The first look, or a cooldown began after this request took its
                    # place: it queues again, so the requests that were waiting do not
                    # all go at the moment the cooldown ends.
                    place = max(now, self._next, self._cooldown_until)
                    self._next = place + self._interval
                delay = place - now
            if delay <= 0:
                return waited
            self._sleep(delay)
            waited += delay

    def back_off(self, attempt: int, retry_after: float | None = None) -> bool:
        """The API answered 429 to a call's attempt number `attempt`, counted from 0.
        Starts a cooldown for every thread and returns whether to try the call again.

        The cooldown starts whatever the answer is: a call that has used up its
        retries is given up, and the other threads still hold off. Raises
        RateLimitHoldError, with the cooldown set, when the API asked for a wait
        longer than the longest delay: that is no failure of this call's, and it is
        not to be tried again until the wait has passed."""
        with self._lock:
            self._rate_limited += 1
            # 0.5 s, 1 s, 2 s, ... each stretched by up to as much again, so the threads
            # of a run do not all come back at the same moment.
            delay = min(self._max, self._base * 2 ** min(attempt, 32) * (1 + self._jitter()))
            # The header is a minimum, and is never cut short.
            delay = max(delay, retry_after or 0.0)
            self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
            if retry_after is not None and retry_after > self._max:
                raise RateLimitHoldError(
                    f"The API answered 429 and asked for a wait of {retry_after:.0f} s, "
                    f"longer than the {self._max:.0f} s a paced client waits. Nothing is "
                    "sent until it has passed."
                )
            return attempt < self.max_retries
