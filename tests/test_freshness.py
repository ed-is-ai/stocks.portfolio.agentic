"""Acceptance contracts for last-usable-run freshness (#42)."""

from datetime import date, datetime, timedelta, timezone
import json

from app.core.config import pipeline_stale_after_hours
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceGapV1,
    build_analysis_payload,
    read_analysis_artifact,
    read_analysis_artifact_meta,
    read_analysis_records,
)
from app.schemas.pipeline_status import PipelineState
from app.services.freshness_service import FreshnessState, calculate_freshness


def test_freshness_threshold_and_future_clock_skew() -> None:
    refreshed = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)

    fresh = calculate_freshness(
        refreshed, now=refreshed + timedelta(hours=23, minutes=59), stale_after_hours=24
    )
    stale = calculate_freshness(
        refreshed, now=refreshed + timedelta(hours=24), stale_after_hours=24
    )
    future = calculate_freshness(
        refreshed, now=refreshed - timedelta(minutes=5), stale_after_hours=24
    )

    assert fresh.state is FreshnessState.FRESH
    assert stale.state is FreshnessState.STALE
    assert future.state is FreshnessState.FRESH
    assert future.age_seconds == 0
    assert future.age_display == "less than a minute ago"
    assert (
        future.diagnostic
        == "Refresh timestamp is in the future; age was clamped to zero."
    )


def test_friday_data_has_weekend_grace_across_dst() -> None:
    # 20:00 UTC is 16:00 EDT on this Friday; the ordinary 24h boundary is Saturday.
    refreshed = datetime(2026, 3, 6, 20, 0, tzinfo=timezone.utc)

    sunday = calculate_freshness(
        refreshed,
        now=datetime(2026, 3, 8, 18, 0, tzinfo=timezone.utc),
        stale_after_hours=24,
    )
    monday_open = calculate_freshness(
        refreshed,
        now=datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc),
        stale_after_hours=24,
    )

    assert sunday.state is FreshnessState.FRESH
    assert sunday.stale_at == datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)
    assert monday_open.state is FreshnessState.STALE


def test_short_friday_threshold_is_extended_to_monday_session() -> None:
    refreshed = datetime(2026, 7, 17, 19, 0, tzinfo=timezone.utc)

    weekend = calculate_freshness(
        refreshed,
        now=datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc),
        stale_after_hours=1,
    )
    monday_open = calculate_freshness(
        refreshed,
        now=datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc),
        stale_after_hours=1,
    )

    assert weekend.state is FreshnessState.FRESH
    assert weekend.stale_at == datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    assert monday_open.state is FreshnessState.STALE


def test_missing_refresh_is_unknown() -> None:
    result = calculate_freshness(None)

    assert result.state is FreshnessState.UNKNOWN
    assert result.refreshed_at is None
    assert result.age_display is None


def test_freshness_age_display_is_deterministic() -> None:
    refreshed = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

    assert (
        calculate_freshness(
            refreshed, now=refreshed + timedelta(minutes=1), stale_after_hours=48
        ).age_display
        == "1 minute ago"
    )
    assert (
        calculate_freshness(
            refreshed, now=refreshed + timedelta(hours=3), stale_after_hours=48
        ).age_display
        == "3 hours ago"
    )
    assert (
        calculate_freshness(
            refreshed, now=refreshed + timedelta(days=2), stale_after_hours=48
        ).age_display
        == "2 days ago"
    )


def test_naive_timestamp_is_unknown_with_diagnostic() -> None:
    result = calculate_freshness(datetime(2026, 1, 1, 12, 0))  # no tzinfo

    assert result.state is FreshnessState.UNKNOWN
    assert result.diagnostic == "Refresh timestamp has no timezone."


