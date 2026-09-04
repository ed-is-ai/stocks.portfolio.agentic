"""Adapter tests for the current-scan ``MarketViewV1``/``PortfolioView``.

Offline: records are built in memory — no artifact file, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.agents.scanner.scanner_agent import _build_current_evidence
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceSuccessV1,
)
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.backtest.market_view import PRICE_HISTORY_COLUMNS
from app.services.backtest.scan_view import (
    build_portfolio_view,
    build_scan_market_view,
)
from app.services.backtest.strategy_evidence import EvidenceKind
from app.services.backtest.trading_calendar import TradingCalendar

SESSION = date(2026, 8, 28)
PREVIOUS = date(2026, 8, 27)


def _bars(*sessions: date) -> list[dict[str, float | int | str]]:
    """Newest-first daily bars (the artifact's convention)."""
    return [
        {
            "date": session.isoformat(),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 1000,
        }
        for session in sorted(sessions, reverse=True)
    ]


def _record(ticker: str, bars: list[dict[str, float | int | str]]) -> StockRecord:
    return StockRecord.model_validate(
        {
            "ticker": ticker,
            "as_of": SESSION.isoformat(),
            "price": 10.0,
            "volume": 1000,
            "rel_volume": 1.0,
            "high_52w": 11.0,
            "low_52w": 9.0,
            "pct_from_52w_high": -1.0,
            "pct_change_week": 0.5,
            "ohlcv_history": bars,
        }
    )


def test_history_is_oldest_first_and_bounded_to_as_of_session() -> None:
    view, unresolved = build_scan_market_view(
        [_record("AAA", _bars(PREVIOUS, SESSION))], {}
    )
    assert unresolved == ()
    assert view.as_of_session == SESSION
    frame = view.price_history("AAA")
    assert list(frame.index) == [PREVIOUS, SESSION]
    assert frame.index[-1] == view.as_of_session


def test_price_history_columns_values_and_index() -> None:
    view, _ = build_scan_market_view([_record("AAA", _bars(SESSION))], {})
    frame = view.price_history("AAA")
    assert tuple(frame.columns) == PRICE_HISTORY_COLUMNS
    assert frame.index.name == "session"
    assert all(isinstance(value, Decimal) for value in frame["close"])
    assert all(isinstance(session, date) for session in frame.index)


def test_newest_first_input_is_reversed() -> None:
    bars = _bars(PREVIOUS, SESSION)
    assert date.fromisoformat(str(bars[0]["date"])) == SESSION
    view, _ = build_scan_market_view([_record("AAA", bars)], {})
    assert list(view.price_history("AAA").index) == [PREVIOUS, SESSION]


def test_alias_resolves_to_canonical_security_id() -> None:
    view, unresolved = build_scan_market_view(
        [_record("OLD", _bars(SESSION))], {"OLD": "AAA"}
    )
    assert unresolved == ()
    assert view.selected_universe == ("AAA",)
    assert not view.price_history("AAA").empty


def test_ambiguous_alias_is_surfaced_not_dropped() -> None:
    view, unresolved = build_scan_market_view(
        [_record("CYC1", _bars(SESSION))],
        {"CYC1": "CYC2", "CYC2": "CYC1"},
        as_of_session=SESSION,
    )
    assert view.selected_universe == ()
    assert unresolved == ("CYC1",)


def test_non_evidenced_security_returns_empty_frame() -> None:
    """Unknown/held-but-absent securities answer empty, per the protocol."""
    view, _ = build_scan_market_view([_record("AAA", _bars(SESSION))], {})
    frame = view.price_history("BBB")
    assert frame.empty
    assert tuple(frame.columns) == PRICE_HISTORY_COLUMNS


def test_in_universe_without_data_returns_empty_frame() -> None:
    view, _ = build_scan_market_view([_record("AAA", [])], {}, as_of_session=SESSION)
    frame = view.price_history("AAA")
    assert frame.empty
    assert tuple(frame.columns) == PRICE_HISTORY_COLUMNS


def test_scan_result_projection_has_no_fabricated_fields() -> None:
    view, _ = build_scan_market_view([_record("AAA", _bars(SESSION))], {})
    record = view.scan_result("AAA")
    assert record is not None
    assert record.security_id == "AAA"
    assert record.as_of_session_date == SESSION
    assert record.stage is None
    assert record.vcp is None
    assert record.technicals is None
    assert view.scan_result("BBB") is None


def test_complete_current_bundle_exposes_typed_scan_coverage() -> None:
    sessions = (
        TradingCalendar()._calendar("XNAS").sessions_window(pd.Timestamp(SESSION), -252)
    )
    frame = pd.DataFrame(
        {
            "open": [100.0 + index / 10 for index in range(252)],
            "high": [101.0 + index / 10 for index in range(252)],
            "low": [99.0 + index / 10 for index in range(252)],
            "close": [100.5 + index / 10 for index in range(252)],
            "volume": [1000.0 + index for index in range(252)],
        },
        index=sessions,
    )
    entry = _build_current_evidence("AAA", frame)
    assert isinstance(entry, CurrentEvidenceSuccessV1)
    bars = [
        {
            "date": stamp.date().isoformat(),
            **{
                name: float(str(frame.loc[stamp, name]))
                for name in PRICE_HISTORY_COLUMNS
            },
        }
        for stamp in reversed(sessions)
    ]
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-a", as_of_session=SESSION, entries=(entry,)
    )

    view, unresolved = build_scan_market_view(
        [_record("AAA", bars)], {}, current_evidence=evidence
    )
    result = view.scan_result("AAA")

    assert unresolved == ()
    assert result is not None
    assert result.technicals is not None
    assert result.stage is not None
    assert result.vcp is not None
    assert view.evidence_capabilities == frozenset(EvidenceKind)
    assert view.evidence_coverage("AAA").kinds == frozenset(EvidenceKind)


