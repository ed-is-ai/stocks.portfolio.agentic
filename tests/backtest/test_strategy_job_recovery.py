from __future__ import annotations

import sqlite3

import pytest

from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.services.backtest.notification_projector import StrategyNotificationProjector
from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobConflict,
)

from tests.backtest.test_strategy_job_repository import _enqueue, _repo


def _fail(repo, job_id: str):
    claim = repo.claim_next_strategy_job()
    assert claim is not None and claim.job.id == job_id
    return repo.fail_claimed_strategy_job(
        job_id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )


def test_every_lifecycle_version_has_one_outbox_projection(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    job = _enqueue(repo, "2026-05", "2026-05").job
    assert job is not None
    claimed = repo.claim_next_strategy_job()
    assert claimed is not None
    failed = repo.fail_claimed_strategy_job(
        job.id,
        claimed.claim_token,
        expected_version=claimed.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="safe failure",
    )

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT job_status_version, pending FROM notification_outbox WHERE job_id=?",
            (job.id,),
        ).fetchall()
    assert rows == [(failed.status_version, 1)]


def test_projector_is_idempotent_and_preserves_notification_read_state(tmp_path):
    path = tmp_path / "backtest.db"
    notifications_path = tmp_path / "notifications.db"
    repo = _repo(path)
    job = _enqueue(repo, "2026-05", "2026-05").job
    assert job is not None
    notification_repo = NotificationsRepository(
        db.make_connect(lambda: notifications_path)
    )
    notification_repo.ensure_schema()
    projector = StrategyNotificationProjector(repo, notification_repo)

    assert projector.project_pending() == 1
    assert projector.project_pending() == 0
    projected = notification_repo.recent(include_dismissed=True)
    assert len(projected) == 1
    notification_repo.mark_read(projected[0].id or 0)

    claimed = repo.claim_next_strategy_job()
    assert claimed is not None
    assert projector.project_pending() == 1
    refreshed = notification_repo.recent(include_dismissed=True)[0]
    assert refreshed.job_id == job.id
    assert refreshed.job_status_version == claimed.job.status_version
    assert refreshed.read_at is not None


def test_restart_is_same_key_idempotent_and_replays_from_beginning(tmp_path):
    repo = _repo(tmp_path / "backtest.db")
    source = _enqueue(repo, "2026-05", "2026-07").job
    assert source is not None
    failed = _fail(repo, source.id)

    first = repo.restart_initialization_job(
        source.id, expected_version=failed.status_version, idempotency_key="retry-1"
    )
    assert first.job is not None and first.initialization is not None
    assert first.job.parent_job_id == source.id
    assert first.initialization.requested_months == ("2026-05", "2026-06", "2026-07")
    assert first.initialization.ordered_month_digest is None

    repeated = repo.restart_initialization_job(
        source.id, expected_version=failed.status_version, idempotency_key="retry-1"
    )
    assert repeated.job == first.job
    assert len(repo.list_strategy_jobs()) == 2
    with pytest.raises(StrategyJobConflict, match="already has"):
        repo.restart_initialization_job(
            source.id, expected_version=failed.status_version, idempotency_key="retry-2"
        )


def test_delete_tombstones_attempt_and_keeps_shared_evidence(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    source = _enqueue(repo, "2026-05", "2026-05").job
    assert source is not None
    failed = _fail(repo, source.id)
    deleted = repo.delete_strategy_job(
        source.id, expected_version=failed.status_version
    )
    assert deleted.deleted_at is not None
    assert deleted.audit_summary is not None
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initialization_runs WHERE job_id=?", (source.id,)
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM snapshot_profiles").fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE job_id=?", (source.id,)
        ).fetchone() == (1,)
    assert (
        repo.delete_strategy_job(source.id, expected_version=deleted.status_version)
        == deleted
    )


def test_tombstone_projection_removes_actions_and_target(tmp_path):
    path = tmp_path / "backtest.db"
    notifications_path = tmp_path / "notifications.db"
    repo = _repo(path)
    source = _enqueue(repo, "2026-05", "2026-05").job
    assert source is not None
    failed = _fail(repo, source.id)
    repo.delete_strategy_job(source.id, expected_version=failed.status_version)
    notifications = NotificationsRepository(db.make_connect(lambda: notifications_path))
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)
    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]
    assert item.title == "Activity no longer available"
    assert item.target_url is None
    assert item.actions == ()


def test_notification_projection_rejects_lower_version(tmp_path):
    from datetime import datetime, timezone

    from app.schemas.notification import NotificationCategory, NotificationSeverity

    notifications_path = tmp_path / "notifications.db"
    notifications = NotificationsRepository(db.make_connect(lambda: notifications_path))
    notifications.ensure_schema()
    common = dict(
        job_id="job-1",
        category=NotificationCategory.STRATEGY_INITIALIZATION,
        severity=NotificationSeverity.INFO,
        created_at=datetime.now(timezone.utc),
        target_url="/strategy-manager?job_id=job-1",
        actions=("cancel",),
    )
    notifications.upsert_job_notification(
        **common,
        job_status_version=2,
        event_type="strategy_job_running",
        title="Current",
        body="Current",
    )
    with pytest.raises(ValueError, match="newer"):
        notifications.upsert_job_notification(
            **common,
            job_status_version=1,
            event_type="strategy_job_queued",
            title="Old",
            body="Old",
        )
