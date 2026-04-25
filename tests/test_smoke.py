"""
Smoke tests for the stock agent pipeline.
These tests verify end-to-end functionality works correctly.
"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from orchestrator import pipeline
from agents.scanner.scanner_agent import ScannerAgent
from agents.analyst.analyst_agent import AnalystAgent
from agents.alert.alert_agent import AlertAgent
from ms_agent_framework import AgentApp


class TestSmokeTests:
    """Smoke tests for the complete pipeline."""

    @patch('yfinance.download')
    def test_full_pipeline_execution(self, mock_download):
        """Test that the full pipeline runs without errors."""
        # Mock yfinance to return sample data
        import pandas as pd
        dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
        mock_download.return_value = pd.DataFrame({
            'Close': [100 + i * 0.1 for i in range(100)],
            'High': [105 + i * 0.1 for i in range(100)],
            'Low': [95 + i * 0.1 for i in range(100)],
            'Open': [99 + i * 0.1 for i in range(100)],
            'Volume': [1000000] * 100
        }, index=dates)

        # Run pipeline with force=True to skip market hours check
        pipeline(force=True)

        # Verify output files were created
        assert os.path.exists('agents/scanner/scan_results.json')
        assert os.path.exists('agents/analyst/analysis_results.json')

        # Verify JSON files contain data
        with open('agents/scanner/scan_results.json') as f:
            scan_data = json.load(f)
            assert len(scan_data) > 0
            assert 'ticker' in scan_data[0]
            assert 'price' in scan_data[0]

        with open('agents/analyst/analysis_results.json') as f:
            analysis_data = json.load(f)
            assert len(analysis_data) > 0
            assert 'analysis' in analysis_data[0]
            assert 'score' in analysis_data[0]['analysis']

    @patch('yfinance.download')
    def test_agent_chaining(self, mock_download):
        """Test that agents chain correctly through AgentApp."""
        # Mock yfinance
        import pandas as pd
        dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
        mock_download.return_value = pd.DataFrame({
            'Close': [100 + i * 0.1 for i in range(100)],
            'High': [105 + i * 0.1 for i in range(100)],
            'Low': [95 + i * 0.1 for i in range(100)],
            'Open': [99 + i * 0.1 for i in range(100)],
            'Volume': [1000000] * 100
        }, index=dates)

        # Create and run agent app
        scanner = ScannerAgent()
        analyst = AnalystAgent()
        alerter = AlertAgent()
        app = AgentApp(name="TestApp")
        app.add_agent(scanner)
        app.add_agent(analyst)
        app.add_agent(alerter)

        # Execute pipeline
        final_result, intermediates = app.execute_with_intermediates(['NVDA'])

        # Verify results
        assert len(intermediates) == 2  # scan_results and analysis_results
        scan_results, analysis_results = intermediates

        assert len(scan_results) == 1
        assert len(analysis_results) == 1
        assert analysis_results[0].analysis is not None
        assert isinstance(analysis_results[0].analysis.score, int)

    def test_market_hours_check(self):
        """Test market hours logic."""
        from orchestrator import is_market_hours

        # Mock datetime to be during market hours
        with patch('orchestrator.datetime') as mock_datetime:
            mock_dt = MagicMock()
            mock_dt.hour = 10
            mock_dt.minute = 30
            mock_dt.weekday.return_value = 0  # Monday
            mock_datetime.now.return_value = mock_dt
            assert is_market_hours() == True

        # Mock datetime to be outside market hours
        with patch('orchestrator.datetime') as mock_datetime:
            mock_dt = MagicMock()
            mock_dt.hour = 18
            mock_dt.minute = 0
            mock_dt.weekday.return_value = 0  # Monday
            mock_datetime.now.return_value = mock_dt
            assert is_market_hours() == False

        # Mock datetime to be weekend
        with patch('orchestrator.datetime') as mock_datetime:
            mock_dt = MagicMock()
            mock_dt.hour = 10
            mock_dt.minute = 30
            mock_dt.weekday.return_value = 5  # Saturday
            mock_datetime.now.return_value = mock_dt
            assert is_market_hours() == False