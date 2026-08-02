"""Acceptance contracts for explicit external-source coverage (#40)."""

from datetime import datetime, timezone

import pytest

from app.agents.extraction.extraction_agent import ExtractionAgent
from app.agents.scanner import scanner_agent
from app.orchestration import orchestrator
from app.schemas.pipeline_status import PipelineState
from app.schemas.source_health import (
    SourceHealth,
    SourceName,
    SourceResult,
    SourceStage,
    SourceState,
    derive_pipeline_outcome,
)


def test_source_result_distinguishes_empty_skipped_and_failed() -> None:
    now = datetime.now(timezone.utc)

    empty = SourceResult.from_items(SourceName.TRADINGVIEW_US, [], started_at=now)
    skipped = SourceResult.unavailable(
        SourceName.VCP_FMP,
        SourceState.SKIPPED,
        "missing_configuration",
        "FMP API key is not configured.",
        started_at=now,
    )
    failed = SourceResult.unavailable(
        SourceName.TRADINGVIEW_UK,
        SourceState.FAILED,
        "provider_failure",
        "TradingView UK did not respond.",
        started_at=now,
    )

    assert empty.health.state is SourceState.EMPTY
    assert empty.health.count == 0
    assert skipped.health.state is SourceState.SKIPPED
    assert skipped.health.count == 0
    assert failed.health.state is SourceState.FAILED
    assert failed.health.count == 0
    assert all(
        item.health.completed_at is not None for item in (empty, skipped, failed)
    )


def test_every_source_maps_to_a_pipeline_stage() -> None:
    """Every SourceName must map to exactly one funnel stage (#108)."""
    expected = {
        SourceName.WHALE_WISDOM: SourceStage.DISCOVERY,
        SourceName.STOCKTWITS: SourceStage.DISCOVERY,
        SourceName.VCP_FMP: SourceStage.DISCOVERY,
        SourceName.TRADINGVIEW_US: SourceStage.DISCOVERY,
        SourceName.TRADINGVIEW_UK: SourceStage.DISCOVERY,
        SourceName.YAHOO_MARKET_DATA: SourceStage.MARKET_DATA,
        SourceName.ALPHA_VANTAGE: SourceStage.ENRICHMENT,
        SourceName.CONGRESS: SourceStage.ENRICHMENT,
    }

    assert set(expected) == set(SourceName)
    for source, stage in expected.items():
        assert source.stage is stage


def test_stage_labels_and_order_reflect_the_pipeline_funnel() -> None:
    assert SourceStage.DISCOVERY.label == "Discovery"
    assert SourceStage.MARKET_DATA.label == "Market data"
    assert SourceStage.ENRICHMENT.label == "Enrichment"
    assert (
        SourceStage.DISCOVERY.order
        < SourceStage.MARKET_DATA.order
        < SourceStage.ENRICHMENT.order
    )


def test_source_health_stage_properties_delegate_to_source() -> None:
    health = SourceHealth(source=SourceName.CONGRESS, state=SourceState.OK, count=5)

    assert health.stage is SourceStage.ENRICHMENT
    assert health.stage_label == "Enrichment"
    assert health.stage_order == SourceStage.ENRICHMENT.order


def test_source_health_redacts_secrets_from_serialized_messages() -> None:
    health = SourceHealth(
        source=SourceName.ALPHA_VANTAGE,
        state=SourceState.FAILED,
        count=0,
        detail_code="provider_failure",
        display_message="Request failed with apikey=super-secret and token abc123",
    )

    serialized = health.model_dump_json()

    assert "super-secret" not in serialized
    assert "abc123" not in serialized
    assert "credentials redacted" in serialized


def test_overall_outcome_uses_usable_results_and_coverage() -> None:
    healthy = {
        SourceName.TRADINGVIEW_US: SourceHealth(
            source=SourceName.TRADINGVIEW_US, state=SourceState.OK, count=3
        )
    }
    reduced = {
        **healthy,
        SourceName.VCP_FMP: SourceHealth(
            source=SourceName.VCP_FMP,
            state=SourceState.SKIPPED,
            count=0,
            detail_code="missing_configuration",
            display_message="FMP API key is not configured.",
        ),
    }

    assert (
        derive_pipeline_outcome(healthy, analysed=3, artifact_produced=True)
        is PipelineState.COMPLETE
    )
    assert (
        derive_pipeline_outcome(reduced, analysed=3, artifact_produced=True)
        is PipelineState.PARTIAL
    )
    assert (
        derive_pipeline_outcome(reduced, analysed=0, artifact_produced=False)
        is PipelineState.FAILED
    )
    assert (
        derive_pipeline_outcome(healthy, analysed=3, artifact_produced=False)
        is PipelineState.FAILED
    )


def test_cached_extraction_inputs_are_not_reported_as_freshly_ok() -> None:
    health = orchestrator._cached_extraction_health(
        {"AAPL": (True, False), "MSFT": (False, True)}
    )

    assert health[SourceName.STOCKTWITS].state is SourceState.SKIPPED
    assert health[SourceName.STOCKTWITS].count == 1
    assert health[SourceName.WHALE_WISDOM].state is SourceState.SKIPPED
    assert health[SourceName.WHALE_WISDOM].detail_code == "cached_input"


