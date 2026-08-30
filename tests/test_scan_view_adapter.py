"""Adapter tests for the current-scan ``MarketViewV1``/``PortfolioView``.

Offline: records are built in memory — no artifact file, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.backtest.market_view import PRICE_HISTORY_COLUMNS
from app.services.backtest.scan_view import (
    build_portfolio_view,
    build_scan_market_view,
)

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
    view, unresolved = build_scan_market_view(
        [_record("AAA", _bars(SESSION)), _record("AAA", _bars(SESSION))],
        {},
        as_of_session=SESSION,
    )
    assert view.selected_universe == ("AAA",)
    assert unresolved == ("AAA",)
