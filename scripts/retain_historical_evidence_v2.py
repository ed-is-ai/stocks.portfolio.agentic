"""Plan or execute reviewed, offline v2 historical-evidence retention."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-db", required=True, type=Path)
    parser.add_argument("--grace-before", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--review-reference")
    args = parser.parse_args()
    if args.execute and not args.review_reference:
        parser.error("--execute requires --review-reference")
    path = args.historical_db.resolve()
    if not path.is_file():
        parser.error("--historical-db must name an existing offline database copy")
    repo = HistoricalPriceRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    plan = repo.plan_v2_retention(grace_before=args.grace_before)
    report: dict[str, object] = {
        "grace_before": plan.grace_before,
        "candidates": plan.candidates,
        "exclusions": dict(plan.exclusions),
        "executed": False,
    }
    if args.execute:
        revisions, chunks = repo.execute_v2_retention(
            plan, review_reference=args.review_reference
        )
        report.update(executed=True, deleted_revisions=revisions, deleted_chunks=chunks)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
