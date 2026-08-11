"""Pure, versioned split and dividend accounting policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException

from app.services.backtest.market_planes import (
    CorporateAction,
    MarketDataPolicyError,
    deterministic_decimal_context,
    quote_unit_scale,
    quantize_eight,
)

SPLIT_ACCOUNTING_POLICY_VERSION = "SplitAccountingPolicyV1"
DIVIDEND_CASH_POLICY_VERSION = "DividendCashPolicyV1"


@dataclass(frozen=True)
class PositionState:
    shares: Decimal
    per_share_basis: Decimal


@dataclass(frozen=True)
class CorporateActionEvent:
    action_key: str
    session: date
    action_type: str
    value: Decimal
    evidence_revision: str
    policy_version: str
    quote_currency: str | None = None
    quote_unit: str | None = None
    entitlement_approximation: str | None = None


@dataclass(frozen=True)
class SplitApplication:
    position: PositionState
    value_before: Decimal
    value_after: Decimal
    event: CorporateActionEvent


@dataclass(frozen=True)
class DividendCashApplication:
    cash_credit: Decimal
    event: CorporateActionEvent


def _validate_position(position: PositionState) -> None:
    if (
        not position.shares.is_finite()
        or not position.per_share_basis.is_finite()
        or position.shares < 0
        or position.per_share_basis < 0
        or position.shares != position.shares.to_integral_value()
    ):
        raise MarketDataPolicyError("integrity_error", "Position state is invalid.")


def apply_split(
    position: PositionState,
    action: CorporateAction,
    *,
    quote_currency: str,
    quote_unit: str,
) -> SplitApplication:
    """Apply one split before the effective session's signals."""
    _validate_position(position)
    if action.action_type != "split":
        raise MarketDataPolicyError(
            "unsupported_corporate_action",
            f"Unsupported corporate action: {action.action_type}.",
        )
    if not action.value.is_finite() or action.value <= 0:
        raise MarketDataPolicyError("integrity_error", "Split ratio must be positive.")
    quote_unit_scale(quote_currency, quote_unit)

    try:
        with deterministic_decimal_context():
            value_before = quantize_eight(position.shares * position.per_share_basis)
            updated = PositionState(
                shares=position.shares * action.value,
                per_share_basis=position.per_share_basis / action.value,
            )
            if updated.shares != updated.shares.to_integral_value():
                raise MarketDataPolicyError(
                    "unsupported_corporate_action",
                    "Split would require fractional-share or cash-in-lieu policy.",
                )
            value_after = quantize_eight(updated.shares * updated.per_share_basis)
    except DecimalException as exc:
        raise MarketDataPolicyError(
            "integrity_error", "Split accounting arithmetic failed."
        ) from exc
    if value_before != value_after:
        raise MarketDataPolicyError(
            "integrity_error", "Split accounting changed position value."
        )
    return SplitApplication(
        position=updated,
        value_before=value_before,
        value_after=value_after,
        event=CorporateActionEvent(
            action_key=(
                f"{action.evidence_revision}:{action.session.isoformat()}:split"
            ),
            session=action.session,
            action_type="split",
            value=action.value,
            evidence_revision=action.evidence_revision,
            policy_version=SPLIT_ACCOUNTING_POLICY_VERSION,
            quote_currency=quote_currency,
            quote_unit=quote_unit,
        ),
    )


def apply_dividend_cash(
    *,
    shares_carried_into_open: Decimal,
    action: CorporateAction,
    quote_currency: str,
    quote_unit: str,
) -> DividendCashApplication:
    """Credit only shares explicitly carried into D under DividendCashPolicyV1."""
    if action.action_type != "dividend":
        raise MarketDataPolicyError(
            "unsupported_corporate_action",
            f"Unsupported corporate action: {action.action_type}.",
        )
    if (
        not shares_carried_into_open.is_finite()
        or shares_carried_into_open < 0
        or shares_carried_into_open != shares_carried_into_open.to_integral_value()
        or not action.value.is_finite()
        or action.value <= 0
    ):
        raise MarketDataPolicyError(
            "integrity_error", "Dividend accounting input is invalid."
        )
    quote_unit_scale(quote_currency, quote_unit)
    try:
        with deterministic_decimal_context():
            cash_credit = quantize_eight(shares_carried_into_open * action.value)
    except DecimalException as exc:
        raise MarketDataPolicyError(
            "integrity_error", "Dividend accounting arithmetic failed."
        ) from exc
    return DividendCashApplication(
        cash_credit=cash_credit,
        event=CorporateActionEvent(
            action_key=(
                f"{action.evidence_revision}:{action.session.isoformat()}:dividend"
            ),
            session=action.session,
            action_type="dividend",
            value=action.value,
            evidence_revision=action.evidence_revision,
            policy_version=DIVIDEND_CASH_POLICY_VERSION,
            quote_currency=quote_currency,
            quote_unit=quote_unit,
            entitlement_approximation=(
                "provider_event_date_is_entitlement_and_payment_date"
            ),
        ),
    )
