"""Tests for the pre-live-writer daily snapshot backfill (#502).

Covers every row of the spec's I/O & Edge-Case Matrix with an in-memory
``trades.db`` and a stub price source / evidence-backfill collaborator.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.services.backtest.historical_price_evidence import FX_PAIR
from app.services.snapshot_backfill import SnapshotBackfillService
from app.services.snapshot_price_backfill import PriceEvidenceUnavailable
from app.services.snapshot_repair import NoHistoricalPriceSource


class _FixedPriceSource:
    """Evidence for a fixed ``{ticker: price}`` set; optional dated holes."""

    def __init__(
        self,
        prices: dict[str, float],
        holes: set[tuple[str, str]] | None = None,
    ) -> None:
        self._prices = prices
        self._holes = holes or set()
        self.calls: list[tuple[str, str]] = []

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        self.calls.append((ticker, as_of))
        if (ticker, as_of) in self._holes:
            return None
        return self._prices.get(ticker)


class _FakeBackfill:
    """Stand-in ``PriceEvidenceBackfillService`` recording every call."""

    def __init__(
        self,
        unavailable: set[str] | None = None,
        failing: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.spans: dict[str, tuple[str, str]] = {}
        self.fx_calls: list[tuple[str, str]] = []
        self._unavailable = unavailable or set()
        self._failing = failing or set()

    def ensure_coverage(self, ticker: str, start: date, end: date) -> bool:
        self.calls.append(ticker)
        self.spans[ticker] = (start.isoformat(), end.isoformat())
        if ticker in self._unavailable:
            raise PriceEvidenceUnavailable(f"no rows for {ticker}")
        if ticker in self._failing:
            raise RuntimeError(f"transient failure for {ticker}")
        return True

    def ensure_fx_coverage(self, start: date, end: date) -> bool:
        self.fx_calls.append((start.isoformat(), end.isoformat()))
        return True


def _agent(tmp_path: Path) -> TraderAgent:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    return agent


#: Pinned "today" for every fixed-date test, so the fill window is a stable
#: [first-trade, TODAY) rather than one that grows with the wall clock (#509).
TODAY = date(2024, 1, 8)


def _service(
    agent: TraderAgent,
    source: object | None = None,
    backfill: object | None = None,
    today: date = TODAY,
) -> SnapshotBackfillService:
    return SnapshotBackfillService(
        agent._trades,
        agent._snapshots,
        agent._portfolios,
        agent._account,
        source,  # type: ignore[arg-type]
        backfill=backfill,  # type: ignore[arg-type]
        today=lambda: today,
    )


def _rows(agent: TraderAgent, portfolio_id: int) -> list[Any]:
    conn = sqlite3.connect(agent.db_path)
    try:
        return conn.execute(
            "SELECT timestamp, total_value, total_cost, cash_balance "
            "FROM portfolio_snapshots WHERE portfolio_id = ? ORDER BY timestamp",
            (portfolio_id,),
        ).fetchall()
    finally:
        conn.close()


def test_first_backfill_writes_one_priced_row_per_day(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-05T09:00:00+00:00", 999.0, 900.0, 100.0)

    report = _service(agent, _FixedPriceSource({"AAPL": 7.5})).backfill(pf.id)

    # 2024-01-01 .. 2024-01-07 (TODAY exclusive); 01-05 already has a row.
    assert report.days_considered == 7
    assert report.days_already_present == 1
    assert report.rows_written == 6
    rows = _rows(agent, pf.id)
    backfilled = [r for r in rows if r[0].endswith("T00:00:00+00:00")]
    assert [r[0][:10] for r in backfilled] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-06",
        "2024-01-07",
    ]
    assert all(r[1] == pytest.approx(75.0) for r in backfilled)
    assert all(r[2] is None and r[3] is None for r in backfilled)
    # The pre-existing live row is untouched.
    assert (999.0, 900.0, 100.0) in [(r[1], r[2], r[3]) for r in rows]


def test_re_run_and_double_trigger_write_nothing(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-05T09:00:00+00:00", 999.0, 900.0, 100.0)
    service = _service(agent, _FixedPriceSource({"AAPL": 7.5}))

    first = service.backfill(pf.id)
    second = service.backfill(pf.id)

    assert first.rows_written == 6
    assert second.rows_written == 0
    assert len(_rows(agent, pf.id)) == 7


def test_no_trades_is_a_noop(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")

    report = _service(agent, _FixedPriceSource({})).backfill(pf.id)

    assert (report.rows_written, report.days_considered) == (0, 0)
    assert _rows(agent, pf.id) == []


def test_no_existing_snapshot_fills_settled_days_not_today(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    start = TODAY - timedelta(days=3)
    agent.record_buy("AAPL", 10, 5.0, start.isoformat(), portfolio_id=pf.id)

    report = _service(agent, _FixedPriceSource({"AAPL": 2.0})).backfill(pf.id)

    # start .. yesterday -- today is left for the live writer to own.
    assert report.rows_written == 3
    rows = _rows(agent, pf.id)
    assert rows[-1][0] == f"{(TODAY - timedelta(days=1)).isoformat()}T00:00:00+00:00"


def test_second_run_with_unchanged_range_skips_the_day_loop(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-05T09:00:00+00:00", 1.0, 1.0, 1.0)
    source = _FixedPriceSource({"AAPL": 7.5})
    service = _service(agent, source)

    service.backfill(pf.id)
    calls_after_first = len(source.calls)
    second = service.backfill(pf.id)

    assert second.days_considered == 0  # marker short-circuits before the loop
    assert len(source.calls) == calls_after_first  # no re-pricing


def test_guarded_insert_never_doubles_a_day(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    repo = agent._snapshots
    # A racing run already landed this day at a different intraday time.
    repo.append(pf.id, "2024-01-02T11:22:33+00:00", 50.0, None, None)

    wrote = repo.append_daily_value_if_absent(
        pf.id, "2024-01-02", "2024-01-02T00:00:00+00:00", 99.0
    )
    fresh = repo.append_daily_value_if_absent(
        pf.id, "2024-01-03", "2024-01-03T00:00:00+00:00", 12.0
    )

    assert wrote is False  # day already present -> no second row
    assert fresh is True
    days = sorted(r[0][:10] for r in _rows(agent, pf.id))
    assert days == ["2024-01-02", "2024-01-03"]


def test_sold_and_gone_ticker_is_not_prefetched_to_the_window_end(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("OLD", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_sell("OLD", 10, 6.0, "2024-01-02", portfolio_id=pf.id)
    agent.record_buy("NEW", 5, 5.0, "2024-01-03", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-06-01T09:00:00+00:00", 1.0, 1.0, 1.0)
    backfill = _FakeBackfill(unavailable={"OLD"})

    report = _service(
        agent, _FixedPriceSource({"OLD": 1.0, "NEW": 3.0}), backfill
    ).backfill(pf.id)

    # OLD is flat by the window end, so its span collapses to
    # [2024-01-01, 2024-01-03) (last trade + 1 day) -- never chased to June.
    # NEW is still held, so it keeps the full span to the window end.
    assert backfill.spans["OLD"] == ("2024-01-01", "2024-01-03")
    assert backfill.spans["NEW"][1] == TODAY.isoformat()
    assert "OLD" in report.newly_unavailable


def test_days_with_no_dated_close_get_no_row(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-05T09:00:00+00:00", 1.0, 1.0, 1.0)
    source = _FixedPriceSource(
        {"AAPL": 7.5}, holes={("AAPL", "2024-01-02"), ("AAPL", "2024-01-03")}
    )

    report = _service(agent, source).backfill(pf.id)

    assert report.rows_written == 4
    assert report.days_skipped_no_evidence == 2
    backfilled = [
        r[0][:10] for r in _rows(agent, pf.id) if r[0].endswith("T00:00:00+00:00")
    ]
    assert backfilled == ["2024-01-01", "2024-01-04", "2024-01-06", "2024-01-07"]


def test_one_unpriceable_holding_drops_the_whole_day(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("MSFT", 5, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("TSLA", 2, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)
    # No evidence for TSLA at all.
    source = _FixedPriceSource({"AAPL": 7.5, "MSFT": 3.0})

    report = _service(agent, source).backfill(pf.id)

    assert report.rows_written == 0
    assert report.days_skipped_no_evidence == 6


def test_days_before_first_trade_are_skipped_as_no_holdings(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("MSFT", 5, 5.0, "2024-01-04", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-06T09:00:00+00:00", 1.0, 1.0, 1.0)
    # Fully liquidate AAPL on 2024-01-02 so that day has no holdings.
    agent.record_sell("AAPL", 10, 6.0, "2024-01-02", portfolio_id=pf.id)
    source = _FixedPriceSource({"AAPL": 7.5, "MSFT": 3.0})

    report = _service(agent, source).backfill(pf.id)

    # 01-01 AAPL, 01-02/01-03 flat, 01-04..01-07 MSFT (01-06 already present)
    assert report.rows_written == 4
    assert report.days_skipped_no_holdings == 2


def test_transient_ticker_failure_is_reported_others_proceed(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("MSFT", 5, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)
    backfill = _FakeBackfill(failing={"MSFT"})
    source = _FixedPriceSource({"AAPL": 7.5, "MSFT": 3.0})

    report = _service(agent, source, backfill).backfill(pf.id)

    assert report.fetch_failures == ("MSFT",)
    assert report.newly_unavailable == ()
    assert set(backfill.calls) == {"AAPL", "MSFT"}
    assert backfill.fx_calls == [("2024-01-01", TODAY.isoformat())]
    # A prefetch failure does not stop the read-path valuation.
    assert report.rows_written == 6


def test_permanently_unavailable_ticker_is_reported(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("HSFWA", 3, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)
    backfill = _FakeBackfill(unavailable={"HSFWA"})

    report = _service(
        agent, _FixedPriceSource({"AAPL": 7.5, "HSFWA": 1.0}), backfill
    ).backfill(pf.id)

    assert report.newly_unavailable == ("HSFWA",)
    assert report.fetch_failures == ()


def test_per_portfolio_failure_is_isolated(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    broken = agent.create_portfolio("BROKEN")
    healthy = agent.create_portfolio("HEALTHY")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=broken.id)
    agent.record_buy("MSFT", 5, 5.0, "2024-01-01", portfolio_id=healthy.id)
    for pid in (broken.id, healthy.id):
        agent._snapshots.append(pid, "2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)

    real_open_rows = agent._trades.open_rows

    def flaky_open_rows(portfolio_id: int | None = None) -> list[tuple[object, ...]]:
        if portfolio_id == broken.id:
            raise sqlite3.OperationalError("database is locked")
        return real_open_rows(portfolio_id)

    agent._trades.open_rows = flaky_open_rows  # type: ignore[method-assign]

    report = _service(agent, _FixedPriceSource({"MSFT": 3.0})).backfill()

    assert report.rows_written == 6  # only the healthy portfolio
    assert _rows(agent, broken.id) == [("2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)]


def test_all_portfolios_loop_when_no_id_given(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A")
    b = agent.create_portfolio("B")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=a.id)
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=b.id)
    for pid in (a.id, b.id):
        agent._snapshots.append(pid, "2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)

    report = _service(agent, _FixedPriceSource({"AAPL": 2.0})).backfill()

    assert report.portfolios_scanned == 2
    assert report.rows_written == 12


def test_no_price_source_writes_nothing_but_does_not_raise(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-01-03T09:00:00+00:00", 1.0, 1.0, 1.0)

    report = _service(agent, NoHistoricalPriceSource()).backfill(pf.id)

    assert report.rows_written == 0
    assert report.days_skipped_no_evidence == 6


def test_fx_pair_constant_is_the_reported_name() -> None:
    assert FX_PAIR  # sanity: import is the pair label used in reports


def test_interior_gap_between_existing_snapshots_is_filled(tmp_path: Path) -> None:
    """#509: a stray early row must not act as a floor blocking later gaps.

    Before this, the fill window ended at the earliest existing snapshot, so a
    single row near the start of the history made every later missing day
    permanently unfillable -- the repair pass could not fill them either,
    because it only rewrites rows that already exist and never inserts one.
    """
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    # One stray row right at the start, and one live row near the end: the
    # interior (01-03 .. 01-06) is the gap nothing used to be able to fill.
    agent._snapshots.append(pf.id, "2024-01-02T09:00:00+00:00", 111.0, 100.0, 10.0)
    agent._snapshots.append(pf.id, "2024-01-07T16:30:00+00:00", 222.0, 200.0, 20.0)

    report = _service(agent, _FixedPriceSource({"AAPL": 7.5})).backfill(pf.id)

    assert report.days_already_present == 2  # the two pre-existing days
    assert report.rows_written == 5  # 01-01, and the 01-03..01-06 interior
    filled = [
        r[0][:10] for r in _rows(agent, pf.id) if r[0].endswith("T00:00:00+00:00")
    ]
    assert filled == [
        "2024-01-01",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-06",
    ]
    # Both pre-existing rows survive untouched, values and all.
    preserved = [(r[1], r[2], r[3]) for r in _rows(agent, pf.id) if r[2] is not None]
    assert preserved == [(111.0, 100.0, 10.0), (222.0, 200.0, 20.0)]


# --- #508: progress is published as the run proceeds -----------------------


def test_progress_is_published_through_the_phases(tmp_path: Path) -> None:
    from app.services.backfill_status import BackfillStatusTracker

    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("MSFT", 5, 5.0, "2024-01-01", portfolio_id=pf.id)
    progress = BackfillStatusTracker()
    service = SnapshotBackfillService(
        agent._trades,
        agent._snapshots,
        agent._portfolios,
        agent._account,
        _FixedPriceSource({"AAPL": 7.5, "MSFT": 3.0}),
        backfill=_FakeBackfill(),  # type: ignore[arg-type]
        today=lambda: TODAY,
        progress=progress,
    )

    service.backfill(pf.id)

    final = progress.get(pf.id)
    assert final is not None
    assert final.phase == "done"
    assert final.running is False
    assert final.days_total == 7  # 2024-01-01 .. 2024-01-07
    assert final.days_done == 7
    assert final.rows_written == 7
    assert (final.tickers_done, final.tickers_total) == (2, 2)
    assert final.first_day == "2024-01-01"
    assert final.last_day == "2024-01-07"


def test_progress_records_a_failure_for_a_broken_portfolio(tmp_path: Path) -> None:
    from app.services.backfill_status import BackfillStatusTracker

    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    progress = BackfillStatusTracker()

    def boom(portfolio_id: int | None = None) -> list[tuple[object, ...]]:
        raise sqlite3.OperationalError("database is locked")

    agent._trades.open_rows = boom  # type: ignore[method-assign]
    service = SnapshotBackfillService(
        agent._trades,
        agent._snapshots,
        agent._portfolios,
        agent._account,
        _FixedPriceSource({"AAPL": 7.5}),
        today=lambda: TODAY,
        progress=progress,
    )

    report = service.backfill(pf.id)

    assert report.portfolios_failed == 1
    # begin() never ran, so there is no half-built entry to render.
    assert progress.get(pf.id) is None


def test_a_no_op_run_publishes_no_progress(tmp_path: Path) -> None:
    from app.services.backfill_status import BackfillStatusTracker

    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    progress = BackfillStatusTracker()
    kwargs = dict(today=lambda: TODAY, progress=progress)
    first = SnapshotBackfillService(
        agent._trades,
        agent._snapshots,
        agent._portfolios,
        agent._account,
        _FixedPriceSource({"AAPL": 7.5}),
        **kwargs,  # type: ignore[arg-type]
    )
    first.backfill(pf.id)

    fresh = BackfillStatusTracker()
    second = SnapshotBackfillService(
        agent._trades,
        agent._snapshots,
        agent._portfolios,
        agent._account,
        _FixedPriceSource({"AAPL": 7.5}),
        today=lambda: TODAY,
        progress=fresh,
    )
    second.backfill(pf.id)

    # The marker short-circuits before begin(), so no bar is shown for a
    # trigger that had nothing to do.
    assert fresh.get(pf.id) is None
