"""Tests for app.agents.scanner.scan_history transition-detection logic."""

from __future__ import annotations

import datetime
import json

import app.agents.scanner.scan_history as sh
from app.schemas import StockAnalysis, StockRecord, StockScan
from app.schemas.analysis_artifact import build_analysis_payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(ticker: str, zone: str | None) -> StockRecord:
    """Build a minimal StockRecord with an optional entry_zone."""
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
        ).model_dump()
    )
    if zone is not None:
        rec.analysis = StockAnalysis(
            score=7, stage="Stage 2", entry_zone=zone, summary=""
        )
    return rec


# ---------------------------------------------------------------------------
# get_new_tickers
# ---------------------------------------------------------------------------


class TestGetNewTickers:
    def test_no_history_all_current_are_new(self) -> None:
        result = sh.get_new_tickers(["A", "B"], {})
        assert result == {"A", "B"}

    def test_empty_current_no_new(self) -> None:
        history = {"2024-01-01": {"A": "broken_out"}}
        result = sh.get_new_tickers([], history)
        assert result == set()

    def test_some_seen_some_new(self) -> None:
        history = {"2024-01-01": {"A": "broken_out"}}
        result = sh.get_new_tickers(["A", "B"], history)
        assert result == {"B"}

    def test_uses_latest_snapshot_not_older_ones(self) -> None:
        """Older snapshot contains B; newer does not → B counts as new."""
        history = {
            "2024-01-01": {"A": "broken_out", "B": "approaching"},
            "2024-01-02": {"A": "broken_out"},
        }
        result = sh.get_new_tickers(["A", "B"], history)
        assert result == {"B"}

    def test_all_seen_none_new(self) -> None:
        history = {"2024-01-01": {"A": "", "B": "approaching"}}
        result = sh.get_new_tickers(["A", "B"], history)
        assert result == set()

    def test_zone_value_irrelevant_for_new_check(self) -> None:
        """A ticker with empty-string zone in history is still 'seen'."""
        history = {"2024-01-01": {"A": ""}}
        result = sh.get_new_tickers(["A"], history)
        assert result == set()


# ---------------------------------------------------------------------------
# get_fresh_breakouts
# ---------------------------------------------------------------------------


class TestGetFreshBreakouts:
    def test_no_history_returns_all_broken_out(self) -> None:
        records = [_record("A", "broken_out"), _record("B", "approaching")]
        result = sh.get_fresh_breakouts(records, {})
        assert result == {"A"}

    def test_already_broken_out_not_fresh(self) -> None:
        history = {"2024-01-01": {"A": "broken_out"}}
        records = [_record("A", "broken_out")]
        result = sh.get_fresh_breakouts(records, history)
        assert result == set()

    def test_transitioned_into_breakout_is_fresh(self) -> None:
        history = {"2024-01-01": {"A": "approaching"}}
        records = [_record("A", "broken_out")]
        result = sh.get_fresh_breakouts(records, history)
        assert result == {"A"}

    def test_absent_last_run_broken_out_now_is_fresh(self) -> None:
        history = {"2024-01-01": {"X": "broken_out"}}
        records = [_record("A", "broken_out")]
        result = sh.get_fresh_breakouts(records, history)
        assert result == {"A"}

    def test_no_analysis_record_ignored(self) -> None:
        records = [_record("C", None)]
        result = sh.get_fresh_breakouts(records, {})
        assert result == set()

    def test_uses_latest_snapshot_for_comparison(self) -> None:
        """Older snapshot shows approaching; latest shows broken_out → not fresh."""
        history = {
            "2024-01-01": {"A": "approaching"},
            "2024-01-02": {"A": "broken_out"},
        }
        records = [_record("A", "broken_out")]
        result = sh.get_fresh_breakouts(records, history)
        assert result == set()


# ---------------------------------------------------------------------------
# apply_fresh_breakout_flags
# ---------------------------------------------------------------------------