def test_legacy_run_log_is_migrated_with_structured_health(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "runs.csv"
    path.write_text("start,status,sources\n2026-01-01T00:00:00+00:00,ok,legacy\n")
    monkeypatch.setattr(orchestrator, "RUN_LOG", path)

    orchestrator._append_run_log(
        {
            "run_id": "new",
            "start": "2026-01-02T00:00:00+00:00",
            "status": "partial",
            "source_health_json": "{}",
        }
    )

    content = path.read_text(encoding="utf-8")
    assert "run_id" in content.splitlines()[0]
    assert "source_health_json" in content.splitlines()[0]
    assert "legacy" in content
    assert "new" in content


# ---------------------------------------------------------------------------
# #40 red-team follow-up: VCP subprocess failures must not read as "empty"
# ---------------------------------------------------------------------------


def test_vcp_missing_dependency_is_skipped_but_true_zero_is_empty(
    tmp_path, monkeypatch
) -> None:
    # No FMP key required anymore (universe from DataHub, prices from yfinance);
    # the screener is skipped only when its script/uv dependency is missing.
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setattr(scanner_agent, "_VCP_SCRIPT", tmp_path / "missing.py")

    missing = scanner_agent.fetch_vcp_screener_result()

    assert missing.health.state is SourceState.SKIPPED
    assert missing.health.detail_code == "dependency_missing"

    # With the script + uv present, a genuine zero-candidate screen is EMPTY.
    script = tmp_path / "screen_vcp.py"
    script.touch()
    monkeypatch.setattr(scanner_agent, "_VCP_SCRIPT", script)
    monkeypatch.setattr(scanner_agent.shutil, "which", lambda _command: "/usr/bin/uv")
    monkeypatch.setattr(scanner_agent, "fetch_vcp_screener_tickers", lambda: [])

    empty = scanner_agent.fetch_vcp_screener_result()

    assert empty.health.state is SourceState.EMPTY


def test_vcp_subprocess_failure_is_failed_not_empty(tmp_path, monkeypatch) -> None:
    """A subprocess that ran but failed must not be reported as zero matches."""
    monkeypatch.setenv("FMP_API_KEY", "configured")
    script = tmp_path / "screen_vcp.py"
    script.touch()
    monkeypatch.setattr(scanner_agent, "_VCP_SCRIPT", script)
    monkeypatch.setattr(scanner_agent.shutil, "which", lambda _command: "/usr/bin/uv")

    def _raise() -> list[str]:
        raise scanner_agent.VcpScreenerError("provider_failure", "boom")

    monkeypatch.setattr(scanner_agent, "fetch_vcp_screener_tickers", _raise)

    result = scanner_agent.fetch_vcp_screener_result()

    assert result.health.state is SourceState.FAILED
    assert result.health.detail_code == "provider_failure"
    assert result.tickers == []


def test_vcp_subprocess_nonzero_exit_raises_not_swallows(monkeypatch) -> None:
    """A real, unmocked subprocess failure must raise VcpScreenerError.

    Regression for the red-team finding that a nonzero subprocess exit was
    silently converted to an empty list (indistinguishable from a genuine
    zero-candidate screen) rather than surfacing as a failure.
    """
    monkeypatch.setenv("FMP_API_KEY", "configured")
    monkeypatch.setattr(scanner_agent, "_vcp_unavailable_reason", lambda: None)
    monkeypatch.setattr(scanner_agent.shutil, "which", lambda _command: "/usr/bin/uv")

    class _FakeCompletedProcess:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(
        scanner_agent.subprocess, "run", lambda *a, **k: _FakeCompletedProcess()
    )

    with pytest.raises(scanner_agent.VcpScreenerError):
        scanner_agent.fetch_vcp_screener_tickers()


# ---------------------------------------------------------------------------
# #40 red-team follow-up: Alpha Vantage / Congress failures vs genuine no-data
# ---------------------------------------------------------------------------


def test_alpha_vantage_distinguishes_failure_from_no_backfill_needed() -> None:
    client = scanner_agent._av_client
    client.api_key = "configured"
    client.reset_call_stats()
    assert client.call_stats == (0, 0)


def test_congress_get_stats_many_reports_failures(tmp_path, monkeypatch) -> None:
    from app.integrations.congress import CongressClient, _Lane

    client = CongressClient(cache_db_path=tmp_path / "cache.db")

    def _fake_fetch(_self, _ticker: str) -> tuple[str | None, bool]:
        return None, True

    monkeypatch.setattr(_Lane, "fetch_html", _fake_fetch)
    failed: list[str] = []

    result = client.get_stats_many(["BADCO"], failed_tickers=failed)

    assert result == {"BADCO": None}
    assert failed == ["BADCO"]


def test_extraction_reports_provider_failure_and_missing_local_source(
    tmp_path, monkeypatch
) -> None:
    import app.agents.extraction.extraction_agent as extraction_module

    # Disable the weekly-email StockTwits source so this test exercises the
    # config fallback (and never touches IMAP if .env leaked into the process).
    monkeypatch.delenv("EMAIL_USER", raising=False)
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(extraction_module, "_ST_WATCHLIST", tmp_path / "missing.json")
    monkeypatch.setattr(
        ExtractionAgent,
        "_fetch_heat_map",
        lambda _self: (_ for _ in ()).throw(RuntimeError("token secret-value")),
    )
    monkeypatch.setattr(
        ExtractionAgent, "_update_results_with_sources", lambda *_args: None
    )

    agent = ExtractionAgent(name="ExtractionAgent")
    assert agent.run() == []

    whale = agent.source_health[SourceName.WHALE_WISDOM]
    stocktwits = agent.source_health[SourceName.STOCKTWITS]
    assert whale.state is SourceState.FAILED
    assert "secret-value" not in whale.display_message
    assert stocktwits.state is SourceState.SKIPPED
