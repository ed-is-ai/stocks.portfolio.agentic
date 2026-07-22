"""Server-side freshness calculation for the last usable analysis run.

Freshness must never be computed in Jinja (issue #42): the weekend/DST-aware
staleness boundary is business logic and belongs in a typed, unit-testable
helper.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.core.config import pipeline_stale_after_hours

NEW_YORK = ZoneInfo("America/New_York")


class FreshnessState(StrEnum):
    """The three states surfaced to the watchlist UI."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class Freshness(BaseModel):
    """Server-computed freshness for the last usable analysis run."""

    state: FreshnessState
    refreshed_at: datetime | None = None
    age_seconds: float | None = None
    stale_at: datetime | None = None
    diagnostic: str | None = None


def _weekend_adjusted_boundary(refreshed_at: datetime, boundary: datetime) -> datetime:
    """Push a stale boundary that would fall on a weekend to Monday 09:30 ET.

    Friday data gets weekend grace: if the ordinary threshold would land on
    Saturday or Sunday (or the refresh itself was on Friday and the plain
    threshold is short), the boundary becomes the next Monday market open
    instead, in US/Eastern local time (so DST transitions are handled by
    zoneinfo rather than a fixed UTC offset).
    """
    eastern = boundary.astimezone(NEW_YORK)
    refreshed_eastern = refreshed_at.astimezone(NEW_YORK)
    if refreshed_eastern.weekday() == 4:
        monday = refreshed_eastern.date() + timedelta(days=3)
        monday_open = datetime.combine(monday, time(9, 30), NEW_YORK)
        if eastern < monday_open:
            eastern = monday_open
    if eastern.weekday() == 5:
        monday = eastern.date() + timedelta(days=2)
        eastern = datetime.combine(monday, time(9, 30), NEW_YORK)
    elif eastern.weekday() == 6:
        monday = eastern.date() + timedelta(days=1)
        eastern = datetime.combine(monday, time(9, 30), NEW_YORK)
    return eastern.astimezone(timezone.utc)


def calculate_freshness(
    refreshed_at: datetime | None,
    *,
    now: datetime | None = None,
    stale_after_hours: float | None = None,
) -> Freshness:
    """Return the freshness state for a last-usable-run timestamp.

    A missing or naive timestamp returns ``unknown``. A future timestamp
    (clock skew) clamps age to zero and adds a diagnostic rather than
    reporting a negative age.
    """
    if refreshed_at is None:
        return Freshness(state=FreshnessState.UNKNOWN)
    if refreshed_at.tzinfo is None or refreshed_at.utcoffset() is None:
        return Freshness(
            state=FreshnessState.UNKNOWN,
            diagnostic="Refresh timestamp has no timezone.",
        )
    current = now or datetime.now(timezone.utc)
    hours = stale_after_hours or pipeline_stale_after_hours()
    stale_at = _weekend_adjusted_boundary(
        refreshed_at,
        refreshed_at.astimezone(timezone.utc) + timedelta(hours=hours),
    )
    raw_age = (current - refreshed_at).total_seconds()
    diagnostic = None
    if raw_age < 0:
        raw_age = 0
        diagnostic = "Refresh timestamp is in the future; age was clamped to zero."
    return Freshness(
        state=(FreshnessState.STALE if current >= stale_at else FreshnessState.FRESH),
        refreshed_at=refreshed_at,
        age_seconds=raw_age,
        stale_at=stale_at,
        diagnostic=diagnostic,
    )