class TestApplyFreshBreakoutFlags:
    def test_flags_only_breakout_tickers(self) -> None:
        a, b = _record("A", "broken_out"), _record("B", "approaching")
        sh.apply_fresh_breakout_flags([a, b], {"A"})
        assert a.analysis is not None and a.analysis.fresh_breakout is True
        assert b.analysis is not None and b.analysis.fresh_breakout is False

    def test_record_without_analysis_is_skipped(self) -> None:
        rec = _record("C", None)
        sh.apply_fresh_breakout_flags([rec], {"C"})
        assert rec.analysis is None

    def test_flag_survives_into_serialised_payload(self) -> None:
        """Regression (#131): the published JSON must carry fresh_breakout.

        The bug serialised the analysis payload *before* the flags were
        assigned, so the web dashboard always read fresh_breakout=false even
        when a breakout was detected. Assert the flag reaches the payload that
        os.replace promotes.
        """
        records = [_record("A", "broken_out"), _record("B", "approaching")]
        breakouts = sh.get_fresh_breakouts(records, {})
        sh.apply_fresh_breakout_flags(records, breakouts)

        payload = build_analysis_payload(
            [r.model_dump(mode="json") for r in records],
            run_id="run-1",
            generated_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        )
        by_ticker = {r["ticker"]: r for r in payload["records"]}
        assert by_ticker["A"]["analysis"]["fresh_breakout"] is True
        assert by_ticker["B"]["analysis"]["fresh_breakout"] is False


# ---------------------------------------------------------------------------
# load_history
# ---------------------------------------------------------------------------


class TestLoadHistory:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sh, "_HISTORY_FILE", tmp_path / "nonexistent.json")
        assert sh.load_history() == {}

    def test_migrates_old_list_format(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        f.write_text('{"2024-01-01": ["A", "B"]}', encoding="utf-8")
        monkeypatch.setattr(sh, "_HISTORY_FILE", f)
        assert sh.load_history() == {"2024-01-01": {"A": "", "B": ""}}

    def test_new_dict_format_preserved(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        f.write_text('{"2024-01-01": {"A": "broken_out"}}', encoding="utf-8")
        monkeypatch.setattr(sh, "_HISTORY_FILE", f)
        assert sh.load_history() == {"2024-01-01": {"A": "broken_out"}}

    def test_mixed_old_and_new_format(self, tmp_path, monkeypatch) -> None:
        """A file can have some dates as lists (old) and some as dicts (new)."""
        data = {"2024-01-01": ["X"], "2024-01-02": {"Y": "approaching"}}
        f = tmp_path / "history.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(sh, "_HISTORY_FILE", f)
        result = sh.load_history()
        assert result == {"2024-01-01": {"X": ""}, "2024-01-02": {"Y": "approaching"}}


# ---------------------------------------------------------------------------
# save_history
# ---------------------------------------------------------------------------


class TestSaveHistory:
    def test_round_trip_creates_today_entry(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        monkeypatch.setattr(sh, "_HISTORY_FILE", f)
        records = [_record("A", "broken_out")]
        sh.save_history(records, {})
        loaded = sh.load_history()
        today = datetime.date.today().isoformat()
        assert today in loaded
        assert loaded[today] == {"A": "broken_out"}

    def test_save_record_without_analysis_writes_empty_zone(
        self, tmp_path, monkeypatch
    ) -> None:
        f = tmp_path / "history.json"
        monkeypatch.setattr(sh, "_HISTORY_FILE", f)
        records = [_record("B", None)]
        sh.save_history(records, {})
        loaded = sh.load_history()
        today = datetime.date.today().isoformat()
        assert loaded[today]["B"] == ""

    def test_save_appends_to_existing_history(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "history.json"
        monkeypatch.setattr(sh, "_HISTORY_FILE", f)
        history = {"2024-01-01": {"X": "approaching"}}
        sh.save_history([_record("A", "broken_out")], history)
        loaded = sh.load_history()
        assert "2024-01-01" in loaded
        today = datetime.date.today().isoformat()
        assert today in loaded
