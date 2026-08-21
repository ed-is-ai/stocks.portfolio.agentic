from __future__ import annotations

import sqlite3

import pytest

from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory
from app.services.backtest.notification_projector import StrategyNotificationProjector
from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobStatus,
)

from tests.backtest.test_strategy_job_repository import (
    INSTANCE_A,
    INSTANCE_B,
    LEASE_TTL,
    _MovableClock,
    _enqueue,
    _enqueue_backtest,
    _leased_repo,
    _repo,
)


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


def test_queued_initialization_projects_activity_deep_link(tmp_path):
    """A non-tombstoned initialization job's projected notification must
    also deep-link to the working Activity shell -- never the dead
    ``?job_id=`` query-string format the projector used to emit before
    this story (mirrors the backtest deep-link assertion below)."""
    path = tmp_path / "backtest.db"
    notifications_path = tmp_path / "notifications.db"
    repo = _repo(path)
    job = _enqueue(repo, "2026-05", "2026-05").job
    assert job is not None
    notifications = NotificationsRepository(db.make_connect(lambda: notifications_path))
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)

    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]

    assert item.target_url == f"/strategy-manager/activities/{job.id}"


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


def test_projection_ignores_malformed_row_and_keeps_polling(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue(repo, "2026-05", "2026-05")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE notification_outbox SET payload_json=?",
            ("{not-json",),
        )
    notifications = NotificationsRepository(
        db.make_connect(lambda: tmp_path / "notifications.db")
    )
    notifications.ensure_schema()
    assert StrategyNotificationProjector(repo, notifications).project_pending() == 0


def test_existing_job_is_backfilled_and_restart_removes_restart_action(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    source = _enqueue(repo, "2026-05", "2026-05").job
    assert source is not None
    failed = _fail(repo, source.id)
    child = repo.restart_initialization_job(
        source.id, expected_version=failed.status_version, idempotency_key="retry"
    ).job
    assert child is not None
    assert repo.legal_strategy_job_actions(source.id) == ("delete",)


def test_delete_rejects_stale_version_after_tombstone(tmp_path):
    repo = _repo(tmp_path / "backtest.db")
    source = _enqueue(repo, "2026-05", "2026-05").job
    assert source is not None
    failed = _fail(repo, source.id)
    deleted = repo.delete_strategy_job(
        source.id, expected_version=failed.status_version
    )
    with pytest.raises(StrategyJobConflict, match="stale"):
        repo.delete_strategy_job(source.id, expected_version=failed.status_version)
    assert deleted.deleted_at is not None


# ---------------------------------------------------------------------------
# Story 2.6: Backtest notification projection -- title/body/target/category.
# ---------------------------------------------------------------------------


def _fail_backtest(repo, job_id: str):
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


def test_queued_backtest_projects_working_activity_deep_link_and_category(tmp_path):
    path = tmp_path / "backtest.db"
    notifications_path = tmp_path / "notifications.db"
    repo = _repo(path)
    enqueued = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    notifications = NotificationsRepository(db.make_connect(lambda: notifications_path))
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)

    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]

    assert item.category is NotificationCategory.BACKTEST
    assert item.title == "Backtest queued (momentum_v1)"
    assert item.target_url == f"/strategy-manager/activities/{enqueued.job.id}"
    assert item.body.startswith("Status: queued.")


def test_failed_backtest_projects_strategy_identity_title_and_detail(tmp_path):
    path = tmp_path / "backtest.db"
    notifications_path = tmp_path / "notifications.db"
    repo = _repo(path)
    enqueued = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    failed = _fail_backtest(repo, enqueued.job.id)
    notifications = NotificationsRepository(db.make_connect(lambda: notifications_path))
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)

    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]

    assert item.title == "Backtest failed (momentum_v1)"
    assert "Required historical data is unavailable" in item.body
    assert item.job_status_version == failed.status_version
    assert item.target_url == f"/strategy-manager/activities/{enqueued.job.id}"


def test_backtest_tombstone_projection_removes_actions_and_target(tmp_path):
    path = tmp_path / "backtest.db"
    notifications_path = tmp_path / "notifications.db"
    repo = _repo(path)
    enqueued = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    failed = _fail_backtest(repo, enqueued.job.id)
    repo.delete_strategy_job(enqueued.job.id, expected_version=failed.status_version)
    notifications = NotificationsRepository(db.make_connect(lambda: notifications_path))
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)

    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]

    assert item.title == "Activity no longer available"
    assert item.target_url is None
    assert item.actions == ()


# ---------------------------------------------------------------------------
# Story 4.1: lease-takeover reconciliation and stage-activity notifications.
# ---------------------------------------------------------------------------


def test_lease_takeover_fails_the_claim_its_evicted_owner_abandoned(tmp_path):
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    first = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)
    bootstrap = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job(lease=first.fence)
    assert claim is not None

    clock.advance(LEASE_TTL + 1)
    second = repo.acquire_or_renew_worker_lease(INSTANCE_B, ttl_seconds=LEASE_TTL)
    reconciled = repo.reconcile_interrupted_strategy_jobs(lease=second.fence)

    assert [job.id for job in reconciled] == [bootstrap.id]
    abandoned = repo.strategy_job(bootstrap.id)
    assert abandoned.status is StrategyJobStatus.FAILED
    assert abandoned.failure_code is JobFailureCode.WORKER_INTERRUPTED
    assert abandoned.owner_instance_id is None


def test_reconciliation_never_touches_a_row_the_current_lease_owns(tmp_path):
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    lease = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)
    preparation = repo.create_preparation_job()
    claim = repo.claim_next_strategy_job(lease=lease.fence)
    assert claim is not None
    running = repo.strategy_job(preparation.id)

    assert repo.reconcile_interrupted_strategy_jobs(lease=lease.fence) == ()

    assert repo.strategy_job(preparation.id) == running
    assert running.status is StrategyJobStatus.RUNNING
    assert running.owner_instance_id == INSTANCE_A
    assert running.lease_generation == lease.generation


def test_stage_activity_notifications_carry_their_own_title_and_actions(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    bootstrap = repo.create_bootstrap_job()
    notifications = NotificationsRepository(
        db.make_connect(lambda: tmp_path / "notifications.db")
    )
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)

    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]

    assert item.title == "Bootstrap queued"
    assert item.target_url == f"/strategy-manager/activities/{bootstrap.id}"
    assert item.category is NotificationCategory.STRATEGY_INITIALIZATION


def test_failed_preparation_offers_delete_only(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    preparation = repo.create_preparation_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    repo.fail_claimed_strategy_job(
        preparation.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.WORKER_INTERRUPTED,
        failed_month=None,
        detail="Worker interrupted before completion",
    )
    notifications = NotificationsRepository(
        db.make_connect(lambda: tmp_path / "notifications.db")
    )
    notifications.ensure_schema()
    projector = StrategyNotificationProjector(repo, notifications)

    assert projector.project_pending() == 1
    item = notifications.recent(include_dismissed=True)[0]

    assert item.title == "Evidence preparation failed"
    assert "Worker interrupted before completion" in item.body
    assert repo.legal_strategy_job_actions(preparation.id) == ("delete",)
