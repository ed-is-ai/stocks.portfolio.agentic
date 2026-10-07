"""CLI: classify S&P 500 exits into ``index_membership.db`` (#73).

Usage::

    SEC_USER_AGENT="Name contact@example.com" \\
        uv run python -m app.cli.import_terminal_events sp500 [--dry-run] [--limit N]
        [--price-db PATH] [--wiki-db PATH]

For each interval of the newest ``sp500`` membership import ending since
2000, stores one terminal event (replacing that import's previous events) and
prints counts by type and the ``unknown`` events. ``--dry-run`` writes
nothing; ``--limit N`` classifies only the first N intervals and, since that
is not a full set, also writes nothing. SEC requires a descriptive
``User-Agent``, read from ``SEC_USER_AGENT``; the run refuses to start
without it. Events still unknown get price evidence from the yfinance cache
and the WIKI archive when those databases exist (opened read-only).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from collections import Counter

from app.core.config import (
    HISTORICAL_PRICE_CACHE,
    INDEX_MEMBERSHIP_DB,
    WIKI_PRICES_DB,
)
from app.repositories import db
from app.repositories.index_membership_repo import IndexMembershipRepository
from app.services.index_membership.edgar import sec_fetch
from app.services.index_membership.sp500_import import Fetch, http_fetch
from app.services.index_membership.terminal_events import (
    build_events,
    price_last_trade,
)


def _positive(value: str) -> int:
    """Parse a ``--limit`` of at least 1."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main(
    argv: list[str] | None = None, fetch: Fetch = http_fetch, sec: Fetch | None = None
) -> None:
    """Classify, store and print a summary (``fetch``/``sec`` are injectable).

    Exits non-zero before any request when ``sec`` is not injected and
    ``SEC_USER_AGENT`` is unset.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", choices=["sp500"])
    parser.add_argument("--dry-run", action="store_true", help="Write nothing.")
    parser.add_argument(
        "--limit", type=_positive, help="Only the first N; writes nothing."
    )
    parser.add_argument(
        "--price-db", type=Path, default=HISTORICAL_PRICE_CACHE, help="yfinance cache."
    )
    parser.add_argument(
        "--wiki-db", type=Path, default=WIKI_PRICES_DB, help="WIKI price archive."
    )
    args = parser.parse_args(argv)
    if sec is None:
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not user_agent:
            raise SystemExit("SEC_USER_AGENT is not set; SEC requires a contact UA")
        sec = sec_fetch(user_agent)
    repo = IndexMembershipRepository(db.make_connect(lambda: INDEX_MEMBERSHIP_DB))
    repo.ensure_schema()
    try:
        last_trade = price_last_trade(args.price_db, args.wiki_db)
        import_id, events = build_events(repo, fetch, sec, args.limit, last_trade)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    written = not args.dry_run and args.limit is None
    if written:
        repo.replace_terminal_events(import_id, events)
    print(f"events: {len(events)} (membership import {import_id})")
    print(f"written: {'yes' if written else 'no'}")
    for event_type, count in sorted(Counter(e.event_type for e in events).items()):
        print(f"  {event_type}: {count}")
    for event in events:
        if event.event_type == "unknown":
            print(f"  unknown {event.security_key} {event.exit_date}: {event.note}")


if __name__ == "__main__":
    main()
