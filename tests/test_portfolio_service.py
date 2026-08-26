from typing import Any, cast

import pandas as pd

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

    def get_cash_flows(self, portfolio_id=None, limit=200):
        return []

    def list_reconciliation_issues(self, portfolio_id=None, limit=200):
        return []

    def list_cash_balances(self, portfolio_id=None):
        return []

    def get_cached_ticker_currencies(self, tickers):
        return {}

    def save_ticker_currencies(self, currencies):
        pass


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


def test_cash_balances_by_currency_carries_a_gbp_valuation_projection_per_row(
    monkeypatch,
) -> None:
    """Story 1.6, AC3: ``portfolio_partial_context`` enumerates every
    currency in ``cash_balances``, each paired with a GBP Valuation
    Projection -- a non-GBP entry with an injected ``valuation_unavailable``
    projection is present and flagged, never silently dropped from the
    context."""
    from decimal import Decimal

    from app.services.gbp_valuation_service import GbpValuationProjection

    class _TraderWithBalances(_StubTrader):
        def list_cash_balances(self, portfolio_id=None):
            return [
                ("GBP", Decimal("100.00"), "2024-01-01"),
                ("USD", Decimal("50.00"), "2024-01-02"),
            ]

    class _FakeValuation:
        def value_in_gbp(self, money):
            if money.currency == "GBP":
                return GbpValuationProjection(
                    money=money, status="valued", gbp_amount=money.amount
                )
            return GbpValuationProjection(
                money=money, status="valuation_unavailable", reason="fetch_failed"
            )

    svc = PortfolioService(
        cast(TraderService, _TraderWithBalances()),
        cast(ExitEvaluator, _StubEvaluator()),
        gbp_valuation=cast(Any, _FakeValuation()),
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

    ctx = svc.portfolio_partial_context(
        [], gbpusd_rate=2.0, cash_balance=100.0, portfolio_id=1
    )

    entries = {e["currency"]: e for e in ctx["cash_balances_by_currency"]}
    assert set(entries) == {"GBP", "USD"}
    assert entries["GBP"]["amount"] == Decimal("100.00")
    assert entries["GBP"]["projection"].status == "valued"
    assert entries["USD"]["amount"] == Decimal("50.00")
    assert entries["USD"]["projection"].status == "valuation_unavailable"
    assert entries["USD"]["projection"].reason == "fetch_failed"


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


# --- Story 1.2: historical_gbpusd_rates / ticker_currency ------------------


class _FakeFxTrader:
    """``TraderService`` stub with a configurable in-memory FX-rate cache,
    standing in for a real ``FxRateCacheRepository``-backed service."""

    def __init__(self, cached: dict[str, float | None] | None = None) -> None:
        self._cache: dict[tuple[str, str], float | None] = {
            ("GBPUSD=X", date): rate for date, rate in (cached or {}).items()
        }
        self.saved: dict[str, float] = {}

    def get_cached_fx_rates(
        self, dates: list[str], pair: str = "GBPUSD=X"
    ) -> dict[str, float]:
        return cast(
            dict[str, float],
            {
                date: self._cache[(pair, date)]
                for date in dates
                if (pair, date) in self._cache
            },
        )

    def save_fx_rates(self, rates: dict[str, float], pair: str = "GBPUSD=X") -> None:
        if pair == "GBPUSD=X":
            self.saved.update(rates)
        self._cache.update({(pair, date): rate for date, rate in rates.items()})


def _make_fx_service(trader: _FakeFxTrader) -> PortfolioService:
    return PortfolioService(
        cast(TraderService, trader), cast(ExitEvaluator, _StubEvaluator())
    )


def test_historical_gbpusd_rates_batches_fetch_for_multiple_dates(
    monkeypatch,
) -> None:
    """AC2: yfinance is called at most once for a whole batch of distinct
    dates, not once per date."""
    calls: list[tuple] = []

    def fake_download(symbol, start=None, end=None, progress=None, auto_adjust=None):
        calls.append((symbol, start, end))
        idx = pd.date_range("2026-01-01", "2026-01-20", freq="D")
        return pd.DataFrame({"Close": [1.30] * len(idx)}, index=idx)

    monkeypatch.setattr("yfinance.download", fake_download)
    svc = _make_fx_service(_FakeFxTrader())

    result = svc.historical_gbpusd_rates(["2026-01-05", "2026-01-10", "2026-01-15"])

    assert len(calls) == 1
    assert result == {"2026-01-05": 1.3, "2026-01-10": 1.3, "2026-01-15": 1.3}


def test_historical_hkd_rates_use_gbphkd_pair_and_pair_aware_cache(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_download(symbol, **kwargs):
        calls.append(symbol)
        idx = pd.to_datetime(["2026-01-05"])
        value = 10.25 if symbol == "GBPHKD=X" else 1.25
        return pd.DataFrame({"Close": [value]}, index=idx)

    monkeypatch.setattr("yfinance.download", fake_download)
    trader = _FakeFxTrader()
    svc = _make_fx_service(trader)

    assert svc.historical_fx_rates("HKD", ["2026-01-05"]) == {"2026-01-05": 10.25}
    assert svc.historical_gbpusd_rates(["2026-01-05"]) == {"2026-01-05": 1.25}
    assert calls == ["GBPHKD=X", "GBPUSD=X"]
    assert trader._cache[("GBPHKD=X", "2026-01-05")] == 10.25
    assert trader._cache[("GBPUSD=X", "2026-01-05")] == 1.25


def test_historical_gbpusd_rates_uses_nearest_prior_day_within_7_days(
    monkeypatch,
) -> None:
    """A requested date with no exact-match row but a valid rate within the
    7-day lookback window resolves to that nearest prior day's rate."""

    def fake_download(symbol, start=None, end=None, progress=None, auto_adjust=None):
        idx = pd.to_datetime(["2026-01-01", "2026-01-05"])
        return pd.DataFrame({"Close": [1.20, 1.45]}, index=idx)

    monkeypatch.setattr("yfinance.download", fake_download)
    svc = _make_fx_service(_FakeFxTrader())

    result = svc.historical_gbpusd_rates(["2026-01-08"])

    # 2026-01-08 has no exact row; nearest earlier row within 7 days is
    # 2026-01-05 (3 days prior).
    assert result == {"2026-01-08": 1.45}


def test_historical_gbpusd_rates_omits_date_beyond_7_day_lookback(
    monkeypatch,
) -> None:
    """AC3: a date with no rate anywhere within the 7-day window (including
    one older than the whole downloaded series) is simply absent from the
    returned dict -- never present as 0.0 or a negative number."""

    def fake_download(symbol, start=None, end=None, progress=None, auto_adjust=None):
        idx = pd.to_datetime(["2026-01-01"])
        return pd.DataFrame({"Close": [1.20]}, index=idx)

    monkeypatch.setattr("yfinance.download", fake_download)
    svc = _make_fx_service(_FakeFxTrader())

    result = svc.historical_gbpusd_rates(["2026-01-20"])

    assert "2026-01-20" not in result
    assert result == {}


def test_historical_gbpusd_rates_cache_hit_avoids_refetch(monkeypatch) -> None:
    """AC4: a date already cached is served from the cache; yfinance is
    never called."""

    def fail_download(*args, **kwargs):
        raise AssertionError("yfinance.download must not be called on a cache hit")

    monkeypatch.setattr("yfinance.download", fail_download)
    svc = _make_fx_service(_FakeFxTrader(cached={"2026-01-01": 1.30}))

    result = svc.historical_gbpusd_rates(["2026-01-01"])

    assert result == {"2026-01-01": 1.3}


def test_historical_gbpusd_rates_treats_zero_negative_none_rate_as_unavailable(
    monkeypatch,
) -> None:
    """AC7: a cached rate that is 0, negative, or None is treated as
    unavailable and excluded -- and, per this story's chosen judgment call,
    a date already present in the cache (even with a bad value) is not
    retried against yfinance; the cache is authoritative for a date already
    attempted."""

    def fail_download(*args, **kwargs):
        raise AssertionError(
            "yfinance.download must not be called for an already-cached date"
        )

    monkeypatch.setattr("yfinance.download", fail_download)
    svc = _make_fx_service(
        _FakeFxTrader(
            cached={
                "2026-01-01": 0.0,
                "2026-01-02": -1.5,
                "2026-01-03": None,
            }
        )
    )

    result = svc.historical_gbpusd_rates(["2026-01-01", "2026-01-02", "2026-01-03"])

    assert result == {}


def test_historical_fx_rates_treats_infinity_as_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "yfinance.download",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cached invalid rates must not be refetched")
        ),
    )
    trader = _FakeFxTrader(cached={"2026-01-01": float("inf")})

    assert _make_fx_service(trader).historical_gbpusd_rates(["2026-01-01"]) == {}


