from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import pytest
from curl_cffi.requests import exceptions as curl_exceptions

from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProbeDefinition,
    ProviderFailure,
    YFinanceQualificationAdapter,
    classify_missing_observation,
    fx_rate_is_fresh,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)


def _frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": [100.0, 50.0],
            "High": [102.0, 52.0],
            "Low": [99.0, 49.0],
            "Close": [101.0, 51.0],
            "Adj Close": [100.5, 50.5],
            "Volume": [1_000, 2_000],
            "Dividends": [0.25, 0.0],
            "Stock Splits": [0.0, 2.0],
        },
        index=index,
    )


def _definition(**overrides: Any) -> ProbeDefinition:
    values: dict[str, Any] = {
        "symbol": "AAPL",
        "start": date(2024, 1, 1),
        "end": date(2024, 2, 1),
        "expected_currency": "USD",
        "expected_quote_unit": "USD",
        "expected_timezone": "America/New_York",
        "expected_sessions": (date(2024, 1, 2), date(2024, 1, 3)),
        "allowed_observed_symbols": ("AAPL",),
    }
    values.update(overrides)
    return ProbeDefinition(**values)


class FakeTicker:
    def __init__(
        self, outcomes: list[object], metadata_outcomes: list[object] | None = None
    ) -> None:
        self.outcomes = outcomes
        self.metadata_outcomes = metadata_outcomes or [
            {
                "symbol": "AAPL",
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
                "instrumentType": "EQUITY",
            }
        ]
        self.calls: list[dict[str, object]] = []

    def history(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[bad-return]

    def get_history_metadata(self, repair: bool = False) -> dict[str, Any]:
        outcome = self.metadata_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[bad-return]


def test_adapter_binds_contract_retains_identity_and_uses_injected_clock() -> None:
    ticker = FakeTicker([_frame()])
    instant = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    payload = YFinanceQualificationAdapter(
        lambda _: ticker, clock=lambda: instant
    ).fetch(_definition())
    assert ticker.calls == [
        {
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
        }
    ]
    assert payload.requested_symbol == payload.observed_symbol == "AAPL"
    assert (payload.currency, payload.quote_unit, payload.quote_unit_scale) == (
        "USD",
        "USD",
        "1",
    )
    assert payload.acquired_at == "2026-08-10T12:00:00+00:00"
    assert payload.rows[0]["dividends"] == "0x1.0000000000000p-2"


def test_historical_adapter_allows_only_a_missing_prefix_for_full_history() -> None:
    frame = _frame()
    request = HistoricalEvidenceRequest(
        security_id="security-1",
        alias_revision="a" * 64,
        symbol="AAPL",
        start=date(1970, 1, 1),
        end=date(2024, 2, 1),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        expected_sessions=(date(1970, 1, 2), date(2024, 1, 2), date(2024, 1, 3)),
        allowed_observed_symbols=("AAPL",),
        allow_missing_prefix=True,
    )

    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker([frame]), provider_version="test"
    ).fetch(request)

    assert tuple(row["session"] for row in payload.rows) == (
        "2024-01-02",
        "2024-01-03",
    )
    assert "observation_policy" not in payload.request_contract


def test_content_digest_is_repeatable_and_detects_change() -> None:
    first = YFinanceQualificationAdapter(lambda _: FakeTicker([_frame()])).fetch(
        _definition()
    )
    same = YFinanceQualificationAdapter(lambda _: FakeTicker([_frame()])).fetch(
        _definition()
    )
    changed = _frame()
    changed.loc[changed.index[0], "Close"] = 101.5
    other = YFinanceQualificationAdapter(lambda _: FakeTicker([changed])).fetch(
        _definition()
    )
    assert first.content_digest == same.content_digest
    assert first.content_digest != other.content_digest


