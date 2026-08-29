"""Persistence tests for the scanner-screen market-regime snapshot (#387)."""

from __future__ import annotations

import json

import app.agents.scanner.market_regime_snapshot as snap
from app.core.market_regime import MarketRegimeReadingV1
from app.schemas.market_regime import MarketRegimeSnapshotV1


def _reading(return_52w_pct: float = 15.5) -> MarketRegimeReadingV1:
    return MarketRegimeReadingV1(
        spy_uptrend=True,
        return_52w_pct=return_52w_pct,
        sma_200=420.0,
        latest_close=470.0,
        session_count=252,
        is_degraded=False,
    )


def test_round_trip_save_and_load(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(snap, "MARKET_REGIME_JSON", tmp_path / "regime.json")
    snap.save_market_regime(_reading())

    loaded = snap.load_market_regime()
    assert loaded is not None
    assert loaded.spy_uptrend is True
    assert loaded.return_52w_pct == 15.5
    assert loaded.sma_200 == 420.0
    assert loaded.latest_close == 470.0
    assert loaded.session_count == 252
    assert loaded.is_degraded is False
    assert loaded.generated_at != ""


def test_missing_file_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(snap, "MARKET_REGIME_JSON", tmp_path / "missing.json")
    assert snap.load_market_regime() is None


def test_corrupt_file_returns_none(tmp_path, monkeypatch) -> None:
    path = tmp_path / "regime.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(snap, "MARKET_REGIME_JSON", path)
    assert snap.load_market_regime() is None


def test_non_finite_return_on_disk_returns_none(tmp_path, monkeypatch) -> None:
    path = tmp_path / "regime.json"
    path.write_text(
        json.dumps(
            {
                "spy_uptrend": True,
                "return_52w_pct": float("nan"),
                "sma_200": 1.0,
                "latest_close": 1.0,
                "session_count": 252,
                "is_degraded": False,
                "generated_at": "2026-08-29T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(snap, "MARKET_REGIME_JSON", path)
    assert snap.load_market_regime() is None


def test_schema_drift_guard() -> None:
    assert set(MarketRegimeReadingV1.__dataclass_fields__) <= set(
        MarketRegimeSnapshotV1.model_fields
    )