def test_invalid_stale_threshold_uses_default(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_STALE_AFTER_HOURS", "not-a-number")
    assert pipeline_stale_after_hours() == 24
    monkeypatch.setenv("PIPELINE_STALE_AFTER_HOURS", "0")
    assert pipeline_stale_after_hours() == 24
    monkeypatch.setenv("PIPELINE_STALE_AFTER_HOURS", "-5")
    assert pipeline_stale_after_hours() == 24


def test_valid_stale_threshold_is_used(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_STALE_AFTER_HOURS", "6")
    assert pipeline_stale_after_hours() == 6


# ---------------------------------------------------------------------------
# Artifact-ownership-derived freshness (the atomicity fix for #42)
# ---------------------------------------------------------------------------


def test_artifact_meta_roundtrips_and_drives_freshness(tmp_path) -> None:
    path = tmp_path / "analysis_results.json"
    generated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    payload = build_analysis_payload(
        [{"ticker": "AAPL"}], run_id="run-a", generated_at=generated_at
    )
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")

    meta = read_analysis_artifact_meta(path)
    assert meta is not None
    assert meta.run_id == "run-a"
    assert meta.generated_at == generated_at

    records = read_analysis_records(path)
    assert records == [{"ticker": "AAPL"}]

    freshness = calculate_freshness(meta.generated_at)
    assert freshness.state is FreshnessState.FRESH


def test_missing_or_legacy_artifact_meta_is_none(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    assert read_analysis_artifact_meta(missing) is None
    assert read_analysis_records(missing) == []

    legacy = tmp_path / "legacy.json"
    legacy.write_text('[{"ticker": "AAPL"}]', encoding="utf-8")
    assert read_analysis_artifact_meta(legacy) is None
    assert read_analysis_records(legacy) == [{"ticker": "AAPL"}]


def test_current_evidence_gap_roundtrips_in_one_artifact_snapshot(tmp_path) -> None:
    session = date(2026, 9, 3)
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-a",
        as_of_session=session,
        entries=(
            CurrentEvidenceGapV1(
                schema_version="current_scan_evidence_gap.v1",
                security_id="AAPL",
                as_of_session=session,
                reason="insufficient_history",
                detail="251 completed sessions; 252 required",
            ),
        ),
    )
    payload = build_analysis_payload(
        [{"ticker": "AAPL"}],
        run_id="run-a",
        generated_at=datetime.now(timezone.utc),
        current_evidence=evidence,
    )
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_analysis_artifact(path)
    assert loaded is not None
    assert loaded.current_evidence is not None
    assert loaded.current_evidence == evidence
    assert loaded.current_evidence.content_digest == evidence.content_digest


def test_tampered_current_evidence_digest_rejects_whole_envelope(tmp_path) -> None:
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-a",
        as_of_session=date(2026, 9, 3),
        entries=(),
    )
    payload = build_analysis_payload(
        [],
        run_id="run-a",
        generated_at=datetime.now(timezone.utc),
        current_evidence=evidence,
    )
    payload["current_evidence"]["content_digest"] = "0" * 64
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_analysis_artifact(path) is None


def test_tampered_current_evidence_does_not_blank_meta(tmp_path) -> None:
    """A corrupt evidence section must not make an otherwise-healthy run's
    ownership metadata unreadable -- every freshness consumer reads through
    ``read_analysis_artifact_meta``, not the full (stricter) artifact model.
    """
    evidence = CurrentAnalysisEvidenceV1.build(
        run_id="run-a",
        as_of_session=date(2026, 9, 3),
        entries=(),
    )
    generated_at = datetime.now(timezone.utc)
    payload = build_analysis_payload(
        [],
        run_id="run-a",
        generated_at=generated_at,
        current_evidence=evidence,
    )
    payload["current_evidence"]["content_digest"] = "0" * 64
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_analysis_artifact(path) is None
    meta = read_analysis_artifact_meta(path)
    assert meta is not None
    assert meta.run_id == "run-a"


def test_find_run_locates_current_and_archived_runs(tmp_path) -> None:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    repo.start(run_id="old")
    repo.finish(PipelineState.COMPLETE, expected_run_id="old", artifact_produced=True)
    repo.start(run_id="current")

    old_run = repo.find_run("old")
    current_run = repo.find_run("current")
    assert old_run is not None
    assert current_run is not None
    assert old_run.run_id == "old"
    assert current_run.run_id == "current"
    assert repo.find_run("nonexistent") is None


def test_malformed_history_does_not_crash_find_run(tmp_path) -> None:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    repo.history_path.write_text('{"not": "a list"}', encoding="utf-8")

    assert repo.find_run("anything") is None
