"""Tests for ``RealisedPnlService`` (Epic 1, Story 1.1: FIFO lot matching).

Follows ``tests/test_trader_agent.py``'s real-SQLite-no-mocks convention: a
real ``TraderAgent`` backed by a ``tmp_path`` file, wrapped in a real
``TraderService``, seeded via ``record_buy``/``record_sell``.
"""

import inspect
from pathlib import Path

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.schemas import Trade
from app.services.realised_pnl_service import RealisedPnlService
from app.services.trader_service import TraderService

PORTFOLIO_ID = 1


def _make_service_with_agent(tmp_path: Path) -> tuple[RealisedPnlService, TraderAgent]:
    """Build a RealisedPnlService (and its underlying TraderAgent) backed by
    a fresh tmp_path SQLite DB, following tests/test_trader_agent.py's
    real-DB-no-mocks pattern."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trader_service = TraderService(agent)
    return RealisedPnlService(trader_service), agent


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
