"""Closed GBP/USD/GBp conversion over exact immutable FX evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException, InvalidOperation

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.market_planes import (
    MarketDataPolicyError,
    deterministic_decimal_context,
    provider_decimal,
    quote_unit_scale,
    quantize_eight,
    validate_provider_native_request_contract,
)

SUPPORTED_CURRENCIES = frozenset({"GBP", "USD"})
CURRENCY_CONVERSION_POLICY_VERSION = "CurrencyConversionPolicyV1"


class CurrencyPolicyError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class CurrencyConversion:
    policy_version: str
    source_amount: Decimal
    source_currency: str
    source_quote_unit: str
    base_amount: Decimal
    base_currency: str
    fx_rate: Decimal | None
    fx_session: date | None
    fx_revision: str | None


def _input_decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CurrencyPolicyError("integrity_error", "Amount is invalid.") from exc
    if not result.is_finite():
        raise CurrencyPolicyError("integrity_error", "Amount is not finite.")
    return result


def _normalize_quote(
    value: object, quote_currency: str, quote_unit: str
) -> tuple[Decimal, str]:
    amount = _input_decimal(value)
    if quote_currency not in SUPPORTED_CURRENCIES:
        raise CurrencyPolicyError(
            "unsupported_currency", f"Unsupported source currency: {quote_currency}."
        )
    try:
        scale = quote_unit_scale(quote_currency, quote_unit)
        with deterministic_decimal_context():
            return amount * scale, quote_currency
    except DecimalException as exc:
        raise CurrencyPolicyError(
            "integrity_error", "Quote-unit normalization failed."
        ) from exc
    except MarketDataPolicyError as exc:
        raise CurrencyPolicyError(exc.code, exc.detail) from exc


def _fx_closes(
    evidence: StoredHistoricalEvidence,
) -> tuple[tuple[date, Decimal], ...]:
    if (
        evidence.provider != "yfinance"
        or evidence.requested_symbol != "GBPUSD=X"
        or evidence.observed_symbol != "GBPUSD=X"
        or evidence.currency != "USD"
        or evidence.quote_unit != "USD"
        or evidence.quote_unit_scale != "1"
        or evidence.exchange_timezone != "UTC"
        or evidence.actions
    ):
        raise CurrencyPolicyError(
            "fx_ambiguous", "FX evidence is not exact USD-per-GBP evidence."
        )
    try:
        validate_provider_native_request_contract(evidence)
    except MarketDataPolicyError as exc:
        raise CurrencyPolicyError("fx_ambiguous", exc.detail) from exc
    try:
        start = date.fromisoformat(evidence.start)
        end = date.fromisoformat(evidence.end)
    except (TypeError, ValueError) as exc:
        raise CurrencyPolicyError(
            "integrity_error", "FX evidence interval is malformed."
        ) from exc
    if start >= end:
        raise CurrencyPolicyError("integrity_error", "FX evidence interval is invalid.")
    closes: list[tuple[date, Decimal]] = []
    seen: set[date] = set()
    for raw in evidence.rows:
        try:
            session = date.fromisoformat(str(raw["session"]))
            close = provider_decimal(raw["close"])
        except MarketDataPolicyError as exc:
            raise CurrencyPolicyError(exc.code, exc.detail) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CurrencyPolicyError(
                "integrity_error", "FX evidence is malformed."
            ) from exc
        assert close is not None
        if session < start or session >= end:
            raise CurrencyPolicyError(
                "integrity_error", "FX session is outside evidence bounds."
            )
        if session in seen:
            raise CurrencyPolicyError("fx_ambiguous", "FX sessions are duplicated.")
        if close <= 0:
            raise CurrencyPolicyError("integrity_error", "FX rate must be positive.")
        seen.add(session)
        closes.append((session, close))
    if tuple(session for session, _ in closes) != tuple(
        sorted(session for session, _ in closes)
    ):
        raise CurrencyPolicyError("fx_ambiguous", "FX sessions are unordered.")
    if not closes:
        raise CurrencyPolicyError("fx_missing", "Required GBP/USD FX close is missing.")
    return tuple(closes)


def convert_to_base(
    *,
    value: object,
    quote_currency: str,
    quote_unit: str,
    base_currency: str,
    valuation_session: date,
    completed_fx_through: date | None,
    fx_evidence: StoredHistoricalEvidence | None,
) -> CurrencyConversion:
    """Normalize quote units, then convert using bounded GBPUSD=X evidence."""
    if base_currency not in SUPPORTED_CURRENCIES:
        raise CurrencyPolicyError(
            "unsupported_currency", f"Unsupported base currency: {base_currency}."
        )
    source_amount, source_currency = _normalize_quote(value, quote_currency, quote_unit)
    if source_currency == base_currency:
        try:
            rounded_source = quantize_eight(source_amount)
        except MarketDataPolicyError as exc:
            raise CurrencyPolicyError(exc.code, exc.detail) from exc
        return CurrencyConversion(
            policy_version=CURRENCY_CONVERSION_POLICY_VERSION,
            source_amount=rounded_source,
            source_currency=source_currency,
            source_quote_unit=quote_unit,
            base_amount=rounded_source,
            base_currency=base_currency,
            fx_rate=None,
            fx_session=None,
            fx_revision=None,
        )

    if fx_evidence is None or completed_fx_through is None:
        raise CurrencyPolicyError(
            "fx_missing", "Required GBP/USD FX evidence is missing."
        )
    if completed_fx_through > valuation_session:
        raise CurrencyPolicyError(
            "fx_ambiguous", "FX completion bound is after the valuation session."
        )
    try:
        fx_start = date.fromisoformat(fx_evidence.start)
        fx_end = date.fromisoformat(fx_evidence.end)
    except (TypeError, ValueError) as exc:
        raise CurrencyPolicyError(
            "integrity_error", "FX evidence interval is malformed."
        ) from exc
    if not fx_start <= completed_fx_through < fx_end:
        raise CurrencyPolicyError(
            "fx_ambiguous",
            "FX completion bound is outside the exact evidence interval.",
        )
    eligible = tuple(
        row for row in _fx_closes(fx_evidence) if row[0] <= completed_fx_through
    )
    if not eligible:
        raise CurrencyPolicyError("fx_missing", "Required GBP/USD FX close is missing.")
    fx_session, rate = eligible[-1]
    age = (valuation_session - fx_session).days
    if age < 0:
        raise CurrencyPolicyError("fx_ambiguous", "FX evidence is from the future.")
    if age > 5:
        raise CurrencyPolicyError(
            "fx_stale", "GBP/USD FX evidence is more than five calendar days old."
        )

    try:
        with deterministic_decimal_context():
            if source_currency == "GBP" and base_currency == "USD":
                base_amount = source_amount * rate
            elif source_currency == "USD" and base_currency == "GBP":
                base_amount = source_amount / rate
            else:
                raise CurrencyPolicyError(
                    "unsupported_currency", "Currency conversion is unsupported."
                )
    except DecimalException as exc:
        raise CurrencyPolicyError(
            "integrity_error", "Currency conversion arithmetic failed."
        ) from exc
    try:
        rounded_base = quantize_eight(base_amount)
    except MarketDataPolicyError as exc:
        raise CurrencyPolicyError(exc.code, exc.detail) from exc
    return CurrencyConversion(
        policy_version=CURRENCY_CONVERSION_POLICY_VERSION,
        source_amount=source_amount,
        source_currency=source_currency,
        source_quote_unit=quote_unit,
        base_amount=rounded_base,
        base_currency=base_currency,
        fx_rate=rate,
        fx_session=fx_session,
        fx_revision=fx_evidence.data_revision,
    )
