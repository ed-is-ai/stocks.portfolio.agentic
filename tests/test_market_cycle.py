"""Tests for app.agents.scanner.market_cycle (#109 Phase 1, static calendar)."""

from __future__ import annotations

from datetime import date

from app.agents.scanner.market_cycle import (
    PHASE_MID_CYCLE,
    PHASE_POST_FOMC,
    PHASE_PRE_FOMC_BLACKOUT,
    get_market_cycle_context,
)


class TestGetMarketCycleContext:
    def test_mid_cycle_between_meetings(self) -> None:
        # 2026-01-28 was a meeting end date; 2026-03-17 is the next start.
        # Mid-Feb sits comfortably between the two.
        ctx = get_market_cycle_context(date(2026, 2, 15))
        assert ctx.phase == PHASE_MID_CYCLE
        assert ctx.last_decision_date == date(2026, 1, 28)
        assert ctx.next_meeting_start == date(2026, 3, 17)
        assert ctx.days_since_last_decision == 18
        assert ctx.days_to_next_meeting == 30

    def test_post_fomc_within_window(self) -> None:
        ctx = get_market_cycle_context(date(2026, 1, 30))  # 2d after 2026-01-28
        assert ctx.phase == PHASE_POST_FOMC
        assert ctx.days_since_last_decision == 2

    def test_pre_fomc_blackout_within_window(self) -> None:
        # 2026-07-28/29 is a scheduled meeting; 2 days before start.
        ctx = get_market_cycle_context(date(2026, 7, 26))
        assert ctx.phase == PHASE_PRE_FOMC_BLACKOUT
        assert ctx.days_to_next_meeting == 2

    def test_today_default_used_when_no_date_given(self) -> None:
        ctx = get_market_cycle_context()
        assert ctx.as_of == date.today()

    def test_before_first_meeting_has_no_last_decision(self) -> None:
        ctx = get_market_cycle_context(date(2020, 1, 1))
        assert ctx.last_decision_date is None
        assert ctx.days_since_last_decision is None
        assert ctx.next_meeting_start is not None

    def test_after_last_meeting_has_no_next_meeting(self) -> None:
        ctx = get_market_cycle_context(date(2030, 1, 1))
        assert ctx.next_meeting_start is None
        assert ctx.days_to_next_meeting is None
        assert ctx.last_decision_date is not None
