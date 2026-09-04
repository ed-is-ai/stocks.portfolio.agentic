"""CLI: repair zero-valued portfolio snapshots in ``trades.db`` (#466).

Usage::

    uv run python scripts/repair_portfolio_snapshots.py [--portfolio-id N] [--dry-run]

Rows written as a bogus ``0.00`` by the pre-#466 snapshot writer are either
reconstructed from historical evidence (none ships by default) or rewritten as
``NULL`` so the value-history chart shows an honest gap. Safe to re-run: a
second pass reports every row as unchanged.
"""

from __future__ import annotations

import argparse

from app.core.config import TRADES_DB
from app.repositories import db
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.trades_repo import TradesRepository
from app.services.snapshot_repair import SnapshotRepairService


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
    args = parser.parse_args(argv)

    connect = db.make_connect(lambda: TRADES_DB)
    conn = connect()
    try:
        # Ensure the nullable-column migration (#466) has run: this script is
        # a standalone entry point, so it cannot assume a TraderAgent has
        # already initialized the schema against this database file.
        db.init_trades_db(conn)
    finally:
        conn.close()
    service = SnapshotRepairService(
        TradesRepository(connect), PortfolioSnapshotsRepository(connect)
    )
    report = service.repair(portfolio_id=args.portfolio_id, dry_run=args.dry_run)
    for field, value in report.model_dump().items():
        print(f"{field}: {value}")


if __name__ == "__main__":
    main()
