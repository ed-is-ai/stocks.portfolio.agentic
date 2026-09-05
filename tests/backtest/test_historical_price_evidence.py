from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from typing import Any

import pandas as pd
import pytest

from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceFxSeriesFetcher,
    YFinanceHistoricalEvidenceAdapter,
    rebind_historical_evidence_alias,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Adj Close": [100.5, 101.5],
            "Volume": [1_000.0, 2_000.0],
            "Dividends": [0.25, 0.0],
            "Stock Splits": [0.0, 2.0],
        },
        index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="America/New_York"),
    )


def _request(**overrides: Any) -> HistoricalEvidenceRequest:
    values: dict[str, Any] = {
        "security_id": "security-1",
        "alias_revision": "alias-v1",
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
    return HistoricalEvidenceRequest(**values)


def test_canonical_exchange_policy_filters_non_session_rows_and_allows_old_gaps() -> (
    None
):
    frame = _frame()
    frame.index = pd.DatetimeIndex(["2024-01-01", "2024-01-03"], tz="America/New_York")
    request = _request(
        expected_sessions=(
            date(2023, 12, 29),
            date(2024, 1, 2),
            date(2024, 1, 3),
        ),
        allow_missing_prefix=True,
        canonical_exchange_sessions=True,
    )

    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(frame), provider_version="test"
    ).fetch(request)

    assert tuple(row["session"] for row in payload.rows) == ("2024-01-03",)
    assert (
        payload.request_contract["observation_policy"]
        == "canonical_exchange_sessions_v2"
    )


def test_canonical_exchange_policy_omits_invalid_old_ohlc_rows() -> None:
    frame = _frame()
    frame.loc[frame.index[0], "High"] = 99.0

    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(frame), provider_version="test"
    ).fetch(_request(canonical_exchange_sessions=True))

    assert tuple(row["session"] for row in payload.rows) == ("2024-01-03",)


class FakeTicker:
    def __init__(
        self, frame: pd.DataFrame, metadata: dict[str, Any] | None = None
    ) -> None:
        self.frame = frame
        self.metadata = metadata or {
            "symbol": "AAPL",
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "instrumentType": "EQUITY",
        }
        self.calls: list[dict[str, object]] = []

    def history(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(kwargs)
        return self.frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, Any]:
        assert repair is False
        return dict(self.metadata)


def test_adapter_binds_request_and_builds_provider_native_revision() -> None:
    ticker = FakeTicker(_frame())
    acquired = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: ticker, clock=lambda: acquired
    ).fetch(_request())

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
    assert payload.security_id == "security-1"
    assert payload.requested_symbol == payload.observed_symbol == "AAPL"
    assert payload.rows[0]["open"] == float(100.0).hex()
    assert payload.actions == (
        {
            "session": "2024-01-02",
            "action_type": "dividend",
            "value": float(0.25).hex(),
        },
        {
            "session": "2024-01-03",
            "action_type": "split",
            "value": float(2.0).hex(),
        },
    )
    assert payload.acquired_at == "2026-08-11T09:30:00+00:00"
    assert len(payload.data_revision) == 64


def test_adapter_retries_a_transient_contract_mismatch() -> None:
    tickers = [
        FakeTicker(_frame(), {"symbol": "WRONG"}),
        FakeTicker(_frame()),
    ]
    sleeps: list[float] = []
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: tickers.pop(0),
        sleeper=sleeps.append,
        jitter=lambda *_: 0,
    ).fetch(_request())

    assert payload.observed_symbol == "AAPL"
    assert sleeps == [1.0]


def test_content_identity_excludes_acquisition_time_but_binds_security_and_rows() -> (
    None
):
    first = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(_frame()),
        clock=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
    ).fetch(_request())
    later = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(_frame()),
        clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    ).fetch(_request())
    changed_frame = _frame()
    changed_frame.loc[changed_frame.index[0], "Close"] = 101.25
    changed = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(changed_frame)
    ).fetch(_request())
    other_security = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(_frame())
    ).fetch(_request(security_id="security-2"))

    assert first.data_revision == later.data_revision
    assert first.acquired_at != later.acquired_at
    assert changed.data_revision != first.data_revision
    assert other_security.data_revision != first.data_revision


def test_verified_evidence_can_be_resealed_for_a_new_alias_revision() -> None:
    original = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(_frame()), provider_version="test"
    ).fetch(_request(alias_revision="a" * 64))

    rebound = rebind_historical_evidence_alias(
        original,
        alias_revision="b" * 64,
        acquired_at=original.acquired_at,
    )

    assert rebound.alias_revision == "b" * 64
    assert rebound.rows == original.rows
    assert rebound.actions == original.actions
    assert rebound.data_revision != original.data_revision
    assert manifest_digest(json.loads(rebound.canonical_manifest_json)) == (
        rebound.data_revision
    )


def test_adapter_canonicalizes_yfinance_dataframe_metadata() -> None:
    metadata = {
        "symbol": "AAPL",
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "tradingPeriods": pd.DataFrame(
            {"regular_start": [pd.Timestamp("2024-01-02T09:30:00-05:00")]}
        ),
    }

    first = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(_frame(), metadata)
    ).fetch(_request())
    second = YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(_frame(), metadata)
    ).fetch(_request())

    assert len(first.response_metadata_digest) == 64
    assert first.response_metadata_digest == second.response_metadata_digest


