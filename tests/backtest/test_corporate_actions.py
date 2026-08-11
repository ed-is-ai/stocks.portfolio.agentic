from __future__ import annotations

from datetime import date
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext

import pytest

from app.services.backtest.corporate_actions import (
    DIVIDEND_CASH_POLICY_VERSION,
    SPLIT_ACCOUNTING_POLICY_VERSION,
    PositionState,
    apply_dividend_cash,
    apply_split,
)
from app.services.backtest.market_planes import CorporateAction, MarketDataPolicyError


def _action(kind: str, value: str) -> CorporateAction:
    return CorporateAction(
        session=date(2024, 1, 3),
        action_type=kind,
        value=Decimal(value),
        evidence_revision="revision-1",
    )


@pytest.mark.parametrize(
    ("ratio", "expected_shares", "expected_basis"),
    [("4", "40.00000000", "25.00000000"), ("0.1", "1.00000000", "1000.00000000")],
)
def test_split_multiplies_shares_and_inversely_adjusts_basis_once(
    ratio: str, expected_shares: str, expected_basis: str
) -> None:
    result = apply_split(
        PositionState(shares=Decimal("10"), per_share_basis=Decimal("100")),
        _action("split", ratio),
        quote_currency="USD",
        quote_unit="USD",
    )
    assert result.position.shares == Decimal(expected_shares)
    assert result.position.per_share_basis == Decimal(expected_basis)
    assert result.value_before == result.value_after == Decimal("1000.00000000")
    assert result.event.policy_version == SPLIT_ACCOUNTING_POLICY_VERSION
    assert result.event.evidence_revision == "revision-1"
    assert result.event.quote_currency == result.event.quote_unit == "USD"
    assert result.event.action_key == "revision-1:2024-01-03:split"


def test_dividend_credits_only_supplied_carried_at_open_shares_once() -> None:
    result = apply_dividend_cash(
        shares_carried_into_open=Decimal("12"),
        action=_action("dividend", "0.125"),
        quote_currency="GBP",
        quote_unit="GBp",
    )
    assert result.cash_credit == Decimal("1.50000000")
    assert result.event.policy_version == DIVIDEND_CASH_POLICY_VERSION
    assert result.event.action_key == "revision-1:2024-01-03:dividend"
    assert (
        result.event.entitlement_approximation
        == "provider_event_date_is_entitlement_and_payment_date"
    )

    same_session_buy = apply_dividend_cash(
        shares_carried_into_open=Decimal("0"),
        action=_action("dividend", "0.125"),
        quote_currency="GBP",
        quote_unit="GBp",
    )
    assert same_session_buy.cash_credit == Decimal("0E-8")


def test_wrong_action_and_invalid_position_fail_with_stable_codes() -> None:
    with pytest.raises(MarketDataPolicyError) as unsupported:
        apply_split(
            PositionState(Decimal("1"), Decimal("1")),
            _action("merger", "1"),
            quote_currency="USD",
            quote_unit="USD",
        )
    assert unsupported.value.code == "unsupported_corporate_action"

    with pytest.raises(MarketDataPolicyError) as invalid:
        apply_dividend_cash(
            shares_carried_into_open=Decimal("-1"),
            action=_action("dividend", "1"),
            quote_currency="USD",
            quote_unit="USD",
        )
    assert invalid.value.code == "integrity_error"


def test_split_that_requires_fractional_share_policy_fails_visibly() -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        apply_split(
            PositionState(Decimal("1"), Decimal("100")),
            _action("split", "0.1"),
            quote_currency="USD",
            quote_unit="USD",
        )
    assert exc_info.value.code == "unsupported_corporate_action"


def test_corporate_action_rejects_unsupported_quote_unit() -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        apply_dividend_cash(
            shares_carried_into_open=Decimal("1"),
            action=_action("dividend", "1"),
            quote_currency="EUR",
            quote_unit="EUR",
        )
    assert exc_info.value.code == "unsupported_quote_unit"


def test_dividend_credit_uses_exact_half_even_tie_breaking() -> None:
    result = apply_dividend_cash(
        shares_carried_into_open=Decimal("1"),
        action=_action("dividend", "0.000000005"),
        quote_currency="USD",
        quote_unit="USD",
    )
    assert result.cash_credit == Decimal("0E-8")


def test_corporate_action_arithmetic_ignores_ambient_rounding_and_traps() -> None:
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        result = apply_split(
            PositionState(Decimal("3"), Decimal("1")),
            _action("split", "3"),
            quote_currency="USD",
            quote_unit="USD",
        )
    assert result.position == PositionState(
        Decimal("9"), Decimal("0.33333333333333333333333333333333333333333333333333")
    )
    assert result.value_before == result.value_after == Decimal("3.00000000")
