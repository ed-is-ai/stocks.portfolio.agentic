"""Unit tests for ``app.core.money``: ``Money``, ``parse_money``.

Story 1.4, AC1-4. The required-case table in the story's Testing
Requirements section is the source of truth for the parametrized cases
below -- one test id per row.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.money import (
    Money,
    MoneyParseError,
    SUPPORTED_CURRENCIES,
    has_currency_marker,
    parse_money,
)


def test_supported_currencies_are_v1_scoped() -> None:
    assert SUPPORTED_CURRENCIES == frozenset({"GBP", "USD", "EUR", "HKD"})


@pytest.mark.parametrize(
    "raw,default_currency,expected",
    [
        ("£123.45", "GBP", True),
        ("$1.25", "GBP", True),
        ("HK$1,234.56", "GBP", True),
        ("(£45.00)", "GBP", True),
        ("-$45.00", "GBP", True),
        ("1,234.56", "GBP", False),
        ("(123.45)", "GBP", False),
        ("1,234.56", "HKD", False),
    ],
)
def test_has_currency_marker(raw: str, default_currency: str, expected: bool) -> None:
    """Story 1.4, Gate 4: the SIPP import's contradiction cross-check must
    tell an explicitly-marked field apart from one that silently defaulted
    -- a marker-less field never contradicts another field's currency."""
    assert has_currency_marker(raw, default_currency=default_currency) is expected


def test_money_is_frozen() -> None:
    money = Money(amount=Decimal("1.00"), currency="GBP")
    with pytest.raises(ValidationError):
        money.amount = Decimal("2.00")  # type: ignore[misc]


def test_money_uppercases_and_shape_checks_currency() -> None:
    assert Money(amount=Decimal("1"), currency="gbp").currency == "GBP"
    with pytest.raises(ValidationError):
        Money(amount=Decimal("1"), currency="GB")
    with pytest.raises(ValidationError):
        Money(amount=Decimal("1"), currency="GBPX")


def test_money_accepts_currency_outside_the_v1_allow_list() -> None:
    # Money itself is generic -- SUPPORTED_CURRENCIES is parse_money's
    # policy, not a Money type invariant (so Epic 2/Story 1.6 can reuse it).
    assert Money(amount=Decimal("1"), currency="CHF").currency == "CHF"


def test_money_parse_error_has_a_stable_code() -> None:
    with pytest.raises(MoneyParseError) as exc_info:
        parse_money("abc")
    assert exc_info.value.code == "malformed_locale"


@pytest.mark.parametrize(
    ("raw", "expected_amount", "expected_currency"),
    [
        pytest.param("£123.45", "123.45", "GBP", id="gbp-baseline"),
        pytest.param("€153.61", "153.61", "EUR", id="eur-marker-preserved"),
        pytest.param("$1.25", "1.25", "USD", id="usd-marker-preserved"),
        pytest.param("HK$1,234.56", "1234.56", "HKD", id="hkd-ac2"),
        pytest.param("1,234.56", "1234.56", "GBP", id="uk-us-grouping-no-marker"),
        pytest.param("1.234,56", "1234.56", "GBP", id="eu-format-ac4"),
        pytest.param("1,234,567.89", "1234567.89", "GBP", id="repeated-grouping-us"),
        pytest.param("1.234.567,89", "1234567.89", "GBP", id="repeated-grouping-eu"),
        pytest.param("(123.45)", "-123.45", "GBP", id="parenthesized-negative-ac4"),
        pytest.param("$-1,762.15", "-1762.15", "USD", id="real-marker-then-sign"),
        pytest.param("-$45.00", "-45.00", "USD", id="sign-then-marker"),
        pytest.param(
            "$0.73427", "0.73427", "USD", id="real-fx-rate-shape-5dp-unambiguous"
        ),
        pytest.param("1,25", "1.25", "GBP", id="n2-after-comma-unambiguous"),
    ],
)
def test_parse_money_accepts(
    raw: str, expected_amount: str, expected_currency: str
) -> None:
    money = parse_money(raw)
    assert money.amount == Decimal(expected_amount)
    assert money.currency == expected_currency


def test_parse_money_default_currency_param_is_not_hardcoded_to_gbp() -> None:
    money = parse_money("1,234.56", default_currency="HKD")
    assert money.amount == Decimal("1234.56")
    assert money.currency == "HKD"


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        pytest.param("1.234", "ambiguous_currency", id="single-dot-3dp-ambiguous"),
        pytest.param("1,234", "ambiguous_currency", id="single-comma-3dp-ambiguous"),
        pytest.param("¥123", "unsupported_currency", id="yen-marker-ac3"),
        pytest.param("CHF 123.45", "unsupported_currency", id="chf-code-prefix-ac3"),
        pytest.param(
            "12.34.56", "malformed_locale", id="two-decimal-shaped-no-grouping"
        ),
        pytest.param("", "malformed_locale", id="empty"),
        pytest.param("abc", "malformed_locale", id="garbage-text"),
        pytest.param("$", "malformed_locale", id="marker-alone-no-digits"),
    ],
)
def test_parse_money_rejects(raw: str, expected_code: str) -> None:
    with pytest.raises(MoneyParseError) as exc_info:
        parse_money(raw)
    assert exc_info.value.code == expected_code


def test_parse_money_constructs_decimal_from_string_not_float() -> None:
    # A float round-trip would corrupt e.g. 0.1 + 0.2 style values; Decimal
    # must be built from the string form throughout.
    money = parse_money("£0.1")
    assert money.amount == Decimal("0.1")
    assert str(money.amount) == "0.1"
