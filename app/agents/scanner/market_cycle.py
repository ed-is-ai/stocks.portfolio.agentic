"""Static FOMC calendar + coarse market-cycle phase (deterministic, no scraping).

v1 hardcodes known/scheduled FOMC meeting dates for 2025-2026. There is
deliberately no statement scraping or sentiment extraction here — that is a
later phase; this module only answers "where are we relative to the
calendar" so the deterministic narrative can say things like "N days to the
next FOMC decision" without any network access.

Update ``_FOMC_MEETINGS`` as the Federal Reserve publishes new meeting dates.
"""

from __future__ import annotations

from datetime import date

from app.schemas.market_cycle import FomcMeeting, MarketCycleContext

# Days before a meeting's start that count as the pre-FOMC "blackout" window
# for this coarse phase label (the Fed's actual quiet period is ~2 weeks
# before a meeting; we use a slightly narrower window here since v1 only
# needs a coarse signal, not a precise blackout boundary).
_BLACKOUT_DAYS = 7
# Days after a decision that still count as "just past" it.
_POST_DECISION_DAYS = 3

PHASE_PRE_FOMC_BLACKOUT = "pre_fomc_blackout"
PHASE_POST_FOMC = "post_fomc"
PHASE_MID_CYCLE = "mid_cycle"

_FOMC_MEETINGS: list[FomcMeeting] = [
    FomcMeeting(
        start_date=date(2025, 1, 28), end_date=date(2025, 1, 29), label="2025-01 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 3, 18), end_date=date(2025, 3, 19), label="2025-03 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 5, 6), end_date=date(2025, 5, 7), label="2025-05 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 6, 17), end_date=date(2025, 6, 18), label="2025-06 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 7, 29), end_date=date(2025, 7, 30), label="2025-07 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 9, 16), end_date=date(2025, 9, 17), label="2025-09 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 10, 28), end_date=date(2025, 10, 29), label="2025-10 FOMC"
    ),
    FomcMeeting(
        start_date=date(2025, 12, 9), end_date=date(2025, 12, 10), label="2025-12 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 1, 27), end_date=date(2026, 1, 28), label="2026-01 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 3, 17), end_date=date(2026, 3, 18), label="2026-03 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 4, 28), end_date=date(2026, 4, 29), label="2026-04 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 6, 16), end_date=date(2026, 6, 17), label="2026-06 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 7, 28), end_date=date(2026, 7, 29), label="2026-07 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 9, 15), end_date=date(2026, 9, 16), label="2026-09 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 10, 27), end_date=date(2026, 10, 28), label="2026-10 FOMC"
    ),
    FomcMeeting(
        start_date=date(2026, 12, 8), end_date=date(2026, 12, 9), label="2026-12 FOMC"
    ),
]


def _phase(
    days_since_last_decision: int | None, days_to_next_meeting: int | None
) -> str:
    """Return the coarse cycle-phase label for the given day offsets."""
    if days_to_next_meeting is not None and days_to_next_meeting <= _BLACKOUT_DAYS:
        return PHASE_PRE_FOMC_BLACKOUT
    if (
        days_since_last_decision is not None
        and days_since_last_decision <= _POST_DECISION_DAYS
    ):
        return PHASE_POST_FOMC
    return PHASE_MID_CYCLE


def get_market_cycle_context(today: date | None = None) -> MarketCycleContext:
    """Return the current position relative to the static FOMC calendar.

    *today* defaults to ``date.today()``; a caller may pass an explicit date
    for deterministic testing.
    """
    as_of = today or date.today()
    past_meetings = [m for m in _FOMC_MEETINGS if m.end_date <= as_of]
    future_meetings = [m for m in _FOMC_MEETINGS if m.start_date > as_of]

    last_meeting = max(past_meetings, key=lambda m: m.end_date, default=None)
    next_meeting = min(future_meetings, key=lambda m: m.start_date, default=None)

    days_since = (as_of - last_meeting.end_date).days if last_meeting else None
    days_to = (next_meeting.start_date - as_of).days if next_meeting else None

    return MarketCycleContext(
        as_of=as_of,
        last_decision_date=last_meeting.end_date if last_meeting else None,
        days_since_last_decision=days_since,
        next_meeting_start=next_meeting.start_date if next_meeting else None,
        days_to_next_meeting=days_to,
        phase=_phase(days_since, days_to),
    )
