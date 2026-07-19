"""
Smoke tests for the stock agent pipeline.
These tests verify end-to-end functionality works correctly.
"""

import json
import os
from unittest.mock import patch, MagicMock

from app.orchestration.orchestrator import pipeline
from app.workflows.momentum import build_momentum_pipeline
from app.integrations.tv_screener import ScreenerResult


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
        "app.agents.scanner.scanner_agent.fetch_vcp_screener_tickers", return_value=[]
    )
    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch(
        "app.agents.analyst.analyst_agent.AnalystAgent.get_llm_client",
        return_value=None,
    )
    @patch("yfinance.download")
    def test_full_pipeline_execution(
        self,
        mock_download,
        _mock_llm,
        mock_congress,
        _mock_vcp,
        _mock_fund,
        _mock_tv_uk,
        _mock_tv,
    ):
        """Test that the full pipeline runs without errors."""
        import tempfile
        import app.orchestration.orchestrator as orchestrator

        mock_congress.get_stats.return_value = None

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
                    stage["stage"]: stage["state"]
                    for stage in running_status["stages"]
                }
                alert_lifecycle.append((by_stage["alerts"], by_stage["export"]))

            with (
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
                assert len(analysis_data) > 0
                assert "analysis" in analysis_data[0]
                assert "score" in analysis_data[0]["analysis"]

            with open(status_out) as f:
                status_data = json.load(f)
                assert status_data["state"] == "complete"
                assert [stage["stage"] for stage in status_data["stages"]] == [
                    "sources",
                    "market_data",
                    "enrichment",
                    "analysis",
                    "alerts",
                    "export",
                ]
                assert all(
                    stage["state"] == "complete" for stage in status_data["stages"]
                )
                for previous, current in zip(
                    status_data["stages"], status_data["stages"][1:], strict=False
                ):
                    assert previous["completed_at"] <= current["started_at"]
                assert status_data["scanned"] == status_data["analysed"]
                assert alert_lifecycle == [("running", "pending")]

            failed_status_out = os.path.join(tmp, "failed_pipeline_status.json")
            with (
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
        "app.agents.scanner.scanner_agent.fetch_vcp_screener_tickers", return_value=[]
    )
    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch(
        "app.agents.analyst.analyst_agent.AnalystAgent.get_llm_client",
        return_value=None,
    )
    @patch("yfinance.download")
    def test_agent_chaining(
        self,
        mock_download,
        _mock_llm,
        mock_congress,
        _mock_vcp,
        _mock_fund,
        _mock_tv_uk,
        _mock_tv,
    ):
        """Test that stages chain correctly through the typed pipeline."""
        mock_congress.get_stats.return_value = None
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
        scan_results = trace[0][1]
        analysis_results = trace[1][1]

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