def test_historical_gbpusd_rates_yfinance_exception_degrades_gracefully(
    monkeypatch,
) -> None:
    """A network/rate-limit failure inside yfinance.download must not crash
    the whole call -- every requested date simply comes back unresolved,
    matching `_fetch_gbpusd_rate()`'s existing same-style try/except."""

    def raising_download(*args, **kwargs):
        raise ConnectionError("simulated yfinance outage")

    monkeypatch.setattr("yfinance.download", raising_download)
    svc = _make_fx_service(_FakeFxTrader())

    result = svc.historical_gbpusd_rates(["2026-01-05"])

    assert result == {}


def test_historical_gbpusd_rates_caches_permanently_unresolved_date(
    monkeypatch,
) -> None:
    """A date that comes back with no rate anywhere in the 7-day window is
    persisted as a known-invalid sentinel, not left un-cached -- otherwise
    it would trigger a fresh yfinance download on every future call
    forever (NFR1's per-calendar-date caching intent)."""
    calls: list[tuple] = []

    def fake_download(symbol, start=None, end=None, progress=None, auto_adjust=None):
        calls.append((symbol, start, end))
        idx = pd.to_datetime(["2020-01-01"])  # far outside any 7-day window
        return pd.DataFrame({"Close": [1.20]}, index=idx)

    monkeypatch.setattr("yfinance.download", fake_download)
    trader = _FakeFxTrader()
    svc = _make_fx_service(trader)

    first = svc.historical_gbpusd_rates(["2026-01-20"])
    assert first == {}
    assert len(calls) == 1
    # The unresolved date is now cached as an invalid sentinel.
    assert trader.saved.get("2026-01-20") is not None
    assert trader.saved["2026-01-20"] <= 0

    # A second call for the same date must not re-fetch -- the cache is
    # now authoritative for this already-attempted date.
    second = svc.historical_gbpusd_rates(["2026-01-20"])
    assert second == {}
    assert len(calls) == 1


