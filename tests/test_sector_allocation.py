"""Tests for app.agents.scanner.sector_allocation (#109 Phase 1)."""

from __future__ import annotations

import json

import app.agents.scanner.sector_allocation as sa
from app.schemas import Position, StockAnalysis, StockRecord, StockScan
from app.schemas.sector_allocation import SectorAllocationSnapshot, SectorShare


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(ticker: str, sector: str | None, score: int = 7) -> StockRecord:
    """Build a minimal StockRecord with a sector and analyst score."""
    rec = StockRecord.model_validate(
        StockScan(
            ticker=ticker,
            as_of="2024-01-01",
            price=50.0,
            volume=1_000_000,
            rel_volume=1.0,
            high_52w=120.0,
            low_52w=40.0,
            pct_from_52w_high=-10.0,
            pct_change_week=0.0,
            sector=sector,
        ).model_dump()
    )
    rec.analysis = StockAnalysis(score=score, stage="Stage 2", summary="")
    return rec


def _position(ticker: str, current_value: float, currency: str = "GBP") -> Position:
    return Position(
        ticker=ticker,
        shares=10,
        avg_cost=10.0,
        total_cost=100.0,
        current_value=current_value,
        price_currency=currency,
    )


# ---------------------------------------------------------------------------
# compute_sector_prevalence
# ---------------------------------------------------------------------------


class TestComputeSectorPrevalence:
    def test_empty_records_returns_zero_totals(self) -> None:
        snapshot = sa.compute_sector_prevalence([])
        assert snapshot.total_candidates == 0
        assert snapshot.shares == []

    def test_groups_and_shares_by_sector(self) -> None:
        records = [
            _record("A", "Technology"),
            _record("B", "Technology"),
            _record("C", "Healthcare"),
            _record("D", "Healthcare"),
        ]
        snapshot = sa.compute_sector_prevalence(records)
        assert snapshot.total_candidates == 4
        by_sector = {s.sector: s for s in snapshot.shares}
        assert by_sector["Technology"].count == 2
        assert by_sector["Technology"].count_share == 0.5
        assert by_sector["Healthcare"].count_share == 0.5

    def test_missing_sector_grouped_as_unknown(self) -> None:
        records = [_record("A", None), _record("B", "Technology")]
        snapshot = sa.compute_sector_prevalence(records)
        by_sector = {s.sector: s for s in snapshot.shares}
        assert "Unknown" in by_sector
        assert by_sector["Unknown"].count == 1

    def test_score_share_weights_by_analyst_score(self) -> None:
        records = [
            _record("A", "Technology", score=10),
            _record("B", "Healthcare", score=2),
        ]
        snapshot = sa.compute_sector_prevalence(records)
        by_sector = {s.sector: s for s in snapshot.shares}
        assert by_sector["Technology"].score_share == 10 / 12
        assert by_sector["Healthcare"].score_share == 2 / 12

    def test_deltas_empty_on_fresh_snapshot(self) -> None:
        snapshot = sa.compute_sector_prevalence([_record("A", "Technology")])
        assert snapshot.deltas == []


# ---------------------------------------------------------------------------
# compute_sector_deltas / with_deltas
# ---------------------------------------------------------------------------


class TestComputeSectorDeltas:
    def test_no_prior_returns_empty(self) -> None:
        current = sa.compute_sector_prevalence([_record("A", "Technology")])
        assert sa.compute_sector_deltas(current, None) == []

    def test_gaining_and_losing_sector(self) -> None:
        prior = SectorAllocationSnapshot(
            as_of="2024-01-01",
            total_candidates=2,
            shares=[
                SectorShare(
                    sector="Technology", count=1, count_share=0.5, score_share=0.5
                ),
                SectorShare(
                    sector="Healthcare", count=1, count_share=0.5, score_share=0.5
                ),
            ],
        )
        current = sa.compute_sector_prevalence(
            [
                _record("A", "Technology"),
                _record("B", "Technology"),
                _record("C", "Technology"),
                _record("D", "Healthcare"),
            ]
        )
        deltas = sa.compute_sector_deltas(current, prior)
        by_sector = {d.sector: d for d in deltas}
        assert by_sector["Technology"].delta > 0
        assert by_sector["Healthcare"].delta < 0

    def test_sector_absent_from_current_treated_as_zero(self) -> None:
        prior = SectorAllocationSnapshot(
            as_of="2024-01-01",
            total_candidates=1,
            shares=[
                SectorShare(sector="Energy", count=1, count_share=1.0, score_share=1.0)
            ],
        )
        current = sa.compute_sector_prevalence([_record("A", "Technology")])
        deltas = sa.compute_sector_deltas(current, prior)
        by_sector = {d.sector: d for d in deltas}
        assert by_sector["Energy"].current_share == 0.0
        assert by_sector["Energy"].delta == -1.0

    def test_with_deltas_populates_field(self) -> None:
        current = sa.compute_sector_prevalence([_record("A", "Technology")])
        result = sa.with_deltas(current, None)
        assert result.deltas == []
        assert result.as_of == current.as_of