@pytest.mark.parametrize("mutation", ["nonfinite", "partial", "naive", "duplicate"])
def test_malformed_or_partial_success_fails_closed(mutation: str) -> None:
    frame = _frame()
    request = _request()
    if mutation == "nonfinite":
        frame.loc[frame.index[0], "Close"] = float("inf")
    elif mutation == "partial":
        request = _request(
            expected_sessions=(
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
            )
        )
    elif mutation == "naive":
        frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
    else:
        frame.index = pd.DatetimeIndex([frame.index[0], frame.index[0]])

    with pytest.raises(ProviderFailure) as exc_info:
        YFinanceHistoricalEvidenceAdapter(lambda _: FakeTicker(frame)).fetch(request)
    assert exc_info.value.code in {
        FailureCode.PROVIDER_CONTRACT_ERROR,
        FailureCode.REQUIRED_DATA_MISSING,
    }


def test_invalid_interval_fails_before_provider_access() -> None:
    called = False

    def factory(_symbol: str) -> FakeTicker:
        nonlocal called
        called = True
        return FakeTicker(_frame())

    with pytest.raises(ProviderFailure) as exc_info:
        YFinanceHistoricalEvidenceAdapter(factory).fetch(
            _request(start=date(2024, 2, 1), end=date(2024, 2, 1))
        )
    assert exc_info.value.code is FailureCode.PROVIDER_CONTRACT_ERROR
    assert called is False


# ---------------------------------------------------------------------------
# YFinanceFxSeriesFetcher -- the #459 daily GBPUSD=X series ingestion path
# ---------------------------------------------------------------------------


def _fx_frame(sessions: tuple[date, ...], rate: float = 1.27) -> pd.DataFrame:
    """A flat daily ``GBPUSD=X`` frame covering exactly ``sessions``."""
    return pd.DataFrame(
        {
            "Open": [rate] * len(sessions),
            "High": [rate] * len(sessions),
            "Low": [rate] * len(sessions),
            "Close": [rate] * len(sessions),
            "Adj Close": [rate] * len(sessions),
            "Volume": [0.0] * len(sessions),
            "Dividends": [0.0] * len(sessions),
            "Stock Splits": [0.0] * len(sessions),
        },
        index=pd.DatetimeIndex([s.isoformat() for s in sessions], tz="UTC"),
    )


def _fx_fetcher(frame: pd.DataFrame) -> Any:
    class _Ticker:
        def history(self, **_kwargs: object) -> pd.DataFrame:
            return frame.copy()

        def get_history_metadata(self, repair: bool = False) -> dict[str, Any]:
            return {
                "symbol": "GBPUSD=X",
                "currency": "USD",
                "exchangeTimezoneName": "Europe/London",
            }

    return YFinanceFxSeriesFetcher(
        adapter=YFinanceHistoricalEvidenceAdapter(
            lambda _symbol: _Ticker(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),  # noqa: E501
        )
    )


def test_fx_series_fetcher_produces_engine_contract_payload() -> None:
    """The ingested payload carries the FX pseudo-security identity and the
    provider-native shape ``currency.py::_fx_closes`` demands (#459)."""
    sessions = tuple(date(2026, 1, 5) + timedelta(days=offset) for offset in range(5))
    fetcher = _fx_fetcher(_fx_frame(sessions))

    payload = fetcher.fetch(start=date(2026, 1, 5), end=date(2026, 1, 10))

    assert payload.security_id == "fx:GBPUSD=X"
    assert payload.requested_symbol == "GBPUSD=X"
    assert payload.observed_symbol == "GBPUSD=X"
    assert payload.provider == "yfinance"
    assert payload.currency == "USD"
    assert payload.quote_unit == "USD"
    assert payload.quote_unit_scale == "1"
    assert payload.exchange_timezone == "Europe/London"
    assert payload.actions == ()
    assert [row["session"] for row in payload.rows] == [
        session.isoformat() for session in sessions
    ]
    assert len(payload.data_revision) == 64


def test_fx_series_fetcher_drops_rows_outside_the_window() -> None:
    """Provider rows outside the requested window never reach the
    committed evidence -- the series spans exactly the run window."""
    sessions = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(10))
    fetcher = _fx_fetcher(_fx_frame(sessions))

    payload = fetcher.fetch(start=date(2026, 1, 4), end=date(2026, 1, 7))

    assert [row["session"] for row in payload.rows] == [
        "2026-01-04",
        "2026-01-05",
        "2026-01-06",
    ]


def test_fx_series_fetcher_rejects_an_empty_window() -> None:
    """A degenerate window fails closed before any provider access."""
    fetcher = _fx_fetcher(_fx_frame((date(2026, 1, 5),)))

    with pytest.raises(ProviderFailure) as exc_info:
        fetcher.fetch(start=date(2026, 1, 10), end=date(2026, 1, 10))

    assert exc_info.value.code is FailureCode.PROVIDER_CONTRACT_ERROR


def test_fx_series_fetcher_fails_when_the_provider_serves_nothing() -> None:
    """An empty provider frame is a definitive REQUIRED_DATA_MISSING -- the
    preparation caller turns it into an actionable failure message."""
    fetcher = _fx_fetcher(
        pd.DataFrame(
            {"Close": []},
            index=pd.DatetimeIndex([], tz="UTC"),
        )
    )

    with pytest.raises(ProviderFailure) as exc_info:
        fetcher.fetch(start=date(2026, 1, 5), end=date(2026, 1, 10))

    assert exc_info.value.code is FailureCode.REQUIRED_DATA_MISSING