def test_historical_gbpusd_rates_end_to_end_no_refetch_on_second_call(
    tmp_path, monkeypatch
) -> None:
    """End-to-end (real TraderAgent + real TraderService + real DB, not the
    in-memory fake) confirmation of the story's headline AC: the same date
    requested on two separate calls triggers yfinance at most once."""
    from app.agents.trader.trader_agent import TraderAgent

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trader_service = TraderService(agent)
    svc = PortfolioService(trader_service, cast(ExitEvaluator, _StubEvaluator()))

    calls: list[tuple] = []

    def fake_download(symbol, start=None, end=None, progress=None, auto_adjust=None):
        calls.append((symbol, start, end))
        idx = pd.to_datetime(["2026-01-01"])
        return pd.DataFrame({"Close": [1.30]}, index=idx)

    monkeypatch.setattr("yfinance.download", fake_download)

    first = svc.historical_gbpusd_rates(["2026-01-01"])
    second = svc.historical_gbpusd_rates(["2026-01-01"])

    assert first == {"2026-01-01": 1.3}
    assert second == {"2026-01-01": 1.3}
    assert len(calls) == 1


class _FakeFastInfo:
    def __init__(self, currency: str | None) -> None:
        self.currency = currency


class _FakeTicker:
    def __init__(self, currency: str | None) -> None:
        self.fast_info = _FakeFastInfo(currency)


