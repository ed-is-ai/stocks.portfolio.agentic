"""Tests for ``RealisedPnlService`` (Epic 1, Story 1.1: FIFO lot matching;
Story 1.2: trade-date FX conversion with historical rate caching).

Follows ``tests/test_trader_agent.py``'s real-SQLite-no-mocks convention: a
real ``TraderAgent`` backed by a ``tmp_path`` file, wrapped in a real
``TraderService``, seeded via ``record_buy``/``record_sell``. Unless a test
overrides them, ``PortfolioService.ticker_currency``/``historical_gbpusd_rates``
are stubbed to a GBP-only, rate-1 default so Story 1.1's pre-existing tests
(written before FX conversion existed) keep asserting native-currency
figures unchanged.
"""

import inspect
from pathlib import Path
from typing import cast

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.schemas import Trade
from app.services.portfolio_service import PortfolioService
from app.services.realised_pnl_service import RealisedPnlService
from app.services.trader_service import TraderService

PORTFOLIO_ID = 1


class _StubPortfolioService:
    """Default FX/currency stub: every ticker is GBP, so conversion is a
    no-op (rate = 1) and every pre-existing Story 1.1 test's native-currency
    assertions still hold unchanged."""

    def ticker_currency(self, ticker: str) -> str:
        return "GBP"

    def historical_gbpusd_rates(self, dates: list[str]) -> dict[str, float]:
        return {}


def _make_service_with_agent(
    tmp_path: Path, portfolio_service: object | None = None
) -> tuple[RealisedPnlService, TraderAgent]:
    """Build a RealisedPnlService (and its underlying TraderAgent) backed by
    a fresh tmp_path SQLite DB, following tests/test_trader_agent.py's
    real-DB-no-mocks pattern. Defaults to an all-GBP ``PortfolioService``
    stub unless a test needs to exercise real FX conversion."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trader_service = TraderService(agent)
    portfolio = portfolio_service or _StubPortfolioService()
    return (
        RealisedPnlService(trader_service, cast(PortfolioService, portfolio)),
        agent,
    )


def _stub_trade_history(
    service: RealisedPnlService,
    monkeypatch: pytest.MonkeyPatch,
    trades: list[Trade],
) -> None:
    """Replace ``service._trader.get_trade_history`` to return ``trades``.

    ``trades.shares`` carries a DB-level ``CHECK(shares > 0)`` constraint, so
    a malformed (zero/negative-share) row can never actually be persisted --
    it also means a genuine FIFO-vs-avg-cost divergence can't be manufactured
    by writing bad rows directly. Both scenarios are instead exercised at
    ``RealisedPnlService``'s real input boundary (``TraderService``, not the
    DB layer) by stubbing the trade list it receives, while leaving
    ``get_portfolio()`` (and the DB underneath it) untouched and real.
    """
    monkeypatch.setattr(service._trader, "get_trade_history", lambda **kwargs: trades)


def test_simple_one_to_one_round_trip(tmp_path: Path) -> None:
    """AC 1: one BUY fully consumed by one SELL produces one RoundTrip."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    rt = summary.round_trips["TEST1"][0]
    assert rt.entry_date == "2026-01-01"
    assert rt.entry_price == 100.0
    assert rt.exit_date == "2026-02-01"
    assert rt.exit_price == 150.0
    assert rt.shares == 10
    assert rt.holding_period_days == 31
    assert rt.realised_pnl_gbp == 500.0
    assert summary.total_realised_pnl_gbp == 500.0


def test_partial_sell_leaves_remainder_open(tmp_path: Path) -> None:
    """Partial sell: fewer shares sold than the open lot; remainder stays
    open and is excluded from every Round-trip/unmatched result."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 4, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    rt = summary.round_trips["TEST1"][0]
    assert rt.shares == 4
    assert summary.unmatched_count == 0
    # The remaining 6 shares stay part of the open position (avg-cost still
    # reports them), so no mismatch should be reported.
    assert summary.mismatched_tickers == []


def test_sell_spanning_two_lots(tmp_path: Path) -> None:
    """AC 2: a SELL spanning two BUY lots produces one RoundTrip per lot,
    oldest lot first, each with its own share quantity."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST1", 5, 110.0, "2026-01-05", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 8, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 2
    rts = summary.round_trips["TEST1"]
    # Both round trips share the same exit_date; internal order among ties
    # is not specified beyond FIFO consumption order, so assert as a set of
    # (entry_price, shares) pairs plus the total.
    pairs = {(rt.entry_price, rt.shares) for rt in rts}
    assert pairs == {(100.0, 5.0), (110.0, 3.0)}
    assert sum(rt.shares for rt in rts) == 8