# ---------------------------------------------------------------------------
# load/save sector history
# ---------------------------------------------------------------------------


class TestSectorHistoryPersistence:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sa, "_HISTORY_FILE", tmp_path / "nonexistent.json")
        assert sa.load_sector_history() == {}

    def test_round_trip_save_and_load(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        monkeypatch.setattr(sa, "_HISTORY_FILE", f)
        snapshot = sa.compute_sector_prevalence([_record("A", "Technology")])
        sa.save_sector_snapshot(snapshot, {})
        loaded = sa.load_sector_history()
        assert snapshot.as_of in loaded
        assert loaded[snapshot.as_of].total_candidates == 1

    def test_corrupt_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        f.write_text("not json", encoding="utf-8")
        monkeypatch.setattr(sa, "_HISTORY_FILE", f)
        assert sa.load_sector_history() == {}

    def test_invalid_snapshot_entry_skipped(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        f.write_text(json.dumps({"2024-01-01": {"bad": "data"}}), encoding="utf-8")
        monkeypatch.setattr(sa, "_HISTORY_FILE", f)
        assert sa.load_sector_history() == {}

    def test_caps_max_snapshots(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        monkeypatch.setattr(sa, "_HISTORY_FILE", f)
        monkeypatch.setattr(sa, "_MAX_SNAPSHOTS", 2)
        history: dict[str, SectorAllocationSnapshot] = {}
        for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
            snapshot = SectorAllocationSnapshot(as_of=day, total_candidates=0)
            sa.save_sector_snapshot(snapshot, history)
        loaded = sa.load_sector_history()
        assert len(loaded) == 2
        assert "2024-01-01" not in loaded

    def test_latest_snapshot_picks_max_date(self) -> None:
        older = SectorAllocationSnapshot(as_of="2024-01-01", total_candidates=1)
        newer = SectorAllocationSnapshot(as_of="2024-01-02", total_candidates=2)
        history = {older.as_of: older, newer.as_of: newer}
        assert sa.latest_snapshot(history) is newer

    def test_latest_snapshot_none_on_empty_history(self) -> None:
        assert sa.latest_snapshot({}) is None


# ---------------------------------------------------------------------------
# compute_portfolio_sector_weights
# ---------------------------------------------------------------------------


class TestComputePortfolioSectorWeights:
    def test_empty_positions_returns_empty(self) -> None:
        assert sa.compute_portfolio_sector_weights([], {}, 1.35) == []

    def test_groups_by_sector_with_gbp_value_share(self) -> None:
        positions = [
            _position("A", 100.0, "GBP"),
            _position("B", 100.0, "GBP"),
            _position("C", 50.0, "GBP"),
        ]
        ticker_sector = {"A": "Technology", "B": "Technology", "C": "Energy"}
        weights = sa.compute_portfolio_sector_weights(positions, ticker_sector, 1.35)
        by_sector = {w.sector: w for w in weights}
        assert by_sector["Technology"].count == 2
        assert by_sector["Technology"].value_share == 200 / 250
        assert by_sector["Energy"].value_share == 50 / 250

    def test_usd_positions_converted_to_gbp(self) -> None:
        positions = [
            _position("A", 135.0, "USD"),  # -> 100 GBP at 1.35
            _position("B", 100.0, "GBP"),
        ]
        ticker_sector = {"A": "Technology", "B": "Healthcare"}
        weights = sa.compute_portfolio_sector_weights(positions, ticker_sector, 1.35)
        by_sector = {w.sector: w for w in weights}
        assert by_sector["Technology"].value_share == 0.5
        assert by_sector["Healthcare"].value_share == 0.5

    def test_unknown_sector_falls_back(self) -> None:
        positions = [_position("Z", 100.0, "GBP")]
        weights = sa.compute_portfolio_sector_weights(positions, {}, 1.35)
        assert weights[0].sector == "Unknown"

    def test_unpriced_position_skipped(self) -> None:
        pos = Position(
            ticker="A",
            shares=10,
            avg_cost=10.0,
            total_cost=100.0,
            current_value=None,
        )
        weights = sa.compute_portfolio_sector_weights([pos], {"A": "Technology"}, 1.35)
        assert weights == []


# ---------------------------------------------------------------------------
# high-conviction (7/10+) universe share + multi-year breakout counts
# ---------------------------------------------------------------------------


class TestStrongAndMybCounts:
    def test_strong_share_is_fraction_of_whole_universe(self) -> None:
        records = [
            _record("A", "Technology", score=8),
            _record("B", "Technology", score=5),
            _record("C", "Technology", score=7),
            _record("D", "Healthcare", score=9),
        ]
        by_sector = {s.sector: s for s in sa.compute_sector_prevalence(records).shares}
        # Tech: 2 of 4 records are >= 7/10 -> 0.5 of the whole universe.
        assert by_sector["Technology"].strong_count == 2
        assert by_sector["Technology"].strong_share == 0.5
        assert by_sector["Healthcare"].strong_count == 1
        assert by_sector["Healthcare"].strong_share == 0.25

    def test_myb_count_tallies_multiyear_breakouts(self) -> None:
        a = _record("A", "Energy", score=8)
        a.analysis = StockAnalysis(
            score=8, stage="Stage 2", summary="", multiyear_breakout=True
        )
        b = _record("B", "Energy", score=8)
        by_sector = {s.sector: s for s in sa.compute_sector_prevalence([a, b]).shares}
        assert by_sector["Energy"].myb_count == 1


# ---------------------------------------------------------------------------
# week-on-week baseline + deltas
# ---------------------------------------------------------------------------


def _dated_snapshot(as_of: str) -> SectorAllocationSnapshot:
    return SectorAllocationSnapshot(
        as_of=as_of,
        total_candidates=2,
        shares=[
            SectorShare(sector="Technology", count=1, count_share=0.5, score_share=0.5)
        ],
    )


class TestWeekBaseline:
    def test_picks_snapshot_nearest_seven_days_ago(self) -> None:
        history = {
            "2026-07-01": _dated_snapshot("2026-07-01"),  # 26d before -> far
            "2026-07-19": _dated_snapshot("2026-07-19"),  # 8d before -> nearest ~7
        }
        baseline, lookback = sa.week_baseline(history, "2026-07-27")
        assert baseline is not None
        assert baseline.as_of == "2026-07-19"
        assert lookback == 8

    def test_excludes_current_and_future_dates(self) -> None:
        history = {
            "2026-07-27": _dated_snapshot("2026-07-27"),  # same day -> excluded
            "2026-07-30": _dated_snapshot("2026-07-30"),  # future -> excluded
        }
        baseline, lookback = sa.week_baseline(history, "2026-07-27")
        assert baseline is None
        assert lookback is None

    def test_with_week_deltas_sets_lookback_days(self) -> None:
        current = _dated_snapshot("2026-07-27")
        history = {"2026-07-20": _dated_snapshot("2026-07-20")}
        result = sa.with_week_deltas(current, history)
        assert result.lookback_days == 7
        assert result.deltas != []


# ---------------------------------------------------------------------------
# top congressional / senate net buys
# ---------------------------------------------------------------------------


def _congress_record(
    ticker: str,
    sector: str,
    congress_buys: int,
    congress_sells: int = 0,
    senate_buys: int = 0,
    senate_sells: int = 0,
) -> StockRecord:
    rec = _record(ticker, sector)
    rec.congress_buys = congress_buys
    rec.congress_sells = congress_sells
    rec.senate_buys = senate_buys
    rec.senate_sells = senate_sells
    return rec


class TestTopCongressionalBuys:
    def test_ranks_by_net_and_excludes_non_positive(self) -> None:
        records = [
            _congress_record("AAA", "Technology", congress_buys=10, congress_sells=2),
            _congress_record("BBB", "Energy", congress_buys=5),
            _congress_record("CCC", "Healthcare", congress_buys=1, congress_sells=4),
        ]
        rows = sa.top_congressional_buys(records)
        tickers = [r.ticker for r in rows]
        assert tickers == ["AAA", "BBB"]  # CCC net -3 excluded
        assert rows[0].congress_net == 8

    def test_limit_caps_results(self) -> None:
        records = [
            _congress_record(f"T{i}", "Technology", congress_buys=i + 1)
            for i in range(6)
        ]
        rows = sa.top_congressional_buys(records, limit=3)
        assert len(rows) == 3