def test_ticker_currency_resolves_usd_and_gbp_and_defaults_on_failure(
    monkeypatch,
) -> None:
    """AC6: resolves a USD ticker, a GBP ticker, and defaults to GBP when
    the lookup raises."""
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {})

    def fake_ticker(symbol: str):
        if symbol == "AAPL":
            return _FakeTicker("USD")
        if symbol == "VOD.L":
            return _FakeTicker("GBP")
        raise RuntimeError("lookup failed")

    monkeypatch.setattr("yfinance.Ticker", fake_ticker)

    assert svc.ticker_currency("AAPL") == "USD"
    assert svc.ticker_currency("VOD.L") == "GBP"
    assert svc.ticker_currency("BROKEN") == "GBP"


def test_ticker_currency_is_reused_during_process_lifetime(monkeypatch) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {})
    calls: list[str] = []

    def fake_ticker(symbol: str):
        calls.append(symbol)
        return _FakeTicker("USD")

    monkeypatch.setattr("yfinance.Ticker", fake_ticker)

    assert svc.ticker_currency("AAPL") == "USD"
    assert svc.ticker_currency("AAPL") == "USD"
    assert calls == ["AAPL"]


def test_ticker_currencies_prefers_price_cache_and_only_resolves_misses(
    monkeypatch,
) -> None:
    trader = _StubTrader()
    trader.load_price_cache = lambda: (  # type: ignore[attr-defined]
        {"AAPL": 100.0},
        "2026-08-25T10:00:00+00:00",
        {"AAPL": (130.0, "USD")},
    )
    svc = PortfolioService(
        cast(TraderService, trader), cast(ExitEvaluator, _StubEvaluator())
    )
    misses: list[str] = []

    def resolve_miss(ticker: str) -> str:
        misses.append(ticker)
        return "GBP"

    monkeypatch.setattr(svc, "ticker_currency", resolve_miss)

    assert svc.ticker_currencies(["AAPL", "VOD.L", "AAPL"]) == {
        "AAPL": "USD",
        "VOD.L": "GBP",
    }
    assert misses == ["VOD.L"]


def test_ticker_currencies_uses_persistent_cache_before_live_lookup(
    monkeypatch,
) -> None:
    """A ticker absent from the live price cache (delisted/no-longer-scanned)
    but present in the durable ticker-currency cache must never trigger a
    live Yahoo lookup -- this is the whole point of the durable cache: it
    survives process restarts where the in-process ``@lru_cache`` cannot.
    """
    trader = _StubTrader()
    trader.load_price_cache = lambda: ({}, None, {})  # type: ignore[attr-defined]
    trader.get_cached_ticker_currencies = lambda tickers: {  # type: ignore[attr-defined]
        "DELISTED": "USD"
    }
    svc = PortfolioService(
        cast(TraderService, trader), cast(ExitEvaluator, _StubEvaluator())
    )

    def fail_if_called(ticker: str) -> str:
        raise AssertionError("must not resolve a durably-cached ticker live")

    monkeypatch.setattr(svc, "ticker_currency", fail_if_called)

    assert svc.ticker_currencies(["DELISTED"]) == {"DELISTED": "USD"}


def test_ticker_currencies_persists_newly_resolved_misses(monkeypatch) -> None:
    """A ticker resolved via a live lookup (durable cache miss) must be
    saved back to the durable cache so a future process restart doesn't
    re-pay the same lookup."""
    trader = _StubTrader()
    trader.load_price_cache = lambda: ({}, None, {})  # type: ignore[attr-defined]
    saved: dict[str, str] = {}
    trader.save_ticker_currencies = lambda currencies: saved.update(  # type: ignore[attr-defined]
        currencies
    )
    svc = PortfolioService(
        cast(TraderService, trader), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "ticker_currency", lambda ticker: "HKD")

    assert svc.ticker_currencies(["0700.HK"]) == {"0700.HK": "HKD"}
    assert saved == {"0700.HK": "HKD"}


