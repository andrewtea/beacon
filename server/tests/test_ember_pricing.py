import pytest

from beacon.ember import EmberConfigError, SpendingStop
from beacon.ember.pricing import Usage, parse_usd, price_nanousd, read_stop_settings


def test_cached_input_is_billed_at_the_cached_rate_instead_of_the_input_rate() -> None:
    # 6,000 uncached at $0.10/M, 4,000 cached at $0.004/M, 2,000 output at $0.50/M.
    cost = price_nanousd(input_tokens=10_000, cached_tokens=4_000, output_tokens=2_000)

    assert cost == 600_000 + 16_000 + 1_000_000


def test_a_request_over_200k_input_tokens_is_billed_at_long_context_rates_in_full() -> None:
    at_threshold = price_nanousd(input_tokens=200_000, cached_tokens=0, output_tokens=1_000)
    over = price_nanousd(input_tokens=200_001, cached_tokens=100_000, output_tokens=1_000)

    assert at_threshold == 200_000 * 100 + 1_000 * 500
    # $0.30/M input, $0.012/M cached, $1.50/M output, on every token of the request.
    assert over == 100_001 * 300 + 100_000 * 12 + 1_000 * 1500


def test_usage_without_detail_objects_prices_the_whole_input_at_the_full_rate() -> None:
    usage = Usage.from_response({"usage": {"input_tokens": 69, "output_tokens": 163}})

    assert (usage.cached_tokens, usage.reasoning_tokens) == (0, 0)
    assert usage.cost_nanousd == 69 * 100 + 163 * 500


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"output_tokens": 5},
        {"input_tokens": -1, "output_tokens": 5},
        {"input_tokens": True, "output_tokens": 5},
        {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 11}},
    ],
)
def test_usage_that_cannot_be_priced_is_rejected(usage: object) -> None:
    with pytest.raises(ValueError, match="usage"):
        Usage.from_response({"usage": usage})


def test_the_stop_defaults_to_three_dollars_and_what_was_spent_before_has_no_default() -> None:
    assert SpendingStop.from_env({"EMBER_SPENT_BEFORE_USD": "0"}) == SpendingStop(
        ledger_nanousd=3_000_000_000, spent_before_nanousd=0
    )
    # An account that had spent $8.00 has $1.00 left under the ceiling, so a guess of
    # $0.00 would admit $3.00 more: there is no stop until the page has been read.
    # A variable that is set and empty, as an unfilled line of a .env file is, is unset.
    for env in ({}, {"EMBER_STOP_USD": "1"}, {"EMBER_STOP_USD": "", "EMBER_SPENT_BEFORE_USD": " "}):
        with pytest.raises(EmberConfigError, match="EMBER_SPENT_BEFORE_USD is not set"):
            SpendingStop.from_env(env)
    # What reads the settings without spending tells an unset amount from zero.
    assert read_stop_settings({}) == (3_000_000_000, None)
    assert read_stop_settings({"EMBER_STOP_USD": "1", "EMBER_SPENT_BEFORE_USD": "0"}) == (
        1_000_000_000,
        0,
    )
    # Neither amount can be left out of a stop, or passed without its name.
    with pytest.raises(TypeError):
        SpendingStop(ledger_nanousd=3_000_000_000)
    with pytest.raises(TypeError):
        SpendingStop(3_000_000_000, 0)


def test_the_stop_and_what_was_spent_before_it_are_read_from_the_environment() -> None:
    stop = SpendingStop.from_env({"EMBER_STOP_USD": "0.50", "EMBER_SPENT_BEFORE_USD": "$1.25"})

    assert stop == SpendingStop(ledger_nanousd=500_000_000, spent_before_nanousd=1_250_000_000)


