"""Notification-centre routes — the Today badge and read/dismiss (#80, GH-128).

The Today tab carries the polled badge: how many Today items need action.
The bell and its dropdown are gone: notifications reach the user as Today
items. Mutating endpoints (mark-read, mark-all-read,
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


#: Response header that makes Today's attention re-render after a dismiss.
DESK_REFRESH = {"HX-Trigger": "portfolio-agents-refresh, attention-changed"}


@router.get("/notifications/count", response_class=HTMLResponse)
def notifications_count(
    request: Request, desk: DeskDep, portfolio_id: str | None = None
) -> HTMLResponse:
    """Return the Today badge: ``badge_count`` for the portfolio (AD-5).

    Polled from the Today tab every 30s or slower (AD-2). Pending Strategy
    job notifications are projected first so Today's notification items stay
    current; a failure to count renders the badge's "?" (count unavailable)
    state. A plain ``def``: the count reads the ledger, scan artifact and
    price cache in the threadpool, cached per input revision by the Desk
    service, so a repeat poll with unchanged inputs never rebuilds.
    """
    try:
        get_strategy_notification_projector().project_pending()
    except sqlite3.OperationalError:
        pass
    try:
        count: int | None = desk.badge_count(optional_int(portfolio_id))
    except Exception:
        logger.warning("Badge count failed for the Today tab", exc_info=True)
        count = None
    return templates.TemplateResponse(request, "_notif_badge.html", {"count": count})


@router.post(
    "/notifications/{notification_id}/read",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
def mark_notification_read(
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
def mark_all_notifications_read(notifications: NotificationsDep) -> HTMLResponse:
    """Mark every notification read."""
    notifications.mark_all_read()
    return HTMLResponse(status_code=204)


@router.post(
    "/notifications/{notification_id}/dismiss",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
def dismiss_notification(
    notification_id: int, notifications: NotificationsDep
) -> HTMLResponse:
    """Dismiss one notification so it leaves Today."""
    notifications.dismiss(notification_id)
    # Attention views re-render on these events, so the notice leaves them.
    return HTMLResponse(status_code=204, headers=DESK_REFRESH)