def test_stale_evidence_is_excluded_and_surfaced() -> None:
    older = date(2026, 8, 20)
    view, unresolved = build_scan_market_view(
        [_record("AAA", _bars(SESSION)), _record("OLD", _bars(older))],
        {},
        as_of_session=SESSION,
    )
    assert view.selected_universe == ("AAA",)
    assert unresolved == ("OLD",)


def test_build_portfolio_view_maps_and_skips() -> None:
    positions = [
        Position(ticker="AAA", shares=100.0, avg_cost=10.0, total_cost=1000.0),
        Position(ticker="ZERO", shares=0.0, avg_cost=5.0, total_cost=0.0),
        Position(ticker="NOCOST", shares=10.0, avg_cost=0.0, total_cost=0.0),
    ]
    view = build_portfolio_view(positions, 500.0, SESSION)
    assert view.as_of_session == SESSION
    assert view.base_currency == "GBP"
    assert view.cash == Decimal("500.0")
    assert len(view.positions) == 1
    assert view.positions[0].security_id == "AAA"
    assert view.positions[0].quantity == Decimal("100.0")
    assert view.positions[0].average_cost == Decimal("10.0")
    assert view.volatility_observations == ()


def test_malformed_bar_missing_key_raises_value_error() -> None:
    bars = _bars(SESSION)
    del bars[0]["volume"]
    record = StockRecord.model_validate(
        {
            **_record("AAA", []).model_dump(exclude={"ohlcv_history"}),
            "ohlcv_history": bars,
        }
    )
    with pytest.raises(ValueError, match="missing"):
        build_scan_market_view([record], {}, as_of_session=SESSION)


def test_non_numeric_bar_value_raises_value_error() -> None:
    bars = _bars(SESSION)
    bars[0]["close"] = "n/a"
    record = StockRecord.model_validate(
        {
            **_record("AAA", []).model_dump(exclude={"ohlcv_history"}),
            "ohlcv_history": bars,
        }
    )
    with pytest.raises(ValueError, match="malformed"):
        build_scan_market_view([record], {}, as_of_session=SESSION)


def test_non_finite_bar_value_raises_value_error() -> None:
    bars = _bars(SESSION)
    bars[0]["close"] = float("nan")
    record = StockRecord.model_validate(
        {
            **_record("AAA", []).model_dump(exclude={"ohlcv_history"}),
            "ohlcv_history": bars,
        }
    )
    with pytest.raises(ValueError, match="non-finite"):
        build_scan_market_view([record], {}, as_of_session=SESSION)


def test_out_of_order_sessions_raise_value_error() -> None:
    bars = list(reversed(_bars(PREVIOUS, SESSION)))  # oldest-first input
    record = StockRecord.model_validate(
        {
            **_record("AAA", []).model_dump(exclude={"ohlcv_history"}),
            "ohlcv_history": bars,
        }
    )
    with pytest.raises(ValueError, match="out-of-order"):
        build_scan_market_view([record], {}, as_of_session=SESSION)


def test_duplicate_canonical_records_are_surfaced() -> None:
    """Both raw spellings that collide on one canonical id must be surfaced.

    Uses two *different* raw tickers aliased to the same canonical id --
    identical strings would let a bug that only surfaces the second
    colliding ticker (and silently drops the first) pass unnoticed.
    """
    aliases = {"AAA": "CANON", "BBB": "CANON"}
    view, unresolved = build_scan_market_view(
        [_record("AAA", _bars(SESSION)), _record("BBB", _bars(SESSION))],
        aliases,
        as_of_session=SESSION,
    )
    assert view.selected_universe == ()
    assert set(unresolved) == {"AAA", "BBB"}
