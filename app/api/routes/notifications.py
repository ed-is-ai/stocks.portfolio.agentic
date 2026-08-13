"""Notification-centre routes — the bell badge, dropdown, and read/dismiss.

Read endpoints render the badge (polled) and the dropdown list. Mutating
endpoints (mark-read, mark-all-read, dismiss) are guarded by
``require_local_or_token`` like every other state change in the app.
"""

from typing import Annotated
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_notifications_repository,
    get_strategy_notification_projector,
)
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.repositories.notifications_repo import NotificationsRepository

router = APIRouter()

NotificationsDep = Annotated[
    NotificationsRepository, Depends(get_notifications_repository)
]

#: Cap on how many items the dropdown lists before "older items pruned".
DROPDOWN_LIMIT = 15


def _badge(request: Request, repo: NotificationsRepository) -> HTMLResponse:
    """Render the standalone bell badge (used by polling and OOB updates)."""
    return templates.TemplateResponse(
        request,
        "_notif_badge.html",
        {"unread_count": repo.unread_count(), "oob": False},
    )


def _panel(request: Request, repo: NotificationsRepository) -> HTMLResponse:
    """Render the dropdown panel plus an out-of-band badge refresh."""
    return templates.TemplateResponse(
        request,
        "_notifications.html",
        {
            "notifications": repo.recent(limit=DROPDOWN_LIMIT),
            "unread_count": repo.unread_count(),
        },
    )


@router.get("/notifications/count", response_class=HTMLResponse)
async def notifications_count(
    request: Request, notifications: NotificationsDep
) -> HTMLResponse:
    """Return just the unread badge — polled by the bell every few seconds."""
    try:
        get_strategy_notification_projector().project_pending()
    except sqlite3.OperationalError:
        pass
    return _badge(request, notifications)


@router.get("/partials/notifications", response_class=HTMLResponse)
async def partial_notifications(
    request: Request, notifications: NotificationsDep
) -> HTMLResponse:
    """Return the dropdown list of recent notifications."""
    try:
        get_strategy_notification_projector().project_pending()
    except sqlite3.OperationalError:
        pass
    return _panel(request, notifications)


@router.post(
    "/notifications/{notification_id}/read",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def mark_notification_read(
    request: Request, notification_id: int, notifications: NotificationsDep
) -> HTMLResponse:
    """Mark one notification read and return the refreshed panel."""
    notifications.mark_read(notification_id)
    return _panel(request, notifications)


@router.post(
    "/notifications/read-all",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def mark_all_notifications_read(
    request: Request, notifications: NotificationsDep
) -> HTMLResponse:
    """Mark every notification read and return the refreshed panel."""
    notifications.mark_all_read()
    return _panel(request, notifications)


@router.post(
    "/notifications/{notification_id}/dismiss",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def dismiss_notification(
    request: Request, notification_id: int, notifications: NotificationsDep
) -> HTMLResponse:
    """Dismiss one notification and return the refreshed panel."""
    notifications.dismiss(notification_id)
    return _panel(request, notifications)
