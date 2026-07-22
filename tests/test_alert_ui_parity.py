"""Regression test for #58: on-screen and emailed alerts must agree.

Feeds a fixture watchlist through both the watchlist's alert-worthy
classification (``app.core.recommendation.classify_recommendation`` — the
same function the template renders — plus the VCP/MYB breakout badges) and
``AlertAgent.alert_trigger``/``should_alert`` (via
``app.core.alerting.classify_alert``), asserting the two surfaces agree on
what currently counts as alert-worthy.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agents.alert.alert_agent import AlertAgent
from app.core.alerting import classify_alert
from app.core.recommendation import classify_recommendation
from app.schemas import StockAnalysis, StockRecord, StockScan


def _record(
    ticker: str,
    *,
    stage: str = "Stage 2",
    entry_zone: str = "far",
    score: int = 5,
    volume_confirmed: bool = True,
    fresh_breakout: bool = False,
    multiyear_breakout: bool = False,
) -> StockRecord:
    """Build a StockRecord carrying an analysis, for parity fixtures."""
    scan = StockScan(
        ticker=ticker,
        as_of="2026-07-22",
        price=100.0,
        volume=1_000_000,
        rel_volume=1.2,
        high_52w=120.0,
        low_52w=80.0,
        pct_from_52w_high=-9.1,
        pct_change_week=2.5,
    )
    record = StockRecord.model_validate(scan.model_dump())
    record.analysis = StockAnalysis(
        score=score,
        stage=stage,
        entry_zone=entry_zone,
        volume_confirmed=volume_confirmed,
        fresh_breakout=fresh_breakout,
        multiyear_breakout=multiyear_breakout,
        summary="test",
    )
    return record


# Fixture watchlist spanning every recommendation bucket, with and without
# breakout flags layered on top, so both the "Stay Alert" bucket and the
# VCP/MYB badges are exercised.
FIXTURE_WATCHLIST = [
    _record("BUY", entry_zone="broken_out", score=8),
    _record("BUY_LOWVOL", entry_zone="broken_out", score=7, volume_confirmed=False),
    _record("FRESH_BREAKOUT", entry_zone="broken_out", score=8, fresh_breakout=True),
    _record("MYB", entry_zone="far", score=6, multiyear_breakout=True),
    _record(
        "FRESH_AND_MYB",
        entry_zone="broken_out",
        score=9,
        fresh_breakout=True,
        multiyear_breakout=True,
    ),
    _record("STAY_ALERT", entry_zone="approaching", score=5),
    _record("STAY_ALERT_HIGH", entry_zone="approaching", score=8),
    _record("APPROACHING_LOW_SCORE", entry_zone="approaching", score=4),
    _record("EXTENDED", entry_zone="extended", score=9),
    _record("GETTING_CLOSE", entry_zone="getting_close", score=6),
    _record("AVOID", stage="Stage 4", score=9),
    _record("WEAK", entry_zone="broken_out", score=3),
    _record("NONE", entry_zone="far", score=6),
]


def _ui_alert_worthy(record: StockRecord) -> bool:
    """What the watchlist page currently flags as alert-worthy.

    True if the Rec column shows "Stay Alert", or a VCP/multi-year-base
    badge is rendered next to the ticker.
    """
    assert record.analysis is not None
    rec = classify_recommendation(record)
    return rec.bucket == "alert" or (
        record.analysis.fresh_breakout or record.analysis.multiyear_breakout
    )


class TestAlertUiParity:
    """AlertAgent and the watchlist template must agree on alert-worthiness."""

    @pytest.mark.parametrize("record", FIXTURE_WATCHLIST, ids=lambda r: r.ticker)
    def test_classify_alert_matches_ui_recommendation(
        self, record: StockRecord
    ) -> None:
        ui_flagged = _ui_alert_worthy(record)
        email_eligible = classify_alert(record).email_eligible
        assert email_eligible == ui_flagged, (
            f"{record.ticker}: UI alert-worthy={ui_flagged} but "
            f"classify_alert.email_eligible={email_eligible}"
        )

    @pytest.mark.parametrize("record", FIXTURE_WATCHLIST, ids=lambda r: r.ticker)
    def test_alert_agent_agrees_with_ui(self, record: StockRecord) -> None:
        """AlertAgent.alert_trigger/should_alert must match the UI, given no
        cooldown/suppression in effect (isolating the criteria themselves)."""
        agent = AlertAgent()
        ui_flagged = _ui_alert_worthy(record)

        assert (agent.alert_trigger(record) is not None) == ui_flagged

        with patch.object(AlertAgent, "was_recently_alerted", return_value=False):
            assert agent.should_alert(record) == ui_flagged
