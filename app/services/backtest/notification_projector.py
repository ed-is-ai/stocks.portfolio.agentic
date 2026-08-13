"""Repairable projection of durable Strategy Manager activity into notifications."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import cast

from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity

logger = logging.getLogger(__name__)


def _projection(payload: dict[str, object]) -> dict[str, object]:
    job = payload.get("job")
    if not isinstance(job, dict):
        raise ValueError("strategy job notification payload has no job")
    status = str(job.get("status", ""))
    job_id = str(job.get("id", ""))
    tombstoned = bool(payload.get("tombstoned"))
    if tombstoned:
        return {
            "event_type": "strategy_job_deleted",
            "severity": NotificationSeverity.INFO,
            "title": "Activity no longer available",
            "body": "Activity no longer available.",
            "target_url": None,
            "actions": (),
        }
    if status == "failed":
        title = "Historical initialization failed"
        severity = NotificationSeverity.ERROR
    elif status == "cancelled":
        title = "Historical initialization cancelled"
        severity = NotificationSeverity.WARNING
    elif status == "complete":
        title = "Historical initialization complete"
        severity = NotificationSeverity.INFO
    elif status == "running":
        title = "Historical initialization in progress"
        severity = NotificationSeverity.INFO
    else:
        title = "Historical initialization queued"
        severity = NotificationSeverity.INFO
    detail = str(job.get("failure_detail") or "")
    body = f"Status: {status}."
    if detail:
        body = f"{body} {detail}"
    actions: tuple[str, ...]
    if status in {"queued", "running"}:
        actions = ("cancel",)
    elif status in {"failed", "cancelled"}:
        actions = ("restart", "delete")
    else:
        actions = ()
    return {
        "event_type": f"strategy_job_{status}",
        "severity": severity,
        "title": title,
        "body": body,
        "target_url": f"/strategy-manager?job_id={job_id}",
        "actions": actions,
    }


class StrategyNotificationProjector:
    """Project pending durable outbox records without becoming lifecycle authority."""

    def __init__(
        self, backtest_repository, notifications_repository: NotificationsRepository
    ):
        self._backtest = backtest_repository
        self._notifications = notifications_repository

    def project_pending(self) -> int:
        projected = 0
        for row in self._backtest.pending_notification_outbox():
            try:
                payload = row["payload"]
                if not isinstance(payload, dict):
                    raise ValueError("strategy job notification payload is invalid")
                details = _projection(payload)
                job = payload["job"]
                if not isinstance(job, dict):
                    raise ValueError("strategy job notification job is invalid")
                updated_at = datetime.fromisoformat(str(job["updated_at"]))
                self._notifications.upsert_job_notification(
                    job_id=str(row["job_id"]),
                    job_status_version=int(row["job_status_version"]),
                    category=(
                        NotificationCategory.BACKTEST
                        if str(job.get("job_type")) == "backtest"
                        else NotificationCategory.STRATEGY_INITIALIZATION
                    ),
                    event_type=str(details["event_type"]),
                    severity=cast(NotificationSeverity, details["severity"]),
                    title=str(details["title"]),
                    body=str(details["body"]),
                    created_at=updated_at,
                    target_url=cast(str | None, details["target_url"]),
                    # Action legality is evaluated against live repository
                    # state by later command routes; projections carry none.
                    actions=(),
                )
                if self._backtest.acknowledge_notification_outbox(
                    str(row["job_id"]), int(row["job_status_version"])
                ):
                    projected += 1
            except Exception:
                logger.exception(
                    "Unable to project Strategy Manager notification for job %s",
                    row.get("job_id"),
                )
        return projected


__all__ = ["StrategyNotificationProjector"]