@pytest.mark.parametrize(
    ("outcome", "code", "attempts"),
    [
        (
            curl_exceptions.ConnectionError("offline"),
            FailureCode.PROVIDER_UNAVAILABLE,
            3,
        ),
        (curl_exceptions.ReadTimeout("timeout"), FailureCode.PROVIDER_UNAVAILABLE, 3),
        (RuntimeError("throttled"), FailureCode.PROVIDER_THROTTLED, 3),
        (RuntimeError("contract"), FailureCode.PROVIDER_CONTRACT_ERROR, 1),
    ],
)
def test_closed_retry_taxonomy_and_exact_delays(
    outcome: Exception, code: FailureCode, attempts: int
) -> None:
    ticker = FakeTicker([outcome] * attempts)
    sleeps: list[float] = []
    adapter = YFinanceQualificationAdapter(
        lambda _: ticker, sleeper=sleeps.append, jitter=lambda _key, _attempt: 0.125
    )
    with pytest.raises(ProviderFailure) as exc_info:
        adapter.fetch(_definition())
    assert exc_info.value.code is code
    assert len(ticker.calls) == attempts
    assert sleeps == ([1.125, 2.125] if attempts == 3 else [])


def test_metadata_transport_failure_is_inside_retry_boundary() -> None:
    tickers = [
        FakeTicker([_frame()], [curl_exceptions.ReadTimeout("timeout")]),
        FakeTicker([_frame()]),
    ]
    sleeps: list[float] = []
    payload = YFinanceQualificationAdapter(
        lambda _: tickers.pop(0), sleeper=sleeps.append, jitter=lambda *_: 0
    ).fetch(_definition())
    assert payload.observed_symbol == "AAPL"
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    "mutation", ["empty", "missing", "null", "duplicate", "naive", "partial", "outside"]
)
def test_partial_or_malformed_success_fails_immediately(mutation: str) -> None:
    frame = _frame()
    definition = _definition()
    if mutation == "empty":
        frame = pd.DataFrame()
    elif mutation == "missing":
        frame = frame.drop(columns=["Adj Close"])
    elif mutation == "null":
        frame.loc[frame.index[0], "Close"] = None
    elif mutation == "duplicate":
        frame.index = pd.DatetimeIndex([frame.index[0], frame.index[0]])
    elif mutation == "naive":
        frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
    elif mutation == "partial":
        definition = _definition(
            expected_sessions=(date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
        )
    elif mutation == "outside":
        definition = _definition(start=date(2024, 1, 3))
    ticker = FakeTicker([frame])
    with pytest.raises(ProviderFailure) as exc_info:
        YFinanceQualificationAdapter(lambda _: ticker).fetch(definition)
    assert exc_info.value.code in {
        FailureCode.REQUIRED_DATA_MISSING,
        FailureCode.PROVIDER_CONTRACT_ERROR,
    }
    assert len(ticker.calls) == 1


def test_provider_identity_conflict_is_ambiguous() -> None:
    ticker = FakeTicker(
        [_frame()],
        [
            {
                "symbol": "META",
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
            }
        ],
    )
    with pytest.raises(ProviderFailure) as exc_info:
        YFinanceQualificationAdapter(lambda _: ticker).fetch(_definition())
    assert exc_info.value.code is FailureCode.IDENTITY_AMBIGUOUS


def test_gbpence_is_distinct_quote_unit_with_scale() -> None:
    ticker = FakeTicker(
        [_frame()],
        [
            {
                "symbol": "ULVR.L",
                "currency": "GBp",
                "exchangeTimezoneName": "America/New_York",
            }
        ],
    )
    payload = YFinanceQualificationAdapter(lambda _: ticker).fetch(
        _definition(
            symbol="ULVR.L",
            expected_currency="GBP",
            expected_quote_unit="GBp",
            allowed_observed_symbols=("ULVR.L",),
        )
    )
    assert (payload.currency, payload.quote_unit, payload.quote_unit_scale) == (
        "GBP",
        "GBp",
        "0.01",
    )


def test_pre_first_observation_and_fx_staleness_boundaries() -> None:
    assert (
        classify_missing_observation(date(2010, 1, 1), date(2010, 1, 2))
        == "before_first_provider_observation"
    )
    with pytest.raises(ProviderFailure, match="on or after"):
        classify_missing_observation(date(2010, 1, 2), date(2010, 1, 2))
    assert fx_rate_is_fresh(date(2024, 1, 5), date(2024, 1, 10))
    assert not fx_rate_is_fresh(date(2024, 1, 5), date(2024, 1, 11))
