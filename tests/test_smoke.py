"""
Smoke tests for the stock agent pipeline.
These tests verify end-to-end functionality works correctly.
"""

from datetime import datetime
import json
import os
from pathlib import Path
from typing import cast
from unittest.mock import patch, MagicMock

from app.orchestration.orchestrator import pipeline
from app.workflows.momentum import build_momentum_pipeline
from app.integrations.tv_screener import ScreenerResult
from app.schemas.record import StockRecord
from app.schemas.source_health import SourceName, SourceResult, SourceState


class TestSmokeTests:
    """Smoke tests for the complete pipeline."""

    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result",
        return_value=ScreenerResult([], "empty"),
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result_uk",
        return_value=ScreenerResult([], "empty"),
    )
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals",
        return_value={
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": None,
        },
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_vcp_screener_result",
        return_value=SourceResult.unavailable(
            SourceName.VCP_FMP,
            SourceState.SKIPPED,
            "missing_configuration",
            "FMP API key is not configured.",
        ),
    )
    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch(
        "app.agents.analyst.analyst_agent.AnalystAgent.get_llm_client",
        return_value=None,
    )
    @patch("app.orchestration.orchestrator.AnthropicNarrativeClient")
    @patch("yfinance.download")
    def test_full_pipeline_execution(
        self,
        mock_download,
        mock_anthropic_client_cls,
        _mock_llm,
        mock_congress,
        _mock_vcp,
        _mock_fund,
        _mock_tv_uk,
        _mock_tv,
    ):
        """Test that the full pipeline runs without errors."""
        # Never hit the real Anthropic API from a smoke test, regardless of
        # whether ANTHROPIC_API_KEY happens to be set in the local .env.
        mock_anthropic_client_cls.return_value.enabled = False
        import tempfile
        import app.orchestration.orchestrator as orchestrator

        # A fixed fixture list, not the real (gitignored, locally-generated)
        # extraction_results.json -- this test only needs 10 tickers to
        # exercise the pipeline, not any particular watchlist content.
        smoke_watchlist = [
            "AAPL",
            "MSFT",
            "GOOGL",
            "AMZN",
            "NVDA",
            "META",
            "TSLA",
            "AMD",
            "NFLX",
            "INTC",
        ]
        assert len(smoke_watchlist) == 10

        mock_congress.get_stats_many.side_effect = lambda tickers, failed_tickers=None: (
            dict.fromkeys(tickers)
        )

        # Mock yfinance to return sample data
        import pandas as pd

        dates = pd.date_range(start="2023-01-01", periods=100, freq="D")
        mock_download.return_value = pd.DataFrame(
            {
                "Close": [100 + i * 0.1 for i in range(100)],
                "High": [105 + i * 0.1 for i in range(100)],
                "Low": [95 + i * 0.1 for i in range(100)],
                "Open": [99 + i * 0.1 for i in range(100)],
                "Volume": [1000000] * 100,
            },
            index=dates,
        )

        # Redirect output to temp files so the real results are not overwritten
        with tempfile.TemporaryDirectory() as tmp:
            scan_out = os.path.join(tmp, "scan.json")
            analysis_out = os.path.join(tmp, "analysis.json")
            excel_out = os.path.join(tmp, "analysis.xlsx")
            status_out = os.path.join(tmp, "pipeline_status.json")
            alert_lifecycle: list[tuple[str, str]] = []

            def capture_alert_lifecycle(*_args, **_kwargs):
                with open(status_out) as status_stream:
                    running_status = json.load(status_stream)
                by_stage = {
                    stage["stage"]: stage["state"] for stage in running_status["stages"]
                }
                alert_lifecycle.append((by_stage["alerts"], by_stage["export"]))

            with (
                patch.object(
                    orchestrator, "load_watchlist", return_value=smoke_watchlist
                ),
                patch.object(orchestrator, "load_source_map", return_value={}),
                patch.object(orchestrator, "SCAN_OUTPUT", scan_out),
                patch.object(orchestrator, "ANALYSIS_OUTPUT", analysis_out),
                patch.object(orchestrator, "EXCEL_OUTPUT", excel_out),
                patch.object(orchestrator, "PIPELINE_STATUS_JSON", status_out),
                patch.object(
                    orchestrator.AlertAgent,
                    "send_summary_email",
                    side_effect=capture_alert_lifecycle,
                ),
            ):
                pipeline(force=True)

            # Verify output files were created
            assert os.path.exists(scan_out)
            assert os.path.exists(analysis_out)

            # Verify JSON files contain data
            with open(scan_out) as f:
                scan_data = json.load(f)
                # scan_results.json is now a grouped dict keyed by source
                assert "as_of" in scan_data
                assert "ww_extraction" in scan_data
                ww = scan_data["ww_extraction"]
                assert "_comment" in ww
                assert "results" in ww
                assert len(ww["results"]) > 0
                assert "ticker" in ww["results"][0]
                assert "price" in ww["results"][0]

            with open(analysis_out) as f:
                analysis_data = json.load(f)
                # The artifact is a self-describing envelope (run ownership
                # metadata + records) rather than a bare list — see
                # app.schemas.analysis_artifact for why that matters for
                # freshness/ownership correctness.
                assert analysis_data["meta"]["run_id"]
                assert analysis_data["meta"]["generated_at"]
                records = analysis_data["records"]
                assert len(records) > 0
                assert "analysis" in records[0]
                assert "score" in records[0]["analysis"]

            with open(status_out) as f:
                status_data = json.load(f)
                # This environment has no FMP/Alpha Vantage keys configured,
                # so VCP and Alpha Vantage are legitimately "skipped" —
                # coverage is reduced, so the run is "partial" rather than
                # "complete" (see derive_pipeline_outcome).
                assert status_data["state"] == "partial"
                assert status_data["analysis_artifact_produced"] is True
                assert status_data["source_health"]["vcp_fmp"]["state"] == "skipped"
                assert [stage["stage"] for stage in status_data["stages"]] == [
                    "sources",
                    "market_data",
                    "enrichment",
                    "analysis",
                    "alerts",
                    "export",
                    "price_backfill",
                ]
                assert all(
                    stage["state"] == "complete" for stage in status_data["stages"][:-1]
                )
                # This fixture can have either no repair candidates or no
                # evidence database. Both outcomes stay isolated from the
                # otherwise-derived partial pipeline result.
                assert status_data["stages"][-1]["state"] in {"failed", "skipped"}
                for previous, current in zip(
                    status_data["stages"], status_data["stages"][1:], strict=False
                ):
                    assert previous["completed_at"] <= current["started_at"]
                assert status_data["scanned"] == status_data["analysed"]
                assert alert_lifecycle == [("running", "pending")]

            # --- Regression: a crash right after analysis-artifact promotion
            # must not leave freshness/ownership pointing at the previous run.
            #
            # The bug this guards against: an earlier design promoted the
            # analysis artifact with `os.replace`, then made a *separate*
            # call to record "this run produced a usable artifact" in the
            # status repo. A crash between those two steps left the file on
            # disk legitimately belonging to the new run while every
            # consumer of "last usable run" kept reporting the *previous*
            # run's (older) timestamp — a silent mismatch between what was
            # displayed and what was true. The fix embeds run ownership
            # inside the artifact payload itself, so promoting the file and
            # publishing its ownership are the same atomic write.
            first_run_meta = json.loads(Path(analysis_out).read_text())["meta"]

            original_replace = os.replace
            crashed = {"done": False}

            def _replace_then_crash(src, dst, *args, **kwargs):
                result = original_replace(src, dst, *args, **kwargs)
                if not crashed["done"] and str(dst) == str(analysis_out):
                    crashed["done"] = True
                    raise RuntimeError("simulated crash after artifact promotion")
                return result

            with (
                patch.object(
                    orchestrator, "load_watchlist", return_value=smoke_watchlist
                ),
                patch.object(orchestrator, "load_source_map", return_value={}),
                patch.object(orchestrator, "SCAN_OUTPUT", scan_out),
                patch.object(orchestrator, "ANALYSIS_OUTPUT", analysis_out),
                patch.object(orchestrator, "EXCEL_OUTPUT", excel_out),
                patch.object(orchestrator, "PIPELINE_STATUS_JSON", status_out),
                patch.object(
                    orchestrator.os, "replace", side_effect=_replace_then_crash
                ),
                patch.object(orchestrator, "_append_run_log"),
            ):
                assert pipeline(force=True) is False

            assert crashed["done"], "the injected crash point was never reached"

            second_run_meta = json.loads(Path(analysis_out).read_text())["meta"]
            assert second_run_meta["run_id"] != first_run_meta["run_id"]

            with open(status_out) as status_stream:
                second_status = json.load(status_stream)
            # The bug's precondition: status bookkeeping for the new run
            # never reached a usable terminal state...
            assert second_status["run_id"] == second_run_meta["run_id"]
            assert second_status["state"] == "failed"

            # ...yet the artifact's own embedded ownership is what freshness
            # must be derived from, so it still correctly reflects run B.
            from app.schemas.analysis_artifact import read_analysis_artifact_meta
            from app.services.freshness_service import (
                FreshnessState,
                calculate_freshness,
            )

            owner = read_analysis_artifact_meta(Path(analysis_out))
            assert owner is not None
            assert owner.run_id == second_run_meta["run_id"]
            freshness = calculate_freshness(owner.generated_at)
            assert freshness.state is FreshnessState.FRESH
            expected_generated_at = datetime.fromisoformat(
                second_run_meta["generated_at"].replace("Z", "+00:00")
            )
            assert freshness.refreshed_at == expected_generated_at

            failed_status_out = os.path.join(tmp, "failed_pipeline_status.json")
            with (
                patch.object(
                    orchestrator, "load_watchlist", return_value=smoke_watchlist
                ),
                patch.object(orchestrator, "load_source_map", return_value={}),
                patch.object(orchestrator, "PIPELINE_STATUS_JSON", failed_status_out),
                patch.object(
                    orchestrator.AlertAgent,
                    "send_summary_email",
                    side_effect=RuntimeError("email post-processing failed"),
                ),
                patch.object(orchestrator, "_append_run_log"),
            ):
                pipeline(force=True)

            with open(failed_status_out) as status_stream:
                failed_status = json.load(status_stream)
            failed_stages = {
                stage["stage"]: stage["state"] for stage in failed_status["stages"]
            }
            assert failed_status["state"] == "failed"
            assert failed_stages["alerts"] == "failed"
            assert failed_stages["export"] == "skipped"
            assert failed_status["error_summary"] == "email post-processing failed"

    def test_pipeline_persists_failure_at_active_stage(self, tmp_path):
        """Unexpected failures leave a terminal artifact, never stale running UI."""
        import app.orchestration.orchestrator as orchestrator

        status_out = tmp_path / "pipeline_status.json"
        with (
            patch.object(orchestrator, "PIPELINE_STATUS_JSON", status_out),
            patch.object(
                orchestrator, "load_watchlist", side_effect=OSError("bad input")
            ),
            patch.object(orchestrator, "_append_run_log"),
        ):
            pipeline(force=True)

        status_data = json.loads(status_out.read_text())
        assert status_data["state"] == "failed"
        assert status_data["current_stage"] == "sources"
        assert status_data["stages"][0]["state"] == "failed"
        assert status_data["error_summary"] == "bad input"

    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result",
        return_value=ScreenerResult([], "empty"),
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result_uk",
        return_value=ScreenerResult([], "empty"),
    )
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals",
        return_value={
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": None,
        },
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_vcp_screener_result",
        return_value=SourceResult.unavailable(
            SourceName.VCP_FMP,
            SourceState.SKIPPED,
            "missing_configuration",
            "FMP API key is not configured.",
        ),
    )
    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch(
        "app.agents.analyst.analyst_agent.AnalystAgent.get_llm_client",
        return_value=None,
    )
    @patch("app.orchestration.orchestrator.AnthropicNarrativeClient")
    @patch("yfinance.download")
    def test_agent_chaining(
        self,
        mock_download,
        mock_anthropic_client_cls,
        _mock_llm,
        mock_congress,
        _mock_vcp,
        _mock_fund,
        _mock_tv_uk,
        _mock_tv,
    ):
        """Test that stages chain correctly through the typed pipeline."""
        # Never hit the real Anthropic API from a smoke test, regardless of
        # whether ANTHROPIC_API_KEY happens to be set in the local .env.
        mock_anthropic_client_cls.return_value.enabled = False
        mock_congress.get_stats_many.side_effect = lambda tickers, failed_tickers=None: (
            dict.fromkeys(tickers)
        )
        # Mock yfinance
        import pandas as pd

        dates = pd.date_range(start="2023-01-01", periods=100, freq="D")
        mock_download.return_value = pd.DataFrame(
            {
                "Close": [100 + i * 0.1 for i in range(100)],
                "High": [105 + i * 0.1 for i in range(100)],
                "Low": [95 + i * 0.1 for i in range(100)],
                "Open": [99 + i * 0.1 for i in range(100)],
                "Volume": [1000000] * 100,
            },
            index=dates,
        )

        # Build and run the momentum pipeline with a traced run
        final_result, trace = build_momentum_pipeline().run_traced(["NVDA"])

        # Verify the trace exposes every stage's output
        assert [name for name, _ in trace] == ["scan", "analyse", "alert"]
        scan_results = cast(list[StockRecord], trace[0][1])
        analysis_results = cast(list[StockRecord], trace[1][1])

        assert len(scan_results) == 1
        assert len(analysis_results) == 1
        assert analysis_results[0].analysis is not None
        assert isinstance(analysis_results[0].analysis.score, int)
        # Final stage returns an AlertSummary
        assert isinstance(final_result.buy_count, int)

    def test_market_hours_check(self):
        """Test market hours logic."""
        from app.orchestration.orchestrator import is_market_hours

        # Mock datetime to be during market hours
        with patch("app.orchestration.orchestrator.datetime") as mock_datetime:
            mock_dt = MagicMock()
            mock_dt.hour = 10
            mock_dt.minute = 30
            mock_dt.weekday.return_value = 0  # Monday
            mock_datetime.now.return_value = mock_dt
            assert is_market_hours() is True

        # Mock datetime to be outside market hours
        with patch("app.orchestration.orchestrator.datetime") as mock_datetime:
            mock_dt = MagicMock()
            mock_dt.hour = 18
            mock_dt.minute = 0
            mock_dt.weekday.return_value = 0  # Monday
            mock_datetime.now.return_value = mock_dt
            assert is_market_hours() is False

        # Mock datetime to be weekend
        with patch("app.orchestration.orchestrator.datetime") as mock_datetime:
            mock_dt = MagicMock()
            mock_dt.hour = 10
            mock_dt.minute = 30
            mock_dt.weekday.return_value = 5  # Saturday
            mock_datetime.now.return_value = mock_dt
            assert is_market_hours() is False
