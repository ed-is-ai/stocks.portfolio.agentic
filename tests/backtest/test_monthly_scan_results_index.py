"""Regression: monthly_scan_results lookups must seek, not scan.

``BacktestRepository.latest_committed_scan_result`` filters
``(profile_hash, security_id)`` and joins ``snapshot_month`` rather than
binding it as a literal, so the table's primary key -- whose leftmost
prefix stops at ``profile_hash`` -- can't serve the second filter column.
Without ``idx_monthly_scan_results_profile_security``, this scans every
month recorded for the profile on every call (once per security per
trading day during a backtest run) instead of seeking straight to the
security's rows.
"""

from __future__ import annotations

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository


def test_monthly_scan_results_seeks_by_profile_and_security(tmp_path):
    connect = db.make_connect(lambda: tmp_path / "backtest.db")
    repo = BacktestRepository(connect)
    repo.ensure_schema()

    with db.session(connect) as conn:
        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT r.snapshot_month, r.historical_scan_record_json, r.record_digest
            FROM monthly_scan_results r
            JOIN snapshot_months m
              ON m.profile_hash = r.profile_hash
             AND m.snapshot_month = r.snapshot_month
            JOIN snapshot_members mem
              ON mem.profile_hash = r.profile_hash
             AND mem.snapshot_month = r.snapshot_month
             AND mem.security_id = r.security_id
            WHERE r.profile_hash = ?
              AND r.security_id = ?
              AND mem.resolution = 'valid_scan'
              AND mem.as_of_session_date <= ?
              AND m.processing_complete = 1
              AND m.market_complete = 'unknown'
            ORDER BY mem.as_of_session_date DESC, r.snapshot_month DESC LIMIT 1
            """,
            ("profile", "AAPL", "2024-01-01"),
        ).fetchall()

    steps = "\n".join(str(row) for row in plan)
    assert "SCAN r" not in steps, steps
    assert "idx_monthly_scan_results_profile_security" in steps, steps
