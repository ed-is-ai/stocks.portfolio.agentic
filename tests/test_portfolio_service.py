from typing import cast

from app.agents.analyst.exit_evaluator import ExitEvaluator
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


def test_to_gbp_converts_usd_and_passes_through_gbp() -> None:
    assert PortfolioService._to_gbp(200.0, "USD", 2.0) == 100.0
    assert PortfolioService._to_gbp(100.0, "GBP", 2.0) == 100.0


def test_current_prices_maps_ticker_to_price() -> None:
    records = [
        StockRecord(
            ticker="AAA",
            price=10.0,
            as_of="2024-01-01",
            volume=1000.0,
            rel_volume=1.0,
            high_52w=20.0,
            low_52w=5.0,
            pct_from_52w_high=-5.0,
            pct_change_week=1.0,
        ),
        StockRecord(
            ticker="BBB",
            price=20.0,
            as_of="2024-01-01",
            volume=1000.0,
            rel_volume=1.0,
            high_52w=30.0,
            low_52w=10.0,
            pct_from_52w_high=-10.0,
            pct_change_week=2.0,
        ),
    ]
    assert PortfolioService.current_prices(records) == {"AAA": 10.0, "BBB": 20.0}


def test_load_analysis_skips_invalid_records_without_dropping_valid(
    monkeypatch,
) -> None:
    """One malformed record (e.g. null price) must not blank the watchlist."""
    valid = {
        "ticker": "AAA",
        "price": 10.0,
        "as_of": "2024-01-01",
        "volume": 1000.0,
        "rel_volume": 1.0,
        "high_52w": 20.0,
        "low_52w": 5.0,
        "pct_from_52w_high": -5.0,
        "pct_change_week": 1.0,
    }
    invalid = {**valid, "ticker": "BAD", "price": None}
    import app.services.portfolio_service as module

    monkeypatch.setattr(module, "read_analysis_records", lambda _path: [invalid, valid])
    records = PortfolioService(
        cast(TraderService, _StubTrader()),
        cast(ExitEvaluator, _StubEvaluator()),
    ).load_analysis()
    assert [record.ticker for record in records] == ["AAA"]


class _StubTrader:
    def get_trade_history(self, ticker=None, portfolio_id=None):
        return []

    def list_portfolios(self):
        return []


class _StubEvaluator:
    def evaluate(self, position, stock):
        return None


def _make_service(monkeypatch) -> PortfolioService:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()),
        cast(ExitEvaluator, _StubEvaluator()),
    )
    monkeypatch.setattr(svc, "load_analysis", lambda: [])
    monkeypatch.setattr(
        svc,
        "_load_portfolio_history",
        lambda portfolio_id=None: {
            "labels": [],
            "values": [],
            "costs": [],
            "cash_values": [],
        },
    )
    return svc


def test_portfolio_totals_convert_usd_and_include_cash(monkeypatch) -> None:
    svc = _make_service(monkeypatch)
    positions = [
        Position(
            ticker="GBPCO",
            shares=1,
            avg_cost=100,
            total_cost=100,
            current_value=150,
            price_currency="GBP",
        ),
        Position(
            ticker="USDCO",
            shares=1,
            avg_cost=200,
            total_cost=200,
            current_value=270,
            price_currency="USD",
        ),
    ]
    ctx = svc.portfolio_partial_context(positions, gbpusd_rate=2.0, cash_balance=1000.0)
    assert ctx["total_cost_gbp"] == 1200.0
    assert ctx["total_value_gbp"] == 1285.0
    assert ctx["total_cost_gbp_valued"] == 200.0
    assert ctx["total_pnl_gbp"] == 85.0
    assert ctx["cash_balance"] == 1000.0


def test_position_without_current_value_excluded_from_value_totals(
    monkeypatch,
) -> None:
    svc = _make_service(monkeypatch)
    positions = [
        Position(
            ticker="VALUED",
            shares=1,
            avg_cost=100,
            total_cost=100,
            current_value=120,
            price_currency="GBP",
        ),
        Position(
            ticker="NOPRICE",
            shares=1,
            avg_cost=50,
            total_cost=50,
            current_value=None,
            price_currency="GBP",
        ),
    ]
    ctx = svc.portfolio_partial_context(positions, gbpusd_rate=2.0, cash_balance=0.0)
    assert ctx["total_cost_gbp"] == 150.0
    assert ctx["total_value_gbp"] == 120.0
    assert ctx["total_pnl_gbp"] == 20.0


def test_fetch_all_prices_assembles_results(monkeypatch) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()),
        cast(ExitEvaluator, _StubEvaluator()),
    )

    def fake_fetch(yf_sym, gbpusd):
        table = {"AAA": (10.0, 10.0, "GBP"), "BBB": (20.0, 20.0, "GBP")}
        return table.get(yf_sym)

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    prices, display = svc.fetch_all_prices(["AAA", "BBB"], {}, 1.35)
    assert prices == {"AAA": 10.0, "BBB": 20.0}
    assert display == {"AAA": (10.0, "GBP"), "BBB": (20.0, "GBP")}


def test_fetch_all_prices_retries_with_london_suffix(monkeypatch) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()),
        cast(ExitEvaluator, _StubEvaluator()),
    )
    calls: list[str] = []

    def fake_fetch(yf_sym, gbpusd):
        calls.append(yf_sym)
        if yf_sym == "VOD":
            return None
        if yf_sym == "VOD.L":
            return (1.23, 123.0, "GBP")
        return None

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    prices, display = svc.fetch_all_prices(["VOD"], {}, 1.35)
    assert prices == {"VOD": 1.23}
    assert "VOD.L" in calls


def test_fetch_all_prices_drops_below_threshold(monkeypatch) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()),
        cast(ExitEvaluator, _StubEvaluator()),
    )

    def fake_fetch(yf_sym, gbpusd):
        return (0.0, 0.0, "GBP")

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    prices, display = svc.fetch_all_prices(["ZZZ"], {"ZZZ": "ZZZ"}, 1.35)
    assert prices == {}
    assert display == {}