def test_ticker_currency_resolves_canonical_ticker_fed_back(monkeypatch) -> None:
    """Story 2.1: a canonical ticker (as ``get_portfolio()`` now returns --
    here, an alias's chain terminal value) must resolve to the correct
    yfinance symbol via an explicit multi-hop walk, not rely on the
    coincidence that a one-hop ``aliases.get`` happens to pass it through
    unchanged."""
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {"FUND_A": "0PXXXXXXXXX.L"})
    calls: list[str] = []

    def fake_ticker(symbol: str):
        calls.append(symbol)
        return _FakeTicker("USD")

    monkeypatch.setattr("yfinance.Ticker", fake_ticker)

    assert svc.ticker_currency("0PXXXXXXXXX.L") == "USD"
    assert calls == ["0PXXXXXXXXX.L"]


def test_ticker_currency_resolves_legacy_identity_through_alias(monkeypatch) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {"HSFWA": "REAL.L"})
    calls: list[str] = []

    def fake_ticker(symbol: str):
        calls.append(symbol)
        return _FakeTicker("GBP")

    monkeypatch.setattr("yfinance.Ticker", fake_ticker)

    assert svc.ticker_currency("HSFWA") == "GBP"
    assert calls == ["REAL.L"]


def test_ticker_currency_resolves_9988_as_hkd_through_hkex_alias(
    monkeypatch,
) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {})
    calls: list[str] = []

    def fake_ticker(symbol: str):
        calls.append(symbol)
        return _FakeTicker("HKD")

    monkeypatch.setattr("yfinance.Ticker", fake_ticker)

    assert svc.ticker_currency("9988") == "HKD"
    assert calls == ["9988.HK"]


def test_ticker_currency_uses_hkex_suffix_when_yahoo_lookup_fails(
    monkeypatch,
) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {})
    monkeypatch.setattr(
        "yfinance.Ticker", lambda _symbol: (_ for _ in ()).throw(RuntimeError())
    )

    assert svc.ticker_currency("9988") == "HKD"


def test_ticker_currency_mandatory_9988_override_wins_over_local_alias(
    monkeypatch,
) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    monkeypatch.setattr(svc, "load_ticker_aliases", lambda: {"9988": "WRONG.L"})
    calls: list[str] = []

    def fake_ticker(symbol: str):
        calls.append(symbol)
        return _FakeTicker(" hkd ")

    monkeypatch.setattr("yfinance.Ticker", fake_ticker)

    assert svc.ticker_currency("9988") == "HKD"
    assert calls == ["9988.HK"]


def test_fetch_all_prices_resolves_chained_alias(monkeypatch) -> None:
    """Story 2.1: ``_resolve`` walks a multi-hop chain to its terminal
    value, matching what ``canonical_ticker`` would produce directly."""
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    calls: list[str] = []

    def fake_fetch(yf_sym, gbpusd):
        calls.append(yf_sym)
        if yf_sym == "ABC-YAHOO":
            return (12.0, 12.0, "GBP")
        return None

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    aliases = {"ABC.L": "ABC", "ABC": "ABC-YAHOO"}
    prices, display = svc.fetch_all_prices(["ABC.L"], aliases, 1.35)

    assert prices == {"ABC.L": 12.0}
    assert calls == ["ABC-YAHOO"]


def test_fetch_all_prices_resolves_legacy_identity_through_alias(monkeypatch) -> None:
    svc = PortfolioService(
        cast(TraderService, _StubTrader()), cast(ExitEvaluator, _StubEvaluator())
    )
    calls: list[str] = []

    def fake_fetch(yf_sym, gbpusd):
        calls.append(yf_sym)
        if yf_sym == "REAL.L":
            return (5.0, 5.0, "GBP")
        return None

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    prices, display = svc.fetch_all_prices(["HSFWA"], {"HSFWA": "REAL.L"}, 1.35)

    assert prices == {"HSFWA": 5.0}
    assert calls == ["REAL.L"]
