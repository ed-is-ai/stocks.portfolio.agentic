"""Static FOMC calendar + coarse market-cycle phase schemas."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class FomcMeeting(BaseModel):
    """One scheduled FOMC meeting (two-day, decision announced on end_date)."""

    start_date: date
    end_date: date
    label: str


class MarketCycleContext(BaseModel):
    """Where "today" sits relative to the static FOMC meeting calendar.

    ``phase`` is a coarse label: "pre_fomc_blackout" (within the blackout
    window before the next meeting), "post_fomc" (just past a decision), or
    "mid_cycle" (otherwise). No statement scraping — v1 is calendar-only.
    """

    as_of: date
    last_decision_date: date | None
    days_since_last_decision: int | None
    next_meeting_start: date | None
    days_to_next_meeting: int | None
    phase: str