def test_the_stop_and_what_was_spent_before_it_cannot_pass_nine_dollars_together() -> None:
    SpendingStop(ledger_nanousd=9_000_000_000, spent_before_nanousd=0)
    SpendingStop(ledger_nanousd=3_000_000_000, spent_before_nanousd=6_000_000_000)

    with pytest.raises(ValueError, match=r"would pass \$9\.00"):
        SpendingStop(ledger_nanousd=9_000_000_001, spent_before_nanousd=0)
    with pytest.raises(ValueError, match="Lower EMBER_STOP_USD"):
        SpendingStop(ledger_nanousd=3_000_000_000, spent_before_nanousd=6_000_000_001)
    # The default $3.00 stop on top of $6.01 already spent, in live mode and in the
    # reading that replay mode does.
    with pytest.raises(EmberConfigError, match=r"\$3\.00 on top of \$6\.01"):
        SpendingStop.from_env({"EMBER_SPENT_BEFORE_USD": "6.01"})
    with pytest.raises(EmberConfigError, match=r"\$3\.00 on top of \$6\.01"):
        read_stop_settings({"EMBER_SPENT_BEFORE_USD": "6.01"})
    # With the earlier spend unknown, the stop alone is still held under the ceiling.
    with pytest.raises(EmberConfigError, match=r"would pass \$9\.00"):
        read_stop_settings({"EMBER_STOP_USD": "9.01"})


@pytest.mark.parametrize("amount", [-1, True, 1.5, "3"])
def test_a_stop_is_a_whole_number_of_nano_dollars(amount: object) -> None:
    with pytest.raises(ValueError, match="nano-dollars"):
        SpendingStop(ledger_nanousd=amount, spent_before_nanousd=0)
    with pytest.raises(ValueError, match="nano-dollars"):
        SpendingStop(ledger_nanousd=0, spent_before_nanousd=amount)


def test_a_dollar_amount_is_read_exactly() -> None:
    assert parse_usd("3") == 3_000_000_000
    assert parse_usd(" $4.25 ") == 4_250_000_000
    assert parse_usd("0.000000001") == 1
    assert parse_usd("0") == 0
    # Zeros past the ninth place change nothing, however many there are.
    assert parse_usd("9." + "0" * 40) == 9_000_000_000


def test_an_amount_a_hair_from_nine_dollars_is_never_rounded_to_it() -> None:
    # Arithmetic at a fixed precision reads both of these as exactly 9.
    under = "8." + "9" * 32
    over = "9." + "0" * 32 + "1"
    for text in (under, over):
        with pytest.raises(ValueError, match="finer than a nano-dollar"):
            parse_usd(text)
        with pytest.raises(EmberConfigError, match=r"^EMBER_STOP_USD: "):
            read_stop_settings({"EMBER_STOP_USD": text})

    # The nearest amounts that can be written, on each side of the ceiling.
    at = {"EMBER_SPENT_BEFORE_USD": "0", "EMBER_STOP_USD": "9"}
    assert parse_usd("8.999999999") == 8_999_999_999
    assert SpendingStop.from_env(at).ledger_nanousd == 9_000_000_000
    with pytest.raises(EmberConfigError, match=r"would pass \$9\.00"):
        SpendingStop.from_env(at | {"EMBER_STOP_USD": "9.000000001"})
    with pytest.raises(EmberConfigError, match=r"would pass \$9\.00"):
        SpendingStop.from_env(at | {"EMBER_SPENT_BEFORE_USD": "0.000000001"})
    # More digits than a float holds are still counted, not rounded.
    with pytest.raises(EmberConfigError, match=r"would pass \$9\.00"):
        SpendingStop.from_env(at | {"EMBER_STOP_USD": "1" + "0" * 30})


@pytest.mark.parametrize(
    "text",
    [
        "three",
        "-1",
        "+1",
        "1e3",
        "1,000",
        "1_000",
        ".5",
        "5.",
        "inf",
        "nan",
        # Finer than a nano-dollar: refused, not rounded.
        "0.0000000001",
        # A digit that is not an ASCII digit.
        "\uff13",
    ],
)
def test_an_amount_that_is_not_plain_dollars_is_refused_and_names_its_variable(text: str) -> None:
    with pytest.raises(ValueError, match="dollar"):
        parse_usd(text)
    with pytest.raises(EmberConfigError, match=r"^EMBER_STOP_USD: "):
        SpendingStop.from_env({"EMBER_STOP_USD": text})
    with pytest.raises(EmberConfigError, match=r"^EMBER_SPENT_BEFORE_USD: "):
        SpendingStop.from_env({"EMBER_SPENT_BEFORE_USD": text})
