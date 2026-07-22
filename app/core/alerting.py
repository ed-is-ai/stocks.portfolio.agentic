"""Shared alert-eligibility rules for email and on-screen alerts (#58).

Single source of truth for "what counts as an alert" so the watchlist UI
and ``AlertAgent``'s email pipeline can never silently diverge again. Both
the web layer (via ``app.api.watchlist_context``) and ``AlertAgent`` import
``classify_alert`` instead of re-deriving these thresholds.

Product decision (#58): "Stay Alert" candidates (Stage 2, approaching entry,
score >= ``STAY_ALERT_SCORE_MIN``) are now emailable, not just shown on
screen — a user who added a ticker to their watchlist because it flashed
"Stay Alert" every day but never got an email had no way to know the system
considered that intentional. They are emailed at lower priority: they share
the same BUY/BREAKOUT section ordering by conviction, but use a much longer
cooldown than a genuine breakout, since a Stage 2 pullback can sit in
"approaching" for days and would otherwise re-email daily.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.recommendation import STAGE_2, STAY_ALERT_SCORE_MIN
from app.schemas import StockRecord

#: Cooldown for a genuine breakout/multi-year-base event: rare and urgent,
#: so a short window is enough to avoid duplicate emails for the same move.
BREAKOUT_COOLDOWN_HOURS = 24

#: Cooldown for a "Stay Alert" (approaching entry, score >= threshold)
#: candidate: these can persist on the watchlist for days, so a much longer
#: cooldown keeps the inbox from repeating the same idea every run.
STAY_ALERT_COOLDOWN_HOURS = 24 * 7


@dataclass(frozen=True)
class AlertClassification:
    """What a stock qualifies for, right now, per the shared alert rules.

    Attributes:
        trigger: breakout label (e.g. "VCP Breakout"), or None if the
            stock isn't in a fresh/multi-year breakout this run.
        is_stay_alert: True if the stock is a Stage 2 "approaching entry"
            candidate at or above ``STAY_ALERT_SCORE_MIN``.
        label: human-readable alert label — ``trigger`` if set, else a
            Stay Alert label, else None if not alert-worthy at all.
        email_eligible: True if ``label`` is set (ignoring cooldowns).
        cooldown_hours: cooldown that applies to this stock if emailed.
    """

    trigger: str | None
    is_stay_alert: bool
    label: str | None
    email_eligible: bool
    cooldown_hours: int


def _breakout_trigger(record: StockRecord) -> str | None:
    """Return the breakout trigger label for ``record``, or None."""
    a = record.analysis
    assert a is not None
    if a.fresh_breakout and a.multiyear_breakout:
        return "VCP + Multi-Year Base Breakout"
    if a.fresh_breakout:
        return "VCP Breakout"
    if a.multiyear_breakout:
        pivot = f"${a.multiyear_pivot:.2f}" if a.multiyear_pivot else ""
        return f"Multi-Year Base Breakout {pivot}"
    return None


def classify_alert(record: StockRecord) -> AlertClassification:
    """Classify whether ``record`` currently qualifies for an alert.

    Mirrors the watchlist's "Stay Alert" bucket (see
    ``app.core.recommendation.classify_recommendation``) plus the breakout
    events AlertAgent has always emailed, so the two surfaces agree on what
    is alert-worthy right now (independent of cooldowns/suppression).
    """
    a = record.analysis
    if a is None:
        return AlertClassification(
            trigger=None,
            is_stay_alert=False,
            label=None,
            email_eligible=False,
            cooldown_hours=BREAKOUT_COOLDOWN_HOURS,
        )

    trigger = _breakout_trigger(record)
    is_stay_alert = (
        a.stage == STAGE_2
        and a.entry_zone == "approaching"
        and a.score >= STAY_ALERT_SCORE_MIN
    )
    if trigger:
        label = trigger
    elif is_stay_alert:
        label = "Stay Alert — approaching entry zone"
    else:
        label = None

    return AlertClassification(
        trigger=trigger,
        is_stay_alert=is_stay_alert,
        label=label,
        email_eligible=label is not None,
        cooldown_hours=(
            BREAKOUT_COOLDOWN_HOURS if trigger else STAY_ALERT_COOLDOWN_HOURS
        ),
    )


def next_eligible_at(
    last_alerted_at: str | None, cooldown_hours: int
) -> datetime | None:
    """Return when the cooldown since ``last_alerted_at`` clears, or None."""
    if not last_alerted_at:
        return None
    return datetime.fromisoformat(last_alerted_at) + timedelta(hours=cooldown_hours)


def is_on_cooldown(
    last_alerted_at: str | None,
    cooldown_hours: int,
    now: datetime | None = None,
) -> bool:
    """Return True if the cooldown window since ``last_alerted_at`` hasn't
    elapsed yet."""
    eligible_at = next_eligible_at(last_alerted_at, cooldown_hours)
    if eligible_at is None:
        return False
    return (now or datetime.now(timezone.utc)) < eligible_at


@dataclass(frozen=True)
class AlertUiState:
    """Cooldown/suppression info shown on screen for one ticker (#58).

    Lets the watchlist explain a badge with no corresponding email instead
    of looking like a bug — e.g. "already alerted, next eligible at ...".
    """

    email_eligible: bool
    label: str | None
    suppressed: bool
    next_eligible_at: datetime | None


def build_alert_ui_state(
    record: StockRecord,
    *,
    has_watching: bool,
    last_alerted_at: str | None,
) -> AlertUiState:
    """Build the UI-facing suppression state for ``record``.

    ``has_watching``/``last_alerted_at`` come from ``AlertsRepository`` —
    this function stays free of any DB dependency so it can be unit tested
    and reused without wiring a connection.
    """
    classification = classify_alert(record)
    if not classification.email_eligible:
        return AlertUiState(False, None, False, None)

    suppressed = has_watching or is_on_cooldown(
        last_alerted_at, classification.cooldown_hours
    )
    eligible_at = (
        next_eligible_at(last_alerted_at, classification.cooldown_hours)
        if suppressed
        else None
    )
    return AlertUiState(
        email_eligible=True,
        label=classification.label,
        suppressed=suppressed,
        next_eligible_at=eligible_at,
    )
