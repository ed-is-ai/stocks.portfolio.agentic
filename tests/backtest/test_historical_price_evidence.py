from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import pytest

from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
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