def test_sell_spanning_three_or_more_lots(tmp_path: Path) -> None:
    """AC 2: a SELL spanning three lots produces three Round-trips, oldest
    lot consumed first, summing to the SELL total."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 2, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST1", 2, 110.0, "2026-01-02", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST1", 2, 120.0, "2026-01-03", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 5, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 3
    rts = summary.round_trips["TEST1"]
    pairs = [(rt.entry_price, rt.shares) for rt in rts]
    assert sorted(pairs) == [(100.0, 2.0), (110.0, 2.0), (120.0, 1.0)]
    assert sum(rt.shares for rt in rts) == 5


def test_unsold_buy_never_becomes_round_trip(tmp_path: Path) -> None:
    """AC 3: shares from a BUY never sold stay part of the open position and
    never appear in any Round-trip, count, or total."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 0
    assert summary.round_trips == {}
    assert summary.total_realised_pnl_gbp == 0.0


def test_same_date_tie_break_follows_ascending_id(tmp_path: Path) -> None:
    """AC 4: two same-date trades for the same ticker/portfolio are ordered
    by ascending id (insertion order), not row-return order."""
    service, agent = _make_service_with_agent(tmp_path)
    # A BUY and a SELL sharing the same date; the BUY must be inserted (and
    # therefore assigned a lower id) before the SELL for FIFO to match them,
    # even though get_trade_history() returns newest-first.
    agent.record_buy("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 5, 120.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    rt = summary.round_trips["TEST1"][0]
    assert rt.entry_price == 100.0
    assert rt.exit_price == 120.0


def test_zero_and_negative_share_rows_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 4: rows with shares == 0 or negative shares are skipped entirely,
    never entering any lot queue or affecting any Round-trip.

    ``trades.shares`` has a DB-level ``CHECK(shares > 0)`` constraint, so a
    malformed row can never actually reach the table via any insert path;
    this test instead simulates one arriving at RealisedPnlService's real
    input boundary (a ``TraderService.get_trade_history()`` return value),
    which is exactly where the service's own defensive filter (AD-4) lives.
    """
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    real_trades = service._trader.get_trade_history(portfolio_id=PORTFOLIO_ID)
    malformed = [
        Trade(
            id=9001,
            ticker="TEST1",
            action="BUY",
            shares=0,
            price=100.0,
            date="2026-01-02",
            portfolio_id=PORTFOLIO_ID,
        ),
        Trade(
            id=9002,
            ticker="TEST1",
            action="SELL",
            shares=-3,
            price=90.0,
            date="2026-01-03",
            portfolio_id=PORTFOLIO_ID,
        ),
    ]
    _stub_trade_history(service, monkeypatch, real_trades + malformed)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    rt = summary.round_trips["TEST1"][0]
    assert rt.shares == 10


def test_service_never_references_replay_trades() -> None:
    """AC 4: a code-level guard that RealisedPnlService's source never
    references TraderAgent._replay_trades."""
    assert "_replay_trades" not in inspect.getsource(RealisedPnlService)


def test_mismatched_tickers_empty_when_fifo_and_avg_cost_agree(
    tmp_path: Path,
) -> None:
    """AC 5: when the FIFO open-lot total and avg-cost total agree,
    mismatched_tickers is empty."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 4, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.mismatched_tickers == []


def test_oversell_unmatched_shortfall_does_not_itself_cause_mismatch(
    tmp_path: Path,
) -> None:
    """An oversell (SELL exceeding the open lot) is recorded as an unmatched
    occurrence, and -- since both the FIFO and avg-cost replays clamp an
    oversell's shortfall at zero the same way -- it does not by itself
    produce a false-positive mismatch."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 8, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.unmatched_count == 1
    assert summary.mismatched_tickers == []


def test_mismatched_tickers_flags_genuine_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 5: a genuine divergence between the FIFO open-lot total and the
    avg-cost total is flagged in mismatched_tickers.

    ``get_portfolio()``'s avg-cost replay always recomputes independently
    from the real, CHECK-constrained DB, so it can never itself be fed bad
    data directly. A genuine divergence is instead simulated the same way
    real drift between the two independent replays would show up: FIFO is
    given a trade history containing a SELL the DB (and therefore
    avg-cost) never saw, so FIFO's open-lot total drops while avg-cost's
    stays put.
    """
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    real_trades = service._trader.get_trade_history(portfolio_id=PORTFOLIO_ID)
    extra_sell = Trade(
        id=9101,
        ticker="TEST1",
        action="SELL",
        shares=2,
        price=110.0,
        date="2026-01-05",
        portfolio_id=PORTFOLIO_ID,
    )
    _stub_trade_history(service, monkeypatch, real_trades + [extra_sell])

    summary = service.compute_summary(PORTFOLIO_ID)

    # FIFO sees the extra SELL (open lot reduced to 3 shares); avg-cost,
    # recomputed from the real DB (which never saw that SELL), still
    # reports 5 shares held -- a genuine, detectable divergence.
    assert "TEST1" in summary.mismatched_tickers


def test_mismatch_epsilon_avoids_false_positive(tmp_path: Path) -> None:
    """AC 5: a sub-1e-6 floating-point difference must not trigger a false
    positive in mismatched_tickers."""
    service, agent = _make_service_with_agent(tmp_path)
    # Three fractional buys whose sum has float-accumulation noise at the
    # ~1e-16 level, well under the 1e-6 epsilon.
    agent.record_buy("TEST1", 0.1, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST1", 0.2, 100.0, "2026-01-02", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.mismatched_tickers == []


def test_compute_summary_recomputes_fresh_each_call(tmp_path: Path) -> None:
    """AC 6: calling compute_summary twice with no new trades yields equal
    results; a new trade between calls is reflected immediately (no cache)."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    first = service.compute_summary(PORTFOLIO_ID)
    second = service.compute_summary(PORTFOLIO_ID)
    assert first == second

    # No instance attribute should be memoizing a prior summary.
    from app.schemas import RealisedPnlSummary

    assert not any(isinstance(v, RealisedPnlSummary) for v in vars(service).values())

    # A new trade between calls must show up immediately.
    agent.record_buy("TEST2", 3, 40.0, "2026-03-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST2", 3, 50.0, "2026-03-05", portfolio_id=PORTFOLIO_ID)
    third = service.compute_summary(PORTFOLIO_ID)
    assert third.round_trip_count == first.round_trip_count + 1


def test_grouping_orders_tickers_by_most_recent_exit_date_descending(
    tmp_path: Path,
) -> None:
    """AD-9: ticker groups are ordered by each group's most recent exit
    date descending; within a group, Round-trips sort exit-date descending."""
    service, agent = _make_service_with_agent(tmp_path)
    # TEST1 closes earliest, TEST2 closes latest.
    agent.record_buy("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 5, 110.0, "2026-01-10", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST2", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST2", 5, 110.0, "2026-02-10", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert list(summary.round_trips.keys()) == ["TEST2", "TEST1"]


def test_sell_against_ticker_never_bought(tmp_path: Path) -> None:
    """AC 1/3 boundary: a SELL for a ticker with zero prior BUYs (queue
    empty from the start, not just exhausted mid-replay) produces no
    RoundTrip and is counted as unmatched, without raising."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_sell("NEVERBOUGHT", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 0
    assert summary.round_trips == {}
    assert summary.unmatched_count == 1


def test_invalid_ticker_rows_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AD-4: rows with an invalid-ticker sentinel ('', 'n/a', 'N/A') are
    excluded from FIFO matching entirely, independent of the shares filter.
    """
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    real_trades = service._trader.get_trade_history(portfolio_id=PORTFOLIO_ID)
    invalid_ticker_rows = [
        Trade(
            id=9101 + i,
            ticker=bad_ticker,
            action="BUY",
            shares=5,
            price=10.0,
            date="2026-01-02",
            portfolio_id=PORTFOLIO_ID,
        )
        for i, bad_ticker in enumerate(("", "n/a", "N/A"))
    ]
    _stub_trade_history(service, monkeypatch, real_trades + invalid_ticker_rows)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    assert set(summary.round_trips.keys()) == {"TEST1"}


def test_malformed_date_row_is_skipped_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trade row with an unparseable ``date`` is skipped defensively
    (logged, not raised) rather than crashing the whole compute_summary
    call -- this codebase has shipped and fixed real trade-date corruption
    before (#166, #167), so this must degrade gracefully, not 500."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    real_trades = service._trader.get_trade_history(portfolio_id=PORTFOLIO_ID)
    malformed_date_row = Trade(
        id=9201,
        ticker="TEST2",
        action="BUY",
        shares=5,
        price=10.0,
        date="not-a-date",
        portfolio_id=PORTFOLIO_ID,
    )
    _stub_trade_history(service, monkeypatch, real_trades + [malformed_date_row])

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    assert "TEST2" not in summary.round_trips


# --- Story 1.2: trade-date FX conversion with historical rate caching -----


class _FakePortfolioService:
    """Configurable FX/currency stub standing in for the real
    yfinance-backed ``PortfolioService`` -- a fixed ``ticker -> currency``
    map and a fixed ``date -> rate`` dict. Records every
    ``historical_gbpusd_rates`` call's argument list so tests can assert on
    batching behaviour (AC2)."""

    def __init__(self, currencies: dict[str, str], rates: dict[str, float]) -> None:
        self._currencies = currencies
        self._rates = rates
        self.rate_lookup_calls: list[list[str]] = []

    def ticker_currency(self, ticker: str) -> str:
        return self._currencies.get(ticker, "GBP")

    def historical_gbpusd_rates(self, dates: list[str]) -> dict[str, float]:
        self.rate_lookup_calls.append(list(dates))
        return {d: self._rates[d] for d in dates if d in self._rates}


def test_gbp_ticker_round_trip_uses_rate_of_one_no_conversion(
    tmp_path: Path,
) -> None:
    """GBP-ticker Round-trip: used as-is, rate=1, no date ever enters the
    FX-rate lookup (no yfinance call needed for that ticker)."""
    portfolio = _FakePortfolioService(currencies={"GBPCO": "GBP"}, rates={})
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("GBPCO", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("GBPCO", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    rt = summary.round_trips["GBPCO"][0]
    assert rt.fx_unavailable is False
    assert rt.realised_pnl_gbp == 500.0
    assert summary.total_realised_pnl_gbp == 500.0
    # GBP legs never contribute a date to the FX-rate lookup.
    assert portfolio.rate_lookup_calls == [[]]


def test_usd_round_trip_converts_buy_and_sell_legs_independently_by_trade_date(
    tmp_path: Path,
) -> None:
    """AC1: the BUY leg is converted at the BUY date's rate and the SELL leg
    at the SELL date's rate, each resolved independently -- not a single
    shared/averaged rate."""
    portfolio = _FakePortfolioService(
        currencies={"USDX": "USD"},
        rates={"2026-01-01": 1.25, "2026-02-01": 1.50},
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("USDX", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    rt = summary.round_trips["USDX"][0]
    # cost = $100*10/1.25 = 800.00 GBP; proceeds = $150*10/1.50 = 1000.00 GBP
    assert rt.fx_unavailable is False
    assert rt.realised_pnl_gbp == 200.0
    assert rt.realised_pnl_pct == 25.0
    assert summary.total_realised_pnl_gbp == 200.0


def test_mixed_currency_account_totals(tmp_path: Path) -> None:
    """A GBP-ticker Round-trip and a separate USD-ticker Round-trip within
    the same Account are each converted independently and sum correctly."""
    portfolio = _FakePortfolioService(
        currencies={"GBPCO": "GBP", "USDCO": "USD"},
        rates={"2026-01-01": 1.25, "2026-02-01": 1.50},
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("GBPCO", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("GBPCO", 5, 120.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("USDCO", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDCO", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    gbp_rt = summary.round_trips["GBPCO"][0]
    usd_rt = summary.round_trips["USDCO"][0]
    assert gbp_rt.realised_pnl_gbp == 100.0
    assert usd_rt.realised_pnl_gbp == 200.0
    assert summary.total_realised_pnl_gbp == 300.0


def test_leg_fx_unavailable_when_rate_missing_and_excluded_from_total(
    tmp_path: Path,
) -> None:
    """A Round-trip whose leg date has no resolvable rate is flagged
    fx_unavailable with a 0.0 placeholder, and is excluded from
    total_realised_pnl_gbp -- only the normal Round-trip's figure counts."""
    portfolio = _FakePortfolioService(
        currencies={"GBPCO": "GBP", "USDX": "USD"},
        rates={},  # no USD rate resolvable at all
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("GBPCO", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("GBPCO", 5, 120.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("USDX", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    usd_rt = summary.round_trips["USDX"][0]
    assert usd_rt.fx_unavailable is True
    assert usd_rt.realised_pnl_gbp == 0.0
    assert usd_rt.realised_pnl_pct == 0.0
    # Only the normal GBP Round-trip's 100.0 counts toward the total.
    assert summary.total_realised_pnl_gbp == 100.0


def test_third_currency_ticker_is_fx_unavailable_without_wrong_pair_lookup(
    tmp_path: Path,
) -> None:
    """A ticker whose currency is neither GBP nor USD must never fall
    through to a rates.get(...) lookup -- doing so could silently apply an
    unrelated USD leg's rate to a same-date third-currency trade. It must
    be immediately fx_unavailable instead, even when a USD round-trip on
    the exact same dates has a perfectly valid rate available."""
    portfolio = _FakePortfolioService(
        currencies={"EURCO": "EUR", "USDX": "USD"},
        rates={"2026-01-01": 1.25, "2026-02-01": 1.50},
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    # Same dates as the USD round-trip below, so a bug that looked EURCO's
    # dates up in `rates` would find a (wrong-currency-pair) value.
    agent.record_buy("EURCO", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("EURCO", 5, 120.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("USDX", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    eur_rt = summary.round_trips["EURCO"][0]
    assert eur_rt.fx_unavailable is True
    assert eur_rt.realised_pnl_gbp == 0.0
    # The USD round-trip is unaffected and still resolves normally -- proof
    # that EURCO's dates weren't excluded from the batch (only its currency
    # was rejected), and USDX's genuinely valid rate wasn't disturbed.
    usd_rt = summary.round_trips["USDX"][0]
    assert usd_rt.fx_unavailable is False
    assert usd_rt.realised_pnl_gbp != 0.0
    # Only the resolved USD round-trip's figure counts toward the total --
    # confirms EURCO's 0.0 placeholder was correctly excluded, not summed.
    assert summary.total_realised_pnl_gbp == usd_rt.realised_pnl_gbp


def test_round_trip_treats_non_positive_rate_as_unavailable(
    tmp_path: Path,
) -> None:
    """AC7 defensive check: even if PortfolioService somehow returned a
    zero/negative rate, RealisedPnlService never uses it -- treated as
    fx_unavailable, same as a missing rate."""
    portfolio = _FakePortfolioService(
        currencies={"USDX": "USD"},
        rates={"2026-01-01": 0.0, "2026-02-01": -1.5},
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("USDX", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    rt = summary.round_trips["USDX"][0]
    assert rt.fx_unavailable is True
    assert rt.realised_pnl_gbp == 0.0
    assert summary.total_realised_pnl_gbp == 0.0


def test_realised_pnl_pct_computed_on_gbp_amounts_not_native(
    tmp_path: Path,
) -> None:
    """AC5: realised_pnl_pct is computed from the GBP cost/proceeds, not
    the native-currency price -- constructed so the two would disagree if
    the wrong figure were used (native % is +10%, GBP % is negative)."""
    portfolio = _FakePortfolioService(
        currencies={"USDX": "USD"},
        rates={"2026-01-01": 1.0, "2026-02-01": 2.0},
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("USDX", 100, 10.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 100, 11.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    rt = summary.round_trips["USDX"][0]
    # Native: cost $1000, proceeds $1100 -> +10%.
    # GBP: cost 1000/1.0=1000.00, proceeds 1100/2.0=550.00 -> pnl=-450, -45%.
    assert rt.realised_pnl_gbp == -450.0
    assert rt.realised_pnl_pct == -45.0


def test_multi_lot_rounding_2dp_before_summing(tmp_path: Path) -> None:
    """AC8: each leg's GBP amount rounds to 2dp before combining into a
    Round-trip's pnl, and each Round-trip's already-2dp pnl is summed and
    rounded again into the total -- not a single final rounding over raw
    floats, which would give a different last-cent result here."""
    portfolio = _FakePortfolioService(
        currencies={"USDX": "USD"},
        rates={"2026-01-01": 3.0, "2026-02-01": 3.0},
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    # Two 1-share lots at the same rate/price, sold together -> two
    # Round-trips whose raw (unrounded) legs are $1/3 = 0.3333... GBP.
    agent.record_buy("USDX", 1, 1.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("USDX", 1, 1.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 2, 2.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    rts = summary.round_trips["USDX"]
    assert len(rts) == 2
    # Each leg: cost = 1/3 -> round2 0.33; proceeds = 2/3 -> round2 0.67;
    # pnl = 0.67 - 0.33 = 0.34 per Round-trip.
    for rt in rts:
        assert rt.realised_pnl_gbp == 0.34
    # Summing the two already-2dp 0.34 figures gives 0.68 -- summing the
    # *raw* unrounded legs first (2 * (0.6667 - 0.3333) = 0.6667) and
    # rounding once at the end would instead give 0.67.
    assert summary.total_realised_pnl_gbp == 0.68


def test_batches_historical_gbpusd_rates_call_once_per_compute_summary(
    tmp_path: Path,
) -> None:
    """AC2: historical_gbpusd_rates is called exactly once per
    compute_summary call -- never once per leg or per Round-trip -- even
    when multiple USD Round-trips need multiple distinct dates."""
    portfolio = _FakePortfolioService(
        currencies={"USDX": "USD", "USDY": "USD"},
        rates={
            "2026-01-01": 1.25,
            "2026-01-05": 1.30,
            "2026-02-01": 1.50,
            "2026-02-05": 1.55,
        },
    )
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("USDX", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDX", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("USDY", 5, 50.0, "2026-01-05", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("USDY", 5, 80.0, "2026-02-05", portfolio_id=PORTFOLIO_ID)

    service.compute_summary(PORTFOLIO_ID)

    assert len(portfolio.rate_lookup_calls) == 1
    assert set(portfolio.rate_lookup_calls[0]) == {
        "2026-01-01",
        "2026-01-05",
        "2026-02-01",
        "2026-02-05",
    }


def test_realised_pnl_service_calls_ticker_currency_not_yfinance_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: RealisedPnlService resolves currency exclusively via
    PortfolioService.ticker_currency -- never yfinance or its own
    currency-derivation logic."""
    portfolio = _FakePortfolioService(currencies={"TEST1": "GBP"}, rates={})
    calls: list[str] = []
    original = portfolio.ticker_currency

    def spy(ticker: str) -> str:
        calls.append(ticker)
        return original(ticker)

    monkeypatch.setattr(portfolio, "ticker_currency", spy)
    service, agent = _make_service_with_agent(tmp_path, portfolio)
    agent.record_buy("TEST1", 10, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_sell("TEST1", 10, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    service.compute_summary(PORTFOLIO_ID)

    assert calls == ["TEST1"]
    assert "yfinance" not in inspect.getsource(RealisedPnlService)


# --- Story 1.3: unmatched-sell detection -----------------------------------


def test_zero_lot_sell_produces_one_unmatched_sell_full_quantity(
    tmp_path: Path,
) -> None:
    """I/O matrix: a SELL for a ticker with no open lots at all produces one
    UnmatchedSell for the full quantity and zero Round-trips."""
    service, agent = _make_service_with_agent(tmp_path)
    sell = agent.record_sell(
        "NEVERBOUGHT", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID
    )

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 0
    assert summary.unmatched_count == 1
    assert len(summary.unmatched_sells) == 1
    unmatched = summary.unmatched_sells[0]
    assert unmatched.trade_id == sell.id
    assert unmatched.ticker == "NEVERBOUGHT"
    assert unmatched.portfolio_id == PORTFOLIO_ID
    assert unmatched.date == "2026-01-01"
    assert unmatched.shares == 5
    assert unmatched.price == 100.0
    # Zero prior lots: the "no prior BUY" reason applies verbatim.
    assert unmatched.reason == "No prior BUY found to match this sell"
    assert unmatched.acknowledged_at is None


def test_partial_shortfall_sell_produces_round_trip_and_unmatched_remainder(
    tmp_path: Path,
) -> None:
    """I/O matrix: a SELL quantity exceeding available lot shares produces
    one Round-trip for the covered portion and one UnmatchedSell for the
    shortfall, with UnmatchedSell.shares equal to the shortfall."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    sell = agent.record_sell("TEST1", 8, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 1
    rt = summary.round_trips["TEST1"][0]
    assert rt.shares == 5
    assert summary.unmatched_count == 1
    unmatched = summary.unmatched_sells[0]
    assert unmatched.trade_id == sell.id
    assert unmatched.ticker == "TEST1"
    assert unmatched.shares == 3
    assert unmatched.price == 150.0
    assert unmatched.date == "2026-02-01"
    # A BUY *was* found and partially consumed -- the "no prior BUY" wording
    # would be factually wrong here; the shortfall-specific reason applies.
    assert unmatched.reason == "Sell exceeds available BUY lots by 3 shares"


def test_multi_lot_sell_produces_round_trips_and_unmatched_shortfall(
    tmp_path: Path,
) -> None:
    """I/O matrix extension: a SELL that drains two open lots before still
    falling short produces one Round-trip per lot consumed *and* one
    UnmatchedSell for the remaining shortfall -- exercising the interaction
    between multi-lot consumption and unmatched-sell emission together."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_buy("TEST1", 2, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST1", 3, 110.0, "2026-01-02", portfolio_id=PORTFOLIO_ID)
    sell = agent.record_sell("TEST1", 9, 150.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 2
    rts = summary.round_trips["TEST1"]
    assert sorted((rt.entry_price, rt.shares) for rt in rts) == [
        (100.0, 2.0),
        (110.0, 3.0),
    ]
    assert summary.unmatched_count == 1
    unmatched = summary.unmatched_sells[0]
    assert unmatched.trade_id == sell.id
    assert unmatched.shares == 4
    assert unmatched.reason == "Sell exceeds available BUY lots by 4 shares"


def test_two_consecutive_unmatched_sells_same_ticker(tmp_path: Path) -> None:
    """Two SELLs in a row for the same ticker, both with no open lot
    available (the second SELL hits an already-drained, not merely
    never-populated, queue), each produce their own distinct
    UnmatchedSell -- not merged, not overwritten."""
    service, agent = _make_service_with_agent(tmp_path)
    first_sell = agent.record_sell(
        "TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID
    )
    second_sell = agent.record_sell(
        "TEST1", 3, 110.0, "2026-01-02", portfolio_id=PORTFOLIO_ID
    )
    assert first_sell.id is not None
    assert second_sell.id is not None

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.round_trip_count == 0
    assert summary.unmatched_count == 2
    by_trade_id = {u.trade_id: u for u in summary.unmatched_sells}
    assert by_trade_id[first_sell.id].shares == 5
    assert by_trade_id[first_sell.id].date == "2026-01-01"
    assert by_trade_id[second_sell.id].shares == 3
    assert by_trade_id[second_sell.id].date == "2026-01-02"


def test_unmatched_sell_never_retroactively_filled_by_later_buy(
    tmp_path: Path,
) -> None:
    """AC #5 regression: a SELL with no open lot, followed chronologically
    by a later-dated BUY for the same ticker, must not be retroactively
    filled. The SELL stays fully unmatched and the later BUY's shares stay
    open (available to a future SELL, never spent on the earlier one).

    This guards against a two-pass implementation ("match sells first, then
    reconcile leftovers against all buys") instead of the required single
    forward chronological replay.
    """
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_sell("TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    agent.record_buy("TEST1", 5, 90.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)

    summary = service.compute_summary(PORTFOLIO_ID)

    # (a) the earlier SELL still appears as an UnmatchedSell, unchanged.
    assert summary.round_trip_count == 0
    assert summary.unmatched_count == 1
    unmatched = summary.unmatched_sells[0]
    assert unmatched.date == "2026-01-01"
    assert unmatched.shares == 5

    # (b)/(c) the later BUY's shares are not consumed by the earlier SELL --
    # they remain part of the ticker's open-lot position, so a subsequent
    # SELL can still match against them.
    agent.record_sell("TEST1", 5, 120.0, "2026-03-01", portfolio_id=PORTFOLIO_ID)
    summary_after = service.compute_summary(PORTFOLIO_ID)
    assert summary_after.round_trip_count == 1
    rt = summary_after.round_trips["TEST1"][0]
    assert rt.entry_date == "2026-02-01"
    assert rt.entry_price == 90.0
    assert rt.shares == 5
    # The original SELL is still unmatched; only it, not the new SELL.
    assert summary_after.unmatched_count == 1
    assert summary_after.unmatched_sells[0].date == "2026-01-01"


def test_unmatched_count_always_equals_len_unmatched_sells(tmp_path: Path) -> None:
    """unmatched_count and len(unmatched_sells) must always agree -- the
    separate counter is retired in favour of deriving it from the list."""
    service, agent = _make_service_with_agent(tmp_path)
    sell1 = agent.record_sell(
        "TEST1", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID
    )
    agent.record_buy("TEST2", 3, 50.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)
    sell2 = agent.record_sell("TEST2", 5, 60.0, "2026-02-01", portfolio_id=PORTFOLIO_ID)
    assert sell1.id is not None
    assert sell2.id is not None

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.unmatched_count == len(summary.unmatched_sells)
    assert summary.unmatched_count == 2
    # Not just the aggregate count -- each record maps to the right trade,
    # not mixed up between tickers/dates/shares.
    by_trade_id = {u.trade_id: u for u in summary.unmatched_sells}
    assert by_trade_id[sell1.id].ticker == "TEST1"
    assert by_trade_id[sell1.id].shares == 5
    assert by_trade_id[sell2.id].ticker == "TEST2"
    assert by_trade_id[sell2.id].shares == 2  # 5 sold - 3 bought = 2 short


# --- Story 1.5: unmatched-sell acknowledge interaction ---------------------


def test_unmatched_sell_acknowledged_at_reflects_persisted_trade_state(
    tmp_path: Path,
) -> None:
    """Read-side wiring: ``UnmatchedSell.acknowledged_at`` reflects the
    underlying trade's ``realised_pnl_ack_at`` (previously hard-coded
    ``None`` before Story 1.5's read path was wired)."""
    service, agent = _make_service_with_agent(tmp_path)
    sell = agent.record_sell(
        "NEVERBOUGHT", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID
    )
    assert sell.id is not None
    agent.set_unmatched_sell_ack(sell.id, True)

    summary = service.compute_summary(PORTFOLIO_ID)

    assert summary.unmatched_sells[0].acknowledged_at is not None


def test_toggle_unmatched_sell_ack_sets_then_clears(tmp_path: Path) -> None:
    """AC #5/#6/#7: toggling an unacknowledged entry acknowledges it (with a
    fresh, recomputed summary reflecting the change); toggling again clears
    it back to unacknowledged."""
    service, agent = _make_service_with_agent(tmp_path)
    sell = agent.record_sell(
        "NEVERBOUGHT", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID
    )
    assert sell.id is not None

    acked_summary = service.toggle_unmatched_sell_ack(sell.id, PORTFOLIO_ID)
    assert acked_summary.unmatched_sells[0].acknowledged_at is not None

    unacked_summary = service.toggle_unmatched_sell_ack(sell.id, PORTFOLIO_ID)
    assert unacked_summary.unmatched_sells[0].acknowledged_at is None


def test_toggle_unmatched_sell_ack_unknown_trade_id_is_noop(tmp_path: Path) -> None:
    """A stale/unknown trade_id is a no-op: no write, no exception, and the
    current (unchanged) summary is returned."""
    service, agent = _make_service_with_agent(tmp_path)
    agent.record_sell("NEVERBOUGHT", 5, 100.0, "2026-01-01", portfolio_id=PORTFOLIO_ID)

    summary = service.toggle_unmatched_sell_ack(999999, PORTFOLIO_ID)

    assert summary.unmatched_sells[0].acknowledged_at is None
