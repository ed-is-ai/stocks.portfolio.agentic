"""Resume an offline historical-evidence v1-to-v2 migration.

Run against a writable offline database copy. Each invocation verifies and
checkpoints every migrated revision; use --activate only after it reports a
complete migration. The original v1 records remain available for rollback.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-db", type=Path, required=True)
    parser.add_argument(
        "--max-revisions",
        type=int,
        help="Migrate at most this many revisions, then leave a durable checkpoint.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--activate",
        action="store_true",
        help="Activate verified v2 reads after migration.",
    )
    action.add_argument(
        "--rollback", action="store_true", help="Return reads to retained v1 evidence."
    )
    parser.add_argument(
        "--activation-review",
        help="Recorded capacity-review reference required with --activate.",
    )
    args = parser.parse_args()
    if args.max_revisions is not None and args.max_revisions < 1:
        parser.error("--max-revisions must be positive")
    if args.activate and not args.activation_review:
        parser.error("--activate requires --activation-review")
    path = args.historical_db.resolve()
    if not path.is_file():
        parser.error("--historical-db must name an existing offline database copy")

    repo = HistoricalPriceRepository(db.make_connect(lambda: path))
    if args.rollback:
        repo.rollback_v2_activation()
        print(json.dumps({"active_format": "v1", "rolled_back": True}))
        return

    progress = repo.migrate_v1_to_v2(max_revisions=args.max_revisions)
    activated = False
    if args.activate:
        repo.activate_v2(review_reference=args.activation_review)
        activated = True
    print(
        json.dumps(
            {
                "source_revision_count": progress.source_revision_count,
                "migrated_revision_count": progress.migrated_revision_count,
                "completed": progress.completed,
                "source_database_bytes": progress.source_database_bytes,
                "available_bytes": progress.available_bytes,
                "required_reserve_bytes": progress.required_reserve_bytes,
                "active_format": "v2" if activated else "v1",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
