"""Regression tests for issue #57 — email/UI portfolio price parity.

Before the fix, the orchestrator's email snapshot priced holdings only from
this run's scanned watchlist (``analysis_results``), so any held ticker
outside that watchlist (or priced in a non-GBP currency) came out blank or
wrong, while the ``/portfolio`` route priced every actual holding via a
cache-first live fetch with GBP/USD normalisation. This module asserts the
two paths now agree, sourced from the same price cache, and that pricing a
held ticker already in the cache costs zero net-new fetches while a cache
miss triggers exactly one.
"""

from pathlib import Path

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.schemas.trade import Position
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


@pytest.fixture
def trader(tmp_path: Path) -> TraderAgent:
    """A TraderAgent backed by an isolated, empty SQLite DB."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    return agent


def _service(trader: TraderAgent) -> PortfolioService:
    return PortfolioService(TraderService(trader))


def test_get_prices_for_holdings_fetches_only_cache_misses(
    monkeypatch, trader: TraderAgent
) -> None:
    """A held ticker outside the watchlist (cache miss) is fetched exactly
    once; a ticker already in the cache triggers zero net-new fetches."""
    # WATCHED is in this run's scan watchlist and already cached (as the web
    # UI would have left it). OFFLIST is held but never scanned or cached —
    # this is the exact blank-value bug from the issue.
    trader.record_buy("WATCHED", 10.0, 100.0, "2026-01-01")
    trader.record_buy("OFFLIST", 5.0, 50.0, "2026-01-01")  # USD holding
    trader.save_price_cache({"WATCHED": 120.0}, {"WATCHED": (120.0, "GBP")})

    monkeypatch.setattr(
        PortfolioService, "_fetch_gbpusd_rate", staticmethod(lambda: 1.25)
    )

    fetch_calls: list[str] = []

    def fake_fetch_price_gbp(self, yf_sym, gbpusd):
        fetch_calls.append(yf_sym)
        assert yf_sym == "OFFLIST"  # WATCHED must never be re-fetched
        return (60.0 / gbpusd, 60.0, "USD")  # native USD price, GBP-converted

    monkeypatch.setattr(PortfolioService, "_fetch_price_gbp", fake_fetch_price_gbp)

    service = _service(trader)
    held_tickers = [p.ticker for p in trader.get_portfolio()]
    prices, display_info, gbpusd = service.get_prices_for_holdings(held_tickers)

    assert fetch_calls == ["OFFLIST"]  # exactly one fetch, for the miss only
    assert prices["WATCHED"] == 120.0  # served from cache, untouched
    assert display_info["OFFLIST"] == (60.0, "USD")
    assert gbpusd == 1.25

    # The fetched price must now be persisted so it's reused (no re-fetch).
    cached_prices, _, cached_display = trader.load_price_cache()
    assert cached_prices["OFFLIST"] == pytest.approx(60.0 / 1.25)
    assert cached_display["OFFLIST"] == (60.0, "USD")

    fetch_calls.clear()
    service.get_prices_for_holdings(held_tickers)
    assert fetch_calls == []  # now fully cached: zero net-new fetches


def test_email_and_ui_paths_agree_on_value_and_pnl(
    monkeypatch, trader: TraderAgent
) -> None:
    """The orchestrator's cache-first pricing and the /portfolio route's
    cache-based view must compute identical current_value/unrealised_pnl for
    every holding, including a non-GBP position."""
    trader.record_buy("WATCHED", 10.0, 100.0, "2026-01-01")
    trader.record_buy("OFFLIST", 5.0, 50.0, "2026-01-01")
    trader.save_price_cache({"WATCHED": 120.0}, {"WATCHED": (120.0, "GBP")})

    monkeypatch.setattr(
        PortfolioService, "_fetch_gbpusd_rate", staticmethod(lambda: 1.25)
    )
    monkeypatch.setattr(
        PortfolioService,
        "_fetch_price_gbp",
        lambda self, yf_sym, gbpusd: (60.0 / gbpusd, 60.0, "USD"),
    )

    service = _service(trader)

    # Orchestrator/email path: price every actual holding, cache-first.
    held_tickers = [p.ticker for p in trader.get_portfolio()]
    prices, display_info, gbpusd = service.get_prices_for_holdings(held_tickers)
    email_positions = trader.get_portfolio(prices, display_info)

    # /portfolio route's cache-based view, reading the same (now fully
    # populated) cache the email path just wrote to.
    cached_prices, _, cached_display = trader.load_price_cache()
    ui_positions = trader.get_portfolio(cached_prices, cached_display)

    email_by_ticker = {p.ticker: p for p in email_positions}
    ui_by_ticker = {p.ticker: p for p in ui_positions}
    assert set(email_by_ticker) == {"WATCHED", "OFFLIST"} == set(ui_by_ticker)

    for ticker in email_by_ticker:
        email_pos, ui_pos = email_by_ticker[ticker], ui_by_ticker[ticker]
        assert email_pos.current_value == ui_pos.current_value
        assert email_pos.unrealised_pnl == ui_pos.unrealised_pnl
        assert email_pos.price_currency == ui_pos.price_currency

    # OFFLIST is USD and must keep native-currency P&L (not silently GBP).
    offlist = email_by_ticker["OFFLIST"]
    assert offlist.price_currency == "USD"
    assert offlist.current_value == pytest.approx(5.0 * 60.0)
    assert offlist.unrealised_pnl == pytest.approx(5.0 * 60.0 - 5.0 * 50.0)

    # Aggregate GBP totals convert the USD leg instead of summing raw
    # cross-currency numbers.
    total_value_gbp, total_cost_gbp, total_pnl_gbp = PortfolioService.gbp_totals(
        email_positions, gbpusd
    )
    expected_value = 10.0 * 120.0 + (5.0 * 60.0) / 1.25
    expected_cost = 10.0 * 100.0 + (5.0 * 50.0) / 1.25
    assert total_value_gbp == pytest.approx(expected_value)
    assert total_cost_gbp == pytest.approx(expected_cost)
    assert total_pnl_gbp == pytest.approx(expected_value - expected_cost)


def test_gbp_totals_converts_usd_positions() -> None:
    """gbp_totals must convert each position's native currency before
    summing, matching the web UI's aggregation (PortfolioService.
    portfolio_partial_context)."""
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
    total_value, total_cost, total_pnl = PortfolioService.gbp_totals(
        positions, gbpusd=2.0
    )
    assert total_value == 285.0  # 150 + 270/2
    assert total_cost == 200.0  # 100 + 200/2
    assert total_pnl == 85.0
