"""Notification-centre routes — the bell badge and read/dismiss (#80, GH-21).

The bell is a link to the AI Desk; its polled badge shows the Desk's urgent
attention count. The dropdown is gone (GH-21): notifications reach the user
as AI Desk queue items. Mutating endpoints (mark-read, mark-all-read,
dismiss) are kept, guarded by ``require_local_or_token`` like every other
state change in the app, and answer ``204 No Content``.
"""

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_desk_service,
    get_notifications_repository,
    get_strategy_notification_projector,
)
from app.api.params import optional_int
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.repositories.notifications_repo import NotificationsRepository
from app.services.desk_service import DeskService

router = APIRouter()
logger = logging.getLogger(__name__)

NotificationsDep = Annotated[
    NotificationsRepository, Depends(get_notifications_repository)
]
DeskDep = Annotated[DeskService, Depends(get_desk_service)]


@router.get("/notifications/count", response_class=HTMLResponse)
def notifications_count(
    request: Request, desk: DeskDep, portfolio_id: str | None = None
) -> HTMLResponse:
    """Return the bell badge: the AI Desk's urgent count for the portfolio.

    Polled by the bell. Pending Strategy job notifications are projected
    first so the Desk's notification items stay current; a failure to count
    renders the badge's "?" (count unavailable) state. A plain ``def``: the
    count reads the ledger, scan artifact and price cache in the threadpool,
    and is cached briefly per input revision by the Desk service.
    """
    try:
        get_strategy_notification_projector().project_pending()
    except sqlite3.OperationalError:
        pass
    try:
        count: int | None = desk.attention_count(optional_int(portfolio_id))
    except Exception:
        logger.warning("Attention count failed for the bell", exc_info=True)
        count = None
    return templates.TemplateResponse(request, "_notif_badge.html", {"count": count})


@router.post(
    "/notifications/{notification_id}/read",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def mark_notification_read(
    notification_id: int, notifications: NotificationsDep
) -> HTMLResponse:
    """Mark one notification read."""
    notifications.mark_read(notification_id)
    return HTMLResponse(status_code=204)


@router.post(
    "/notifications/read-all",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def mark_all_notifications_read(notifications: NotificationsDep) -> HTMLResponse:
    """Mark every notification read."""
    notifications.mark_all_read()
    return HTMLResponse(status_code=204)


@router.post(
    "/notifications/{notification_id}/dismiss",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def dismiss_notification(
    notification_id: int, notifications: NotificationsDep
) -> HTMLResponse:
    """Dismiss one notification so it leaves the AI Desk queue."""
    notifications.dismiss(notification_id)
    return HTMLResponse(status_code=204)
