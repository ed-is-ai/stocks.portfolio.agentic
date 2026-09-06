"""CLI: repair zero-valued portfolio snapshots in ``trades.db`` (#466).

Usage::

    uv run python -m app.cli.repair_portfolio_snapshots [--portfolio-id N] [--dry-run] [--no-historical-evidence]

Rows written as a bogus ``0.00`` by the pre-#466 snapshot writer -- and rows
this pass has previously nulled -- are either reconstructed from dated
historical evidence in ``historical_price_cache.db`` or left/rewritten as
``NULL`` so the value-history chart shows an honest gap. Safe to re-run: a
second pass leaves every row exactly as it found it (``repaired`` and
``marked_unavailable`` both ``0``); ``candidates`` stays non-zero because
each remaining gap is genuinely re-offered to the evidence store.
"""

from __future__ import annotations

import argparse
import logging

from app.core.config import HISTORICAL_PRICE_CACHE, TRADES_DB
from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.trades_repo import TradesRepository
from app.services.snapshot_price_backfill import PriceEvidenceBackfillService
from app.services.snapshot_price_evidence import build_price_source
from app.services.snapshot_repair import (
    HistoricalGbpPriceSource,
    NoHistoricalPriceSource,
    SnapshotRepairService,
)


def main(argv: list[str] | None = None) -> None:
    """Run the repair pass and print its report.

    ``argv`` defaults to ``sys.argv[1:]`` (via ``argparse``) but can be
    supplied explicitly, letting tests drive this entry point directly
    instead of monkeypatching ``sys.argv``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--portfolio-id",
        type=int,
        default=None,
        help="Restrict the pass to one portfolio (default: every portfolio).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--no-historical-evidence",
        action="store_true",
        help=(
            "Skip the historical price cache and null every candidate row "
            "instead of reconstructing it. Also disables the carrying-cost "
            "estimate for an unpriceable holding (#519), so no row is "
            "written as estimated."
        ),
    )
    parser.add_argument(
        "--with-backfill",
        action="store_true",
        help=(
            "Also fetch-on-miss historical evidence for held tickers with no "
            "coverage (#490). Off by default -- this manual tool otherwise "
            "makes no network calls; the automatic path is the orchestrator."
        ),
    )
    args = parser.parse_args(argv)
    # Without this the service's report line and the price source's
    # per-holding "no evidence" reasons -- the only way to learn *why* a row
    # stayed a gap -- are swallowed by the root logger's default level.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    connect = db.make_connect(lambda: TRADES_DB)
    conn = connect()
    try:
        # Ensure the nullable-column migration (#466) has run: this module is
        # a standalone entry point, so it cannot assume a TraderAgent has
        # already initialized the schema against this database file.
        db.init_trades_db(conn)
    finally:
        conn.close()
    price_source: HistoricalGbpPriceSource = (
        NoHistoricalPriceSource()
        if args.no_historical_evidence
        else build_price_source(connect)
    )
    backfill = None
    if args.with_backfill:
        price_cache_connect = db.make_connect(lambda: str(HISTORICAL_PRICE_CACHE))
        price_repo = HistoricalPriceRepository(price_cache_connect)
        price_repo.ensure_schema()
        backfill = PriceEvidenceBackfillService(price_repo)
    service = SnapshotRepairService(
        TradesRepository(connect),
        PortfolioSnapshotsRepository(connect),
        price_source,
        backfill=backfill,
        estimate_unpriceable=not args.no_historical_evidence,
    )
    report = service.repair(portfolio_id=args.portfolio_id, dry_run=args.dry_run)
    for field, value in report.model_dump().items():
        print(f"{field}: {value}")


if __name__ == "__main__":
    main()
