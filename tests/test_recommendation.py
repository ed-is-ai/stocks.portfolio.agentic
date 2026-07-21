"""Tests for watchlist actionability ranking and ordering."""

from app.core.recommendation import actionability_rank, actionability_sort_key
from app.schemas import StockAnalysis, StockRecord, StockScan


def _record(
    ticker: str,
    *,
    stage: str = "Stage 2",
    entry_zone: str = "far",
    score: int = 5,
    with_analysis: bool = True,
) -> StockRecord:
    """Build a StockRecord (optionally carrying an analysis) for ranking tests."""
    scan = StockScan(
        ticker=ticker,
        as_of="2026-07-21",
        price=100.0,
        volume=1_000_000,
        rel_volume=1.0,
        high_52w=120.0,
        low_52w=80.0,
        pct_from_52w_high=-9.1,
        pct_change_week=2.5,
    )
    record = StockRecord.model_validate(scan.model_dump())
    if with_analysis:
        record.analysis = StockAnalysis(
            score=score,
            stage=stage,
            entry_zone=entry_zone,
            summary="test",
        )
    return record


class TestActionabilityRank:
    def test_buy_outranks_stay_alert_and_extended(self) -> None:
        buy = _record("BUY", entry_zone="broken_out", score=8)
        alert = _record("ALERT", entry_zone="approaching", score=6)
        extended = _record("EXT", entry_zone="extended", score=9)

        assert actionability_rank(buy) > actionability_rank(alert)
        assert actionability_rank(alert) > actionability_rank(extended)

    def test_stage_four_and_low_score_rank_lowest(self) -> None:
        late = _record("LATE", stage="Stage 4", score=9)
        weak = _record("WEAK", stage="Stage 2", entry_zone="broken_out", score=3)

        assert actionability_rank(late) == 0
        assert actionability_rank(weak) == 0

    def test_missing_analysis_ranks_zero(self) -> None:
        record = _record("NONE", with_analysis=False)
        assert actionability_rank(record) == 0


class TestActionabilitySort:
    def test_actionable_first_beats_higher_score(self) -> None:
        """A Buy setup sorts above an Extended row with a higher raw score."""
        extended = _record("EXT", entry_zone="extended", score=9)
        buy = _record("BUY", entry_zone="broken_out", score=7)

        ordered = sorted([extended, buy], key=actionability_sort_key, reverse=True)

        assert [r.ticker for r in ordered] == ["BUY", "EXT"]

    def test_ties_break_by_score(self) -> None:
        low = _record("LOW", entry_zone="approaching", score=5)
        high = _record("HIGH", entry_zone="approaching", score=8)

        ordered = sorted([low, high], key=actionability_sort_key, reverse=True)

        assert [r.ticker for r in ordered] == ["HIGH", "LOW"]
