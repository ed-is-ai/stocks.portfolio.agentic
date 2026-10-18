"""CLI: acquire the pinned UCITS ETF universe evidence (#117).

Usage::

    uv run python -m app.cli.acquire_etf_universe [--end YYYY-MM-DD] [--no-bitcoin]

Fetches each pinned symbol from its verified inception into
``historical_price_cache.db`` under ``etf:<SYMBOL>`` and prints one line per
symbol. A failing symbol is reported and never aborts the others.
"""

from __future__ import annotations

import argparse
from datetime import date

from app.core.config import HISTORICAL_PRICE_CACHE
from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.etf_universe import (
    EtfUniverseEvidenceService,
    load_etf_universe,
)


def main(argv: list[str] | None = None) -> int:
    """Acquire every pinned symbol, print its outcome; 1 if any symbol failed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=date.today(),
        help="Exclusive end date (default: today).",
    )
    parser.add_argument(
        "--no-bitcoin", action="store_true", help="Skip the optional BTC-GBP sleeve."
    )
    args = parser.parse_args(argv)
    prices = HistoricalPriceRepository(db.make_connect(lambda: HISTORICAL_PRICE_CACHE))
    prices.ensure_schema()
    outcomes = EtfUniverseEvidenceService(prices).acquire(
        load_etf_universe(), end=args.end, include_optional=not args.no_bitcoin
    )
    for o in outcomes:
        status = o.error or f"revision={o.data_revision}"
        gaps = ",".join(o.incomplete_months) or "none"
        print(f"{o.symbol}: {status} incomplete_months={gaps}")
    return 1 if any(o.error for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
