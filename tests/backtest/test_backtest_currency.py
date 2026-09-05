from __future__ import annotations

from datetime import date
from decimal import Decimal, Inexact, ROUND_UP, localcontext

import pytest

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.currency import (
    CURRENCY_CONVERSION_POLICY_VERSION,
    CurrencyPolicyError,
    convert_to_base,
)


def _hex(value: float) -> str:
    return float(value).hex()


def _fx_evidence(
    closes: tuple[tuple[str, float], ...] = (("2024-01-05", 1.25),),
    **overrides: object,
) -> StoredHistoricalEvidence:
    values: dict[str, object] = {
        "data_revision": "fx-revision-1",
        "security_id": "fx-security",
        "provider": "yfinance",
        "provider_version": "1.4.1",
        "request_contract_version": "YFinanceDailyProviderNativeV1",
        "requested_symbol": "GBPUSD=X",
        "observed_symbol": "GBPUSD=X",
        "alias_revision": "fx-alias-v1",
        "currency": "USD",
        "quote_unit": "USD",
        "quote_unit_scale": "1",
        "exchange_timezone": "Europe/London",
        "start": "2024-01-01",
        "end": "2024-02-01",
        "request_contract": {
            "start": "2024-01-01",
            "end": "2024-02-01",
            "interval": "1d",
            "prepost": False,
            "auto_adjust": False,
            "back_adjust": False,
            "actions": True,
            "repair": False,
            "keepna": True,
            "rounding": False,
            "timeout": 15,
            "raise_errors": True,
        },
        "response_metadata_digest": "metadata",
        "canonical_manifest_json": "{}",
        "rows": tuple(
            {
                "session": session,
                "open": _hex(close),
                "high": _hex(close),
                "low": _hex(close),
                "close": _hex(close),
                "adj_close": _hex(close),
                "volume": _hex(0),
                "dividends": _hex(0),
                "stock_splits": _hex(0),
            }
            for session, close in closes
        ),
        "actions": (),
    }
    values.update(overrides)
    return StoredHistoricalEvidence(**values)  # type: ignore[arg-type]


def test_gbp_pence_scales_before_gbp_to_usd_conversion() -> None:
    result = convert_to_base(
        value="1000",
        quote_currency="GBP",
        quote_unit="GBp",
        base_currency="USD",
        valuation_session=date(2024, 1, 8),
        completed_fx_through=date(2024, 1, 5),
        fx_evidence=_fx_evidence(),
    )
    assert result.source_amount == Decimal("10.00000000")
    assert result.base_amount == Decimal("12.50000000")
    assert result.fx_rate == Decimal("1.25")
    assert result.fx_session == date(2024, 1, 5)
    assert result.fx_revision == "fx-revision-1"
    assert result.policy_version == CURRENCY_CONVERSION_POLICY_VERSION


def test_usd_to_gbp_divides_and_same_currency_does_not_require_fx() -> None:
    converted = convert_to_base(
        value=Decimal("12.5"),
        quote_currency="USD",
        quote_unit="USD",
        base_currency="GBP",
        valuation_session=date(2024, 1, 8),
        completed_fx_through=date(2024, 1, 5),
        fx_evidence=_fx_evidence(),
    )
    assert converted.base_amount == Decimal("10.00000000")

    same = convert_to_base(
        value="1.234567885",
        quote_currency="GBP",
        quote_unit="GBP",
        base_currency="GBP",
        valuation_session=date(2024, 1, 8),
        completed_fx_through=None,
        fx_evidence=None,
    )
    assert same.base_amount == Decimal("1.23456788")
    assert same.fx_rate is None


