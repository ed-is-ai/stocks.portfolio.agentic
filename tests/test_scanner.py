"""
Unit tests for ScannerAgent.
"""

import pandas as pd
from unittest.mock import patch

from app.agents.scanner.scanner_agent import ScannerAgent


class TestScannerAgent:
    """Test ScannerAgent functionality."""

    @patch("yfinance.download")
    def test_fetch_stock_data_success(self, mock_download):
        """Test successful stock data fetching."""
        # Mock yfinance response
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

        agent = ScannerAgent()
        df = agent.fetch_stock_data("AAPL")

        assert df is not None
        assert len(df) == 100
        assert "close" in df.columns
        assert "high" in df.columns
        assert "low" in df.columns
        assert "open" in df.columns
        assert "volume" in df.columns

    @patch("yfinance.download")
    def test_fetch_stock_data_insufficient_data(self, mock_download):
        """Test handling of insufficient data."""
        mock_download.return_value = pd.DataFrame()  # Empty DataFrame

        agent = ScannerAgent()
        df = agent.fetch_stock_data("INVALID")

        assert df is None

    def test_compute_technicals(self, sample_stock_data):
        """Test technical indicator calculations."""
        agent = ScannerAgent()
        result = agent.compute_technicals(sample_stock_data)

        # Check that all expected fields are present
        expected_fields = [
            "price",
            "sma10",
            "sma30",
            "sma50",
            "sma150",
            "sma200",
            "rsi14",
            "atr14",
            "volume",
            "vol_ma50",
            "rel_volume",
            "high_52w",
            "low_52w",
            "pct_from_52w_high",
            "pct_change_week",
        ]

        for field in expected_fields:
            assert field in result

        # Check price is correct (last close)
        assert result["price"] == 104.0

        # Check volume
        assert result["volume"] == 1400000

    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals",
        return_value={"eps_growth": None, "roe": None, "inst_ownership_pct": None},
    )
    @patch("yfinance.download")
    def test_scan_watchlist(self, mock_download, _mock_fundamentals):
        """Test scanning multiple tickers."""
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

        agent = ScannerAgent()
        results = agent.scan_watchlist(
            ["AAPL", "GOOGL"], spy_uptrend=True, spy_52w_return=10.0
        )

        assert len(results) == 2
        assert results[0].ticker == "AAPL"
        assert results[1].ticker == "GOOGL"
        assert all(r.price > 0 for r in results)
        assert all(r.volume > 0 for r in results)

    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_tickers", return_value=[]
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_tickers_uk", return_value=[]
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
    @patch("yfinance.download")
    def test_run_method(
        self, mock_download, mock_congress, _mock_vcp, _mock_fund, _mock_tv_uk, _mock_tv
    ):
        """Test the run method."""
        mock_congress.get_stats.return_value = None
        # Mock yfinance response
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

        agent = ScannerAgent()
        results = agent.run(["TSLA"])

        assert len(results) == 1
        assert results[0].ticker == "TSLA"
        assert isinstance(results[0].price, float)
        assert results[0].price > 0

    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_tickers", return_value=[]
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_tickers_uk", return_value=[]
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
    def test_run_method_default_watchlist(
        self, mock_congress, _mock_vcp, _mock_fund, _mock_tv_uk, _mock_tv
    ):
        """Test run method with default watchlist."""
        mock_congress.get_stats.return_value = None
        agent = ScannerAgent()

        # This would normally fetch data, but we'll just check it doesn't crash
        # In a real test, we'd mock yfinance
        with patch("yfinance.download") as mock_download:
            mock_download.return_value = pd.DataFrame()  # Empty to avoid processing
            results = agent.run()
            # Should return empty list for insufficient data
            assert isinstance(results, list)

    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_tickers",
        return_value=["AAPL"],
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_tickers_uk",
        return_value=[],
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
        "app.agents.scanner.scanner_agent.fetch_vcp_screener_tickers",
        return_value=["AAPL"],
    )
    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("yfinance.download")
    def test_run_dedups_cross_source_ticker_and_combines_labels(
        self, mock_download, mock_congress, _mock_vcp, _mock_fund, _mock_tv_uk, _mock_tv
    ):
        """AAPL appears in WW payload, VCP screener, and TV screener; expect one record
        with all three source labels combined."""
        mock_congress.get_stats.return_value = None
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

        agent = ScannerAgent()
        results = agent.run(["AAPL"])

        assert len(results) == 1
        assert results[0].ticker == "AAPL"
        assert set(results[0].source.split(",")) == {
            "ww_extraction",
            "vcp_screener",
            "tv_screener",
        }
