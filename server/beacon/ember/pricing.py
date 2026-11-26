"""Muse Ember prices and the spending limits built on them.

Rates are from https://dev.meta.ai/docs/pricing-rate-limits, checked 2026-11-26. Money is
kept in whole nano-dollars because every rate is a whole number of them per token, so
ledger sums and budget comparisons are exact.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from beacon.ember.errors import EmberConfigError

MODEL = "muse-ember-1.0"

NANOUSD_PER_USD = 1_000_000_000


@dataclass(frozen=True)
class Rates:
    """Nano-dollars per token."""

    input: int
    cached_input: int
    output: int


# $0.10, $0.004 and $0.50 per million tokens.
STANDARD = Rates(input=100, cached_input=4, output=500)
# A request with more than 200K input tokens is billed at these rates in full,
# not only for the tokens past 200K.
LONG_CONTEXT = Rates(input=300, cached_input=12, output=1500)
LONG_CONTEXT_ABOVE_INPUT_TOKENS = 200_000

# The client refuses larger requests, which keeps every call well inside standard rates.
MAX_INPUT_TOKENS = 150_000

# The hackathon credit is $10 per participant, and going over it disqualifies the
# entry. Meta's Usage page may never pass this total: the last dollar covers drift
# between the ledger and Meta's billing. Raising it means changing it here on purpose.
ACCOUNT_CEILING_NANOUSD = 9 * NANOUSD_PER_USD

# The plan's stop is "$3 above the Usage page total of Nov 26, 2026", the day the
# ledger began. The ledger counts only Beacon's own calls, so that is $3.00 of live
# spend in the ledger whatever the page said.
DEFAULT_STOP_NANOUSD = 3 * NANOUSD_PER_USD
# What the page said that day is needed only to keep the two together under the
# ceiling. Nobody has read it off the page yet. The default assumes $0.00, because
# the only other project on the account, Forte, records that it never made a real
# Ember call. Set EMBER_SPENT_BEFORE_USD when the page says otherwise.
DEFAULT_SPENT_BEFORE_NANOUSD = 0

STOP_VARIABLE = "EMBER_STOP_USD"
SPENT_BEFORE_VARIABLE = "EMBER_SPENT_BEFORE_USD"


@dataclass(frozen=True)
class SpendingStop:
    """Where live spend stops.

    `ledger_nanousd` is the most the ledger may hold in live spend: a paid call is
    refused when it could take the ledger past it. `spent_before_nanousd` is what the
    account had already spent when the ledger began, read off Meta's Usage page. The
    ledger cannot see that spend, so the two are added and held under the ceiling
    when the stop is made, not when a call is."""

    ledger_nanousd: int = DEFAULT_STOP_NANOUSD
    spent_before_nanousd: int = DEFAULT_SPENT_BEFORE_NANOUSD

    def __post_init__(self) -> None:
        for amount in (self.ledger_nanousd, self.spent_before_nanousd):
            # bool is an int subclass; True is not an amount of money.
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError("An amount is a whole number of nano-dollars, zero or more.")
        if self.ledger_nanousd + self.spent_before_nanousd > ACCOUNT_CEILING_NANOUSD:
            raise ValueError(
                f"A stop of ${to_usd(self.ledger_nanousd):.2f} on top of "
                f"${to_usd(self.spent_before_nanousd):.2f} spent before the ledger began "
                f"would pass ${to_usd(ACCOUNT_CEILING_NANOUSD):.2f} of the $10.00 credit. "
                f"Lower {STOP_VARIABLE}."
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SpendingStop":
        """`EMBER_STOP_USD` and `EMBER_SPENT_BEFORE_USD`, each a dollar amount such as
        `2.50`. One that is unset or empty takes its default."""
        try:
            return cls(
                ledger_nanousd=_usd_setting(env, STOP_VARIABLE, DEFAULT_STOP_NANOUSD),
                spent_before_nanousd=_usd_setting(
                    env, SPENT_BEFORE_VARIABLE, DEFAULT_SPENT_BEFORE_NANOUSD
                ),
            )
        except ValueError as error:
            raise EmberConfigError(str(error)) from error


# Digits with an optional fraction: no sign, no exponent, no separators.
_USD = re.compile(r"\$?([0-9]+(?:\.[0-9]+)?)")


def parse_usd(text: str) -> int:
    """A dollar amount such as `3`, `0.50` or `$4.25` as whole nano-dollars. The amount
    is read exactly: one that is not written as plain digits with an optional
    fraction, or is finer than a nano-dollar, raises ValueError and is never rounded."""
    match = _USD.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not a dollar amount, such as 3 or 0.50.")
    amount = Decimal(match[1]) * NANOUSD_PER_USD
    if amount != amount.to_integral_value():
        raise ValueError(f"{text!r} is finer than a nano-dollar.")
    return int(amount)


def _usd_setting(env: Mapping[str, str], name: str, default: int) -> int:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        return parse_usd(text)
    except ValueError as error:
        raise ValueError(f"{name}: {error}") from None


def price_nanousd(*, input_tokens: int, cached_tokens: int, output_tokens: int) -> int:
    """What Meta bills for one request. `cached_tokens` is a subset of `input_tokens`,
    and reasoning tokens are already part of `output_tokens`."""
    if min(input_tokens, cached_tokens, output_tokens) < 0:
        raise ValueError("Token counts cannot be negative.")
    if cached_tokens > input_tokens:
        raise ValueError("cached_tokens is a subset of input_tokens and cannot exceed it.")
    rates = LONG_CONTEXT if input_tokens > LONG_CONTEXT_ABOVE_INPUT_TOKENS else STANDARD
    return (
        (input_tokens - cached_tokens) * rates.input
        + cached_tokens * rates.cached_input
        + output_tokens * rates.output
    )


def to_usd(nanousd: int) -> float:
    return nanousd / NANOUSD_PER_USD


@dataclass(frozen=True)
class Usage:
    """Token counts from a response's `usage` object."""

    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int

    def __post_init__(self) -> None:
        if self.cached_tokens > self.input_tokens:
            raise ValueError("usage reports more cached tokens than input tokens.")

    @classmethod
    def from_response(cls, response: Mapping[str, Any]) -> "Usage":
        """Raises ValueError when the response carries no usable token counts."""
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise ValueError("The response has no usage object.")
        return cls(
            input_tokens=_count(usage, "input_tokens"),
            # The detail objects are sometimes absent. Treating a missing cached count
            # as zero prices the whole input at the full rate, which never undercounts.
            cached_tokens=_count(usage.get("input_tokens_details") or {}, "cached_tokens", 0),
            output_tokens=_count(usage, "output_tokens"),
            reasoning_tokens=_count(
                usage.get("output_tokens_details") or {}, "reasoning_tokens", 0
            ),
        )

    @property
    def cost_nanousd(self) -> int:
        return price_nanousd(
            input_tokens=self.input_tokens,
            cached_tokens=self.cached_tokens,
            output_tokens=self.output_tokens,
        )


def _count(container: Any, key: str, default: int | None = None) -> int:
    value = container.get(key, default) if isinstance(container, Mapping) else default
    # bool is an int subclass; a JSON true is not a token count.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"usage.{key} is not a token count: {value!r}")
    return value
