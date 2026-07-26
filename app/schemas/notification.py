"""Schema for in-app notifications surfaced by the notification centre (#80).

One row per user-facing event: an alert email that was actually sent, a
completed data-refresh run, or a source-health failure during a run. The
repository (``app.repositories.notifications_repo``) owns the table; this
module owns the shape and the small amount of derived presentation logic
(icon, deep-link target) the dropdown template needs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class NotificationCategory(StrEnum):
    """Broad grouping used for filtering and deep-link routing."""

    ALERT = "alert"
    REFRESH = "refresh"
    SOURCE = "source"


class NotificationSeverity(StrEnum):
    """Drives the item's icon and colour in the dropdown."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


#: Tab (element id in ``index.html``) each category deep-links to on click.
_DEEP_LINK_TABS: dict[NotificationCategory, str] = {
    NotificationCategory.ALERT: "tab-watchlist",
    NotificationCategory.REFRESH: "tab-runlog",
    NotificationCategory.SOURCE: "tab-runlog",
}

#: bootstrap-icons glyph per severity.
_SEVERITY_ICONS: dict[NotificationSeverity, str] = {
    NotificationSeverity.INFO: "bi-info-circle",
    NotificationSeverity.WARNING: "bi-exclamation-triangle",
    NotificationSeverity.ERROR: "bi-x-octagon",
}


class Notification(BaseModel):
    """One persisted, user-facing notification."""

    id: int | None = None
    category: NotificationCategory
    event_type: str
    severity: NotificationSeverity = NotificationSeverity.INFO
    title: str
    body: str = ""
    ticker: str | None = None
    run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    read_at: datetime | None = None
    dismissed_at: datetime | None = None

    @property
    def is_read(self) -> bool:
        """Return whether the user has seen this notification."""
        return self.read_at is not None

    @property
    def deep_link_tab(self) -> str:
        """Return the tab element id this notification navigates to."""
        return _DEEP_LINK_TABS[self.category]

    @property
    def icon(self) -> str:
        """Return the bootstrap-icons glyph for this notification's severity."""
        return _SEVERITY_ICONS[self.severity]
