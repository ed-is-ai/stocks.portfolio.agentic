"""Tests for the shared market-regime helper."""

import math

import pandas as pd
import pytest

from app.core import market_regime
from app.core.market_regime import (
    RETURN_LOOKBACK_SESSIONS,
    evaluate_market_regime,
    fetch_spy_market_regime,
)


def test_risk_on_uptrend() -> None:
    closes = [100.0] * 200 + [130.0] * 20
    reading = evaluate_market_regime(closes)
    assert reading.spy_uptrend is True
    assert reading.is_degraded is False
    assert reading.session_count == 220
    n = len(closes)
    oldest = closes[max(0, n - RETURN_LOOKBACK_SESSIONS)]
    assert reading.return_52w_pct == round((closes[-1] / oldest - 1) * 100, 2)


def test_risk_off_downtrend() -> None:
    closes = [200.0] * 200 + [100.0] * 20
    reading = evaluate_market_regime(closes)
    assert reading.spy_uptrend is False
    assert reading.is_degraded is False


def test_exactly_on_sma_is_not_uptrend() -> None:
    closes = [100.0] * 250
    reading = evaluate_market_regime(closes)
    assert reading.sma_200 == 100.0
    assert reading.latest_close == 100.0
    assert reading.spy_uptrend is False


def test_fewer_than_ma_length_is_degraded() -> None:
    reading = evaluate_market_regime([100.0] * 199)
    assert reading.is_degraded is True
    assert reading.spy_uptrend is True
    assert reading.return_52w_pct == 0.0
    assert reading.sma_200 is None
    assert reading.latest_close is None
    assert reading.session_count == 199


def test_empty_series_is_degraded() -> None:
    reading = evaluate_market_regime([])
    assert reading.is_degraded is True
    assert reading.session_count == 0


def test_exactly_200_rows() -> None:
    closes = [float(i) for i in range(1, 201)]
    reading = evaluate_market_regime(closes)
    assert reading.is_degraded is False
    assert reading.session_count == 200
    assert reading.sma_200 == sum(closes) / 200
    # n - 252 < 0 so oldest clamps to closes[0]
    assert reading.return_52w_pct == round((closes[-1] / closes[0] - 1) * 100, 2)


def test_210_row_series_clamps_lookback_to_first() -> None:
    closes = [float(i) for i in range(1, 211)]
    reading = evaluate_market_regime(closes)
    assert reading.session_count == 210
    assert reading.return_52w_pct == round((210.0 / 1.0 - 1) * 100, 2)


def test_non_finite_values_dropped() -> None:
    closes = [float("nan"), float("inf"), -float("inf")] + [100.0] * 199 + [150.0]
    reading = evaluate_market_regime(closes)
    assert reading.session_count == 200
    assert reading.is_degraded is False
    assert reading.latest_close == 150.0


def test_non_finite_values_can_force_degraded() -> None:
    closes = [float("nan")] * 5 + [100.0] * 150
    reading = evaluate_market_regime(closes)
    assert reading.session_count == 150
    assert reading.is_degraded is True


def test_accepts_pandas_series() -> None:
    closes = pd.Series([100.0] * 200 + [130.0] * 20)
    reading = evaluate_market_regime(closes)
    assert reading.spy_uptrend is True


def test_evaluate_never_raises_on_weird_input() -> None:
    reading = evaluate_market_regime(pd.Series([], dtype="float64"))
    assert reading.is_degraded is True


def _spy_frame(closes: list[float], multiindex: bool = False) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    df = pd.DataFrame({"Close": closes}, index=index)
    if multiindex:
        df.columns = pd.MultiIndex.from_tuples([("Close", "SPY")])
    return df


def test_fetch_normal_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = [100.0] * 200 + [130.0] * 20
    monkeypatch.setattr("yfinance.download", lambda *a, **k: _spy_frame(closes))
    reading = fetch_spy_market_regime()
    assert reading.is_degraded is False
    assert reading.spy_uptrend is True


def test_fetch_multiindex_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = [100.0] * 200 + [130.0] * 20
    monkeypatch.setattr(
        "yfinance.download", lambda *a, **k: _spy_frame(closes, multiindex=True)
    )
    reading = fetch_spy_market_regime()
    assert reading.is_degraded is False
    assert reading.spy_uptrend is True


def test_fetch_empty_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yfinance.download", lambda *a, **k: pd.DataFrame())
    reading = fetch_spy_market_regime()
    assert reading.is_degraded is True
    assert reading.spy_uptrend is True
    assert reading.return_52w_pct == 0.0


def test_fetch_short_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yfinance.download", lambda *a, **k: _spy_frame([100.0] * 150))
    reading = fetch_spy_market_regime()
    assert reading.is_degraded is True


def test_fetch_raises_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> pd.DataFrame:
        raise RuntimeError("network down")

    monkeypatch.setattr("yfinance.download", _boom)
    reading = fetch_spy_market_regime()
    assert reading.is_degraded is True
    assert reading.spy_uptrend is True
    assert reading.return_52w_pct == 0.0


def test_module_has_no_agent_or_repository_imports() -> None:
    import inspect

    src = inspect.getsource(market_regime)
    assert "app.agents" not in src
    assert "app.repositories" not in src
    assert math.isfinite(1.0)
