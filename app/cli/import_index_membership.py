"""CLI: import point-in-time index membership into ``index_membership.db`` (#68).

Usage::

    uv run python -m app.cli.import_index_membership sp500 [--dry-run] [--wikipedia]

Downloads fja05680/sp500 pinned to the branch head's commit, derives dated
membership intervals, cross-checks them against the dataset's start/end file
and prints a summary. Re-running against an unchanged source records nothing.
``--dry-run`` writes nothing; ``--wikipedia`` also reports differences from
Wikipedia's current constituents, its changes the dataset lacks, and changes
after the dataset's last date.
"""

from __future__ import annotations

import argparse

from app.core.config import INDEX_MEMBERSHIP_DB
from app.repositories import db
from app.repositories.index_membership_repo import IndexMembershipRepository
from app.services.index_membership.sp500_import import (
    Fetch,
    http_fetch,
    import_sp500,
)
from app.services.index_membership.wikipedia_check import fetch_wikipedia_diff


def main(argv: list[str] | None = None, fetch: Fetch = http_fetch) -> None:
    """Run the import and print its summary (``fetch`` is injectable)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", choices=["sp500"])
    parser.add_argument("--dry-run", action="store_true", help="Write nothing.")
    parser.add_argument(
        "--wikipedia", action="store_true", help="Also diff against Wikipedia."
    )
    args = parser.parse_args(argv)
    repo = None
    if not args.dry_run:
        repo = IndexMembershipRepository(db.make_connect(lambda: INDEX_MEMBERSHIP_DB))
        repo.ensure_schema()
    summary = import_sp500(fetch, repo)
    for field, value in summary.model_dump(
        exclude={"latest_members", "intervals"}
    ).items():
        if isinstance(value, list):
            print(f"{field}: {len(value)}")
            for item in value:
                print(f"  {item}")
        else:
            print(f"{field}: {value}")
    if args.wikipedia:
        diff = fetch_wikipedia_diff(
            fetch, summary.intervals, summary.first_date, summary.last_date
        )
        print(f"wikipedia only_in_dataset: {diff.only_in_dataset}")
        print(f"wikipedia only_in_wikipedia: {diff.only_in_wikipedia}")
        print(f"wikipedia skipped_change_rows: {diff.skipped_change_rows}")
        print(f"wikipedia unmatched_changes: {len(diff.unmatched_changes)}")
        for unmatched in diff.unmatched_changes:
            print(f"  {unmatched}")
        for change in diff.changes_after:
            print(f"  change after dataset: {change.model_dump()}")


if __name__ == "__main__":
    main()
