"""Backfill daily portfolio value snapshots for the full trade history (#502).

``portfolio_snapshots`` only holds rows from whenever the live snapshot writer
started running, so the Portfolio chart's long-range presets have nothing to
show before then even though trades and priced historical evidence reach back
years. :class:`SnapshotRepairService` only *repairs* existing rows; this
service *creates* the missing ones.

Per portfolio it replays trades for every calendar day from the first trade
date up to (but not including) the earliest existing snapshot -- or up to and
not including today when no snapshot exists yet, leaving today to the live
writer -- values the holdings via the same stored-evidence read path the
repair pass uses, and inserts one daily row per fully-priced day. It never
uses live prices or a guessed FX rate, never touches an existing row, and
never raises for a per-ticker or per-portfolio failure: those are isolated
and surfaced in the report.

Backfilled rows carry ``total_value`` (holdings only) with ``total_cost`` and
``cash_balance`` left ``NULL`` -- the chart null-guards the combined
"Portfolio Value" line when cash is absent, so the "Market Value" line
extends back honestly while the total/cash lines stay a gap (a faithful
per-day cost basis and cash balance need their own dated-FX reconstructions,
deferred -- see the issue).

Two cheap idempotency guards keep the repeated triggers (import, price
refresh, pipeline) from doing real work or corrupting rows:

* a per-portfolio ``account_state`` marker recording the ``[start, end)`` a
  successful run already covered -- an unchanged range is a fast no-op that
  never opens the day loop;
* a guarded single-statement insert that writes a day's row only when that
  calendar day has no snapshot yet, so two runs racing on the same day (the
  background tasks the routes schedule) cannot both insert it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.repositories.account_repo import AccountStateRepository
from app.repositories.db import Connect
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.portfolios_repo import PortfoliosRepository
from app.repositories.trades_repo import TradesRepository
from app.services.backtest.historical_price_evidence import FX_PAIR
from app.services.snapshot_price_backfill import (
    PriceEvidenceBackfillService,
    PriceEvidenceUnavailable,
)
from app.services.snapshot_price_evidence import build_price_source
from app.services.snapshot_repair import (
    HistoricalGbpPriceSource,
    NoHistoricalPriceSource,
    first_trade_dates,
    holdings_as_of,
    last_trade_dates,
)

logger = logging.getLogger(__name__)

#: ``account_state`` key prefix for the per-portfolio "already backfilled this
#: range" marker (see the module docstring).
_MARKER_PREFIX = "snapshot_backfill:"


class SnapshotBackfillReport(BaseModel):
    """Counts of what one backfill run did.

    ``rows_written`` + ``days_skipped_no_holdings`` + ``days_skipped_no_evidence``
    + ``days_already_present`` partition ``days_considered``.
    ``fetch_failures`` / ``newly_unavailable`` list the tickers (and
    ``GBPUSD=X``) whose evidence could not be acquired -- reported, never
    raised. ``portfolios_failed`` counts portfolios whose replay itself threw.
    """

    model_config = ConfigDict(frozen=True)

    portfolios_scanned: int
    portfolios_failed: int
    days_considered: int
    rows_written: int
    days_skipped_no_holdings: int
    days_skipped_no_evidence: int
    days_already_present: int
    fetch_failures: tuple[str, ...] = ()
    newly_unavailable: tuple[str, ...] = ()


class SnapshotBackfillService:
    """Creates the daily snapshots that predate the live writer, idempotently."""

    def __init__(
        self,
        trades: TradesRepository,
        snapshots: PortfolioSnapshotsRepository,
        portfolios: PortfoliosRepository,
        account_state: AccountStateRepository,
        price_source: HistoricalGbpPriceSource | None = None,
        backfill: PriceEvidenceBackfillService | None = None,
    ) -> None:
        self._trades = trades
        self._snapshots = snapshots
        self._portfolios = portfolios
        self._account_state = account_state
        self._price_source: HistoricalGbpPriceSource = (
            price_source or NoHistoricalPriceSource()
        )
        self._backfill = backfill

    def backfill(self, portfolio_id: int | None = None) -> SnapshotBackfillReport:
        """Backfill pre-live-writer daily snapshots, returning what was written.

        Scoped to ``portfolio_id`` when given, else every portfolio. Only the
        contiguous stretch strictly before the earliest existing snapshot is
        filled (never a gap between or after existing rows -- that is the
        repair pass's job). A re-run whose ``[start, end)`` range is unchanged
        from a prior successful run is a fast no-op; a run racing another on
        the same day cannot double-insert it.
        """
        if portfolio_id is not None:
            portfolio_ids = [portfolio_id]
        else:
            portfolio_ids = [pf.id for pf in self._portfolios.list_all()]

        totals = _RunTotals()
        failed = 0
        for pid in portfolio_ids:
            try:
                self._backfill_one(pid, totals)
            except Exception:
                failed += 1
                logger.exception("snapshot backfill failed for portfolio %s", pid)

        report = SnapshotBackfillReport(
            portfolios_scanned=len(portfolio_ids),
            portfolios_failed=failed,
            days_considered=totals.days_considered,
            rows_written=totals.rows_written,
            days_skipped_no_holdings=totals.days_skipped_no_holdings,
            days_skipped_no_evidence=totals.days_skipped_no_evidence,
            days_already_present=totals.days_already_present,
            fetch_failures=tuple(sorted(totals.fetch_failures)),
            newly_unavailable=tuple(sorted(totals.newly_unavailable)),
        )
        logger.info("snapshot backfill: %s", report.model_dump())
        return report

    def _backfill_one(self, pid: int, totals: _RunTotals) -> None:
        """Fill one portfolio's pre-live-writer days, accumulating into ``totals``."""
        replay_rows = self._trades.open_rows(pid)
        if not replay_rows:
            return
        first_dates = first_trade_dates(replay_rows)
        start = date.fromisoformat(min(first_dates.values()))

        earliest_snapshot = self._snapshots.earliest_timestamp(pid)
        today = datetime.now(timezone.utc).date()
        if earliest_snapshot is None:
            # Leave today to the live writer -- backfill only settled past days.
            end = today
        else:
            end = min(date.fromisoformat(str(earliest_snapshot)[:10]), today)
        if start >= end:
            return

        marker_key = f"{_MARKER_PREFIX}{pid}"
        signature = f"{start.isoformat()}..{end.isoformat()}"
        if self._account_state.get(marker_key) == signature:
            return

        last_day = end - timedelta(days=1)
        present = self._snapshots.dates_present(
            pid, start.isoformat(), last_day.isoformat()
        )
        self._prefetch_evidence(replay_rows, first_dates, start, end, totals)

        day = start
        while day < end:
            totals.days_considered += 1
            as_of = day.isoformat()
            day += timedelta(days=1)
            if as_of in present:
                totals.days_already_present += 1
                continue
            holdings = holdings_as_of(replay_rows, as_of)
            if not holdings:
                totals.days_skipped_no_holdings += 1
                continue
            value = self._value(holdings, as_of)
            if value is None:
                totals.days_skipped_no_evidence += 1
                continue
            if self._snapshots.append_daily_value_if_absent(
                pid, as_of, f"{as_of}T00:00:00+00:00", value
            ):
                totals.rows_written += 1
            else:
                totals.days_already_present += 1

        self._account_state.set(marker_key, signature)

    def _prefetch_evidence(
        self,
        replay_rows: list[tuple[Any, ...]],
        first_dates: dict[str, str],
        start: date,
        end: date,
        totals: _RunTotals,
    ) -> None:
        """Acquire price + FX evidence spanning the fill range, one fetch per ticker.

        Per distinct ticker: ``[first-trade-date, end)`` while the ticker is
        still held on the last fill day, but only ``[first-trade-date,
        day-after-last-trade)`` for a position closed before the window ends
        -- a stock sold years ago is not chased to ``end``. Then one shared
        ``GBPUSD=X`` fetch for ``[start, end)``. Every failure is isolated: a
        permanent one adds the ticker to ``newly_unavailable``, a transient
        one to ``fetch_failures``, and neither stops the next ticker or the
        day loop that follows.
        """
        if self._backfill is None:
            return
        last_dates = last_trade_dates(replay_rows)
        still_held = holdings_as_of(replay_rows, (end - timedelta(days=1)).isoformat())
        for ticker in sorted({row[0] for row in replay_rows}):
            first_trade = first_dates.get(ticker)
            if first_trade is None:
                continue
            span_start = date.fromisoformat(first_trade)
            span_end = end
            last_trade = last_dates.get(ticker)
            if ticker not in still_held and last_trade is not None:
                span_end = min(end, date.fromisoformat(last_trade) + timedelta(days=1))
            if span_start >= span_end:
                continue
            try:
                self._backfill.ensure_coverage(ticker, span_start, span_end)
            except PriceEvidenceUnavailable:
                totals.newly_unavailable.add(ticker)
            except Exception as exc:
                logger.warning("price evidence backfill failed for %s: %s", ticker, exc)
                totals.fetch_failures.add(ticker)
        try:
            self._backfill.ensure_fx_coverage(start, end)
        except PriceEvidenceUnavailable:
            totals.newly_unavailable.add(FX_PAIR)
        except Exception as exc:
            logger.warning("FX evidence backfill failed: %s", exc)
            totals.fetch_failures.add(FX_PAIR)

    def _value(self, holdings: dict[str, float], as_of: str) -> float | None:
        """Return the GBP holdings value at ``as_of``, or None without evidence.

        All-or-nothing: one unpriced holding makes the whole day unavailable,
        and a value that rounds to ``0.00`` is reported as unavailable too --
        same policy as ``SnapshotRepairService._reconstruct``.
        """
        total = 0.0
        for ticker, shares in holdings.items():
            price = self._price_source.gbp_price(ticker, as_of)
            if price is None:
                return None
            total += shares * price
        value = round(total, 2)
        return None if value == 0.0 else value


class _RunTotals:
    """Mutable accumulator threaded through one backfill run."""

    def __init__(self) -> None:
        self.days_considered = 0
        self.rows_written = 0
        self.days_skipped_no_holdings = 0
        self.days_skipped_no_evidence = 0
        self.days_already_present = 0
        self.fetch_failures: set[str] = set()
        self.newly_unavailable: set[str] = set()


def build_backfill_service(trades_connect: Connect) -> SnapshotBackfillService:
    """Wire :class:`SnapshotBackfillService` against the app's real databases.

    ``trades_connect`` opens ``trades.db`` (trades, portfolios, snapshots,
    account state and both FX evidence stores live there); the read-only
    historical price cache and the fetch-on-miss price-evidence backfill are
    wired exactly as the orchestrator wires them for the repair pass.
    """
    from app.core.config import HISTORICAL_PRICE_CACHE
    from app.repositories import db

    backfill_prices = HistoricalPriceRepository(
        db.make_connect(lambda: str(HISTORICAL_PRICE_CACHE))
    )
    backfill_prices.ensure_schema()
    return SnapshotBackfillService(
        TradesRepository(trades_connect),
        PortfolioSnapshotsRepository(trades_connect),
        PortfoliosRepository(trades_connect),
        AccountStateRepository(trades_connect),
        build_price_source(trades_connect),
        backfill=PriceEvidenceBackfillService(backfill_prices),
    )