@pytest.mark.parametrize(
    ("valuation", "expected_code"),
    [(date(2024, 1, 10), None), (date(2024, 1, 11), "fx_stale")],
)
def test_fx_carry_is_five_calendar_days_maximum(valuation, expected_code) -> None:
    kwargs = dict(
        value="10",
        quote_currency="GBP",
        quote_unit="GBP",
        base_currency="USD",
        valuation_session=valuation,
        completed_fx_through=date(2024, 1, 10),
        fx_evidence=_fx_evidence(),
    )
    if expected_code is None:
        assert convert_to_base(**kwargs).base_amount == Decimal("12.50000000")
    else:
        with pytest.raises(CurrencyPolicyError) as exc_info:
            convert_to_base(**kwargs)
        assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"fx_evidence": None}, "fx_missing"),
        ({"quote_unit": "EUR", "quote_currency": "EUR"}, "unsupported_currency"),
        ({"base_currency": "HKD"}, "unsupported_currency"),
        ({"fx_evidence": _fx_evidence(requested_symbol="USDGBP=X")}, "fx_ambiguous"),
        (
            {"fx_evidence": _fx_evidence((("2024-01-05", 1.25), ("2024-01-05", 1.25)))},
            "fx_ambiguous",
        ),
        (
            {
                "fx_evidence": _fx_evidence(
                    actions=(
                        {
                            "session": "2024-01-05",
                            "action_type": "dividend",
                            "value": _hex(1),
                        },
                    )
                )
            },
            "fx_ambiguous",
        ),
        ({"fx_evidence": _fx_evidence((("2024-01-05", 0),))}, "integrity_error"),
    ],
)
def test_invalid_currency_or_fx_evidence_fails_visibly(changes, expected_code) -> None:
    kwargs = {
        "value": "10",
        "quote_currency": "GBP",
        "quote_unit": "GBP",
        "base_currency": "USD",
        "valuation_session": date(2024, 1, 8),
        "completed_fx_through": date(2024, 1, 5),
        "fx_evidence": _fx_evidence(),
    }
    kwargs.update(changes)
    with pytest.raises(CurrencyPolicyError) as exc_info:
        convert_to_base(**kwargs)
    assert exc_info.value.code == expected_code


def test_completion_bound_never_selects_a_later_fx_close() -> None:
    result = convert_to_base(
        value="10",
        quote_currency="GBP",
        quote_unit="GBP",
        base_currency="USD",
        valuation_session=date(2024, 1, 8),
        completed_fx_through=date(2024, 1, 5),
        fx_evidence=_fx_evidence((("2024-01-05", 1.25), ("2024-01-08", 1.50))),
    )
    assert result.base_amount == Decimal("12.50000000")
    assert result.fx_session == date(2024, 1, 5)


def test_fx_quantizes_only_after_quote_scale_and_conversion() -> None:
    result = convert_to_base(
        value="0.0000005",
        quote_currency="GBP",
        quote_unit="GBp",
        base_currency="USD",
        valuation_session=date(2024, 1, 5),
        completed_fx_through=date(2024, 1, 5),
        fx_evidence=_fx_evidence(),
    )
    assert result.source_amount == Decimal("0.000000005")
    assert result.base_amount == Decimal("0.00000001")


def test_fx_completion_bound_must_be_covered_by_exact_revision() -> None:
    evidence = _fx_evidence(
        start="2024-01-01",
        end="2024-01-06",
        request_contract={
            **_fx_evidence().request_contract,
            "end": "2024-01-06",
        },
    )
    with pytest.raises(CurrencyPolicyError) as exc_info:
        convert_to_base(
            value="10",
            quote_currency="GBP",
            quote_unit="GBP",
            base_currency="USD",
            valuation_session=date(2024, 1, 10),
            completed_fx_through=date(2024, 1, 10),
            fx_evidence=evidence,
        )
    assert exc_info.value.code == "fx_ambiguous"


def test_fx_rejects_incompatible_provider_request_contract() -> None:
    contract = dict(_fx_evidence().request_contract)
    contract["repair"] = True
    with pytest.raises(CurrencyPolicyError) as exc_info:
        convert_to_base(
            value="10",
            quote_currency="GBP",
            quote_unit="GBP",
            base_currency="USD",
            valuation_session=date(2024, 1, 5),
            completed_fx_through=date(2024, 1, 5),
            fx_evidence=_fx_evidence(request_contract=contract),
        )
    assert exc_info.value.code == "fx_ambiguous"


def test_currency_arithmetic_ignores_ambient_rounding_and_traps() -> None:
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        result = convert_to_base(
            value="1",
            quote_currency="USD",
            quote_unit="USD",
            base_currency="GBP",
            valuation_session=date(2024, 1, 5),
            completed_fx_through=date(2024, 1, 5),
            fx_evidence=_fx_evidence((("2024-01-05", 3.0),)),
        )
    assert result.base_amount == Decimal("0.33333333")
