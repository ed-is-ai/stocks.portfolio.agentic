"""Readiness and diagnostics projection for Strategy Manager.

Composes six independent prerequisites (qualification, roster, active
profile, coverage, worker, discovery) plus worker state and bounded
recent failures into :class:`StrategyReadinessV1`. Every method is
read-only -- it never creates, repairs, acquires, activates, or queues
anything.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import SKILLS_DIR
from app.repositories.backtest_repo import BacktestIntegrityError
from app.services.backtest.skill_discovery import discover_strategies
from app.services.backtest.snapshot_profile import adoption_gate_failures
from app.services.backtest.strategy_job import (
    PrerequisiteItemV1,
    PrerequisiteState,
    RecoveryAction,
    StrategyJobStatus,
    StrategyReadinessV1,
    WorkerReadinessV1,
    WorkerState,
)

if TYPE_CHECKING:
    from app.repositories.backtest_repo import BacktestRepository


def _is_fixture_environment() -> bool:
    """Return True when running in a test/fixture environment."""
    return bool(
        os.environ.get("STRATEGY_FIXTURE") or os.environ.get("PYTEST_CURRENT_TEST")
    )


def profile_delta_projection(
    repository: "BacktestRepository", profile_hash: str
) -> dict[str, object] | None:
    """Predecessor-delta projection for one profile, read-only (gh-468).

    Returns ``None`` when there is no discoverable predecessor with
    committed months; otherwise the added/removed/unchanged member counts
    versus that predecessor, whether the Update path is available, and the
    reasons when it is not.
    """
    try:
        previous = repository.previous_snapshot_profile(profile_hash)
        current = repository.snapshot_profile(profile_hash)
    except BacktestIntegrityError:
        return None
    if previous is None or current is None or not repository.profile_has_committed_months(
        previous.profile_hash
    ):
        return None
    delta = repository.profile_member_delta(previous.profile_hash, profile_hash)
    if delta is None:
        return None
    failures = list(adoption_gate_failures(previous, current))
    return {
        "previous_profile_hash": previous.profile_hash,
        "added": len(delta.added),
        "removed": len(delta.removed),
        "unchanged": len(delta.unchanged),
        "update_available": not failures,
        "update_blocked_reasons": failures,
    }


class StrategyReadinessService:
    """Compose six prerequisites + worker state + recent failures.

    Every method is read-only: it queries existing repository state and
    projects it into typed models. It never mutates, creates, or
    enqueues anything.
    """

    def __init__(
        self,
        repository: "BacktestRepository",
        *,
        skills_root: Path | None = None,
        clock: "datetime | None" = None,
    ) -> None:
        self._repository = repository
        self._skills_root = skills_root or SKILLS_DIR
        self._clock = clock

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock
        return datetime.now(timezone.utc)

    def evaluate(self) -> StrategyReadinessV1:
        """Compose six prerequisites independently, read-only."""
        now = self._now()
        return StrategyReadinessV1(
            qualification=self._evaluate_qualification(now),
            roster=self._evaluate_roster(now),
            active_profile=self._evaluate_active_profile(now),
            coverage=self._evaluate_coverage(now),
            worker=self._evaluate_worker(now),
            discovery=self._evaluate_discovery(now),
            recent_failures=self._repository.recent_job_failures(),
            is_fixture=_is_fixture_environment(),
        )

    def profile_delta(self) -> dict[str, object] | None:
        """Predecessor-delta projection for the active profile (gh-468)."""
        active = self._repository.active_snapshot_profile()
        if active is None:
            return None
        return profile_delta_projection(self._repository, active.profile_hash)

    def diagnostics(
        self, readiness: StrategyReadinessV1 | None = None
    ) -> dict[str, object]:
        """Return bounded diagnostics with recent failures."""
        if readiness is None:
            readiness = self.evaluate()
        return {
            "is_fixture": readiness.is_fixture,
            "profile_delta": self.profile_delta(),
            "prerequisites": [
                {
                    "name": item.name,
                    "state": item.state.value,
                    "reason": item.reason,
                    "recovery_action": item.recovery_action.value,
                }
                for item in (
                    readiness.qualification,
                    readiness.roster,
                    readiness.active_profile,
                    readiness.coverage,
                    readiness.discovery,
                )
            ],
            "worker": {
                "state": readiness.worker.state.value,
                "reason": readiness.worker.reason,
                "recovery_action": (readiness.worker.recovery_action.value),
            },
            "recent_failures": [
                {
                    "job_id": f.job_id,
                    "job_type": f.job_type.value,
                    "failure_code": f.failure_code.value,
                    "stage_or_month": f.stage_or_month,
                    "recovery_action": f.recovery_action.value,
                }
                for f in readiness.recent_failures
            ],
        }

    def _evaluate_qualification(self, now: datetime) -> PrerequisiteItemV1:
        digest = self._repository.current_qualification_contract_digest()
        if digest is not None:
            return PrerequisiteItemV1(
                name="qualification",
                state=PrerequisiteState.READY,
                reason="Historical data qualification is current",
                last_verified_at=now,
                recovery_action=RecoveryAction.NONE,
            )
        return PrerequisiteItemV1(
            name="qualification",
            state=PrerequisiteState.MISSING,
            reason="Historical data providers have not passed certification",
            last_verified_at=now,
            recovery_action=RecoveryAction.SET_UP,
        )

    def _evaluate_roster(self, now: datetime) -> PrerequisiteItemV1:
        active = self._repository.active_snapshot_profile()
        if active is None:
            return PrerequisiteItemV1(
                name="roster",
                state=PrerequisiteState.MISSING,
                reason="No active profile identifies a reconstruction roster",
                last_verified_at=now,
                recovery_action=RecoveryAction.SET_UP,
            )
        try:
            profile = self._repository.snapshot_profile(active.profile_hash)
            roster_json = (
                None
                if profile is None
                else self._repository.roster_manifest_json(profile.roster_digest)
            )
        except Exception:
            profile = None
            roster_json = None
        if profile is not None and roster_json is not None:
            return PrerequisiteItemV1(
                name="roster",
                state=PrerequisiteState.READY,
                reason="Reconstruction roster is available",
                last_verified_at=now,
                recovery_action=RecoveryAction.NONE,
            )
        return PrerequisiteItemV1(
            name="roster",
            state=PrerequisiteState.MISSING,
            reason="The active profile has no usable reconstruction roster",
            last_verified_at=now,
            recovery_action=RecoveryAction.SET_UP,
        )

    def _evaluate_active_profile(self, now: datetime) -> PrerequisiteItemV1:
        active = self._repository.active_snapshot_profile()
        if active is not None:
            return PrerequisiteItemV1(
                name="active_profile",
                state=PrerequisiteState.READY,
                reason=(
                    f"Active profile {active.profile_hash[:8]} "
                    f"(seq {active.activation_seq})"
                ),
                last_verified_at=now,
                recovery_action=RecoveryAction.NONE,
            )
        return PrerequisiteItemV1(
            name="active_profile",
            state=PrerequisiteState.MISSING,
            reason="No active snapshot profile is configured",
            last_verified_at=now,
            recovery_action=RecoveryAction.SET_UP,
        )

    def _evaluate_coverage(self, now: datetime) -> PrerequisiteItemV1:
        active = self._repository.active_snapshot_profile()
        if active is None:
            return PrerequisiteItemV1(
                name="coverage",
                state=PrerequisiteState.MISSING,
                reason="No active profile to check coverage against",
                last_verified_at=now,
                recovery_action=RecoveryAction.SET_UP,
            )
        try:
            coverage = self._repository.snapshot_coverage(active.profile_hash)
            if coverage.snapshot_count > 0:
                return PrerequisiteItemV1(
                    name="coverage",
                    state=PrerequisiteState.READY,
                    reason=(
                        f"{coverage.snapshot_count} monthly snapshots "
                        f"({coverage.earliest_month} to "
                        f"{coverage.latest_month})"
                    ),
                    last_verified_at=now,
                    recovery_action=RecoveryAction.NONE,
                )
            return PrerequisiteItemV1(
                name="coverage",
                state=PrerequisiteState.MISSING,
                reason="No monthly snapshots have been initialized",
                last_verified_at=now,
                recovery_action=RecoveryAction.INITIALIZE,
            )
        except Exception:
            return PrerequisiteItemV1(
                name="coverage",
                state=PrerequisiteState.INTEGRITY_ERROR,
                reason="Coverage evidence could not be read",
                last_verified_at=now,
                recovery_action=RecoveryAction.INITIALIZE,
            )

    def _evaluate_worker(self, now: datetime) -> WorkerReadinessV1:
        lease = self._repository.read_worker_lease()
        if lease is None:
            return WorkerReadinessV1(
                state=WorkerState.DISABLED,
                reason="No worker lease has been acquired",
                last_heartbeat_at=None,
                recovery_action=RecoveryAction.RECONCILE_WORKER,
            )
        if lease.expires_at <= now:
            return WorkerReadinessV1(
                state=WorkerState.UNAVAILABLE_INTERRUPTED,
                reason="Worker lease has expired (interrupted)",
                last_heartbeat_at=lease.heartbeat_at,
                recovery_action=RecoveryAction.RECONCILE_WORKER,
            )
        # Check if a running job exists
        jobs = self._repository.list_strategy_jobs()
        running = next(
            (j for j in jobs if j.status is StrategyJobStatus.RUNNING),
            None,
        )
        if running is not None:
            return WorkerReadinessV1(
                state=WorkerState.BUSY,
                reason=f"Worker is running job {running.id}",
                last_heartbeat_at=lease.heartbeat_at,
                recovery_action=RecoveryAction.NONE,
            )
        return WorkerReadinessV1(
            state=WorkerState.READY,
            reason="Worker lease is active and no job is running",
            last_heartbeat_at=lease.heartbeat_at,
            recovery_action=RecoveryAction.NONE,
        )

    def _evaluate_discovery(self, now: datetime) -> PrerequisiteItemV1:
        try:
            result = discover_strategies(self._skills_root)
        except Exception:
            return PrerequisiteItemV1(
                name="discovery",
                state=PrerequisiteState.FAILED,
                reason="Strategy discovery encountered an error",
                last_verified_at=now,
                recovery_action=RecoveryAction.NONE,
            )
        if result.strategies:
            count = len(result.strategies)
            reason = f"{count} Strateg{'ies' if count != 1 else 'y'} discoverable"
            if result.warnings:
                reason += (
                    f" ({len(result.warnings)} warning"
                    f"{'s' if len(result.warnings) != 1 else ''})"
                )
            return PrerequisiteItemV1(
                name="discovery",
                state=PrerequisiteState.READY,
                reason=reason,
                last_verified_at=now,
                recovery_action=RecoveryAction.NONE,
            )
        if result.warnings:
            return PrerequisiteItemV1(
                name="discovery",
                state=PrerequisiteState.FAILED,
                reason=(
                    f"No valid Strategies discovered "
                    f"({len(result.warnings)} warning"
                    f"{'s' if len(result.warnings) != 1 else ''})"
                ),
                last_verified_at=now,
                recovery_action=RecoveryAction.NONE,
            )
        return PrerequisiteItemV1(
            name="discovery",
            state=PrerequisiteState.MISSING,
            reason="No Strategy Skills are discoverable",
            last_verified_at=now,
            recovery_action=RecoveryAction.NONE,
        )


__all__ = ["StrategyReadinessService"]
