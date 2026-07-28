"""
Unit tests for ScannerAgent.
"""

import pandas as pd
from unittest.mock import patch

from app.agents.scanner.scanner_agent import ScannerAgent
from app.integrations.tv_screener import ScreenerResult
from app.schemas.source_health import SourceName, SourceResult, SourceState


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
    def test_fetch_stock_data_drops_trailing_nan_ohlc_bar(self, mock_download):
        """A trailing partial bar (volume only, NaN OHLC) must be dropped.

        Left in, ``iloc[-1]["close"]`` is NaN, ``price`` serializes as null,
        and every record fails StockRecord validation — blanking the watchlist.
        """
        dates = pd.date_range(start="2023-01-01", periods=101, freq="D")
        closes = [100 + i * 0.1 for i in range(101)]
        highs = [105 + i * 0.1 for i in range(101)]
        lows = [95 + i * 0.1 for i in range(101)]
        opens = [99 + i * 0.1 for i in range(101)]
        volumes = [1000000] * 101
        # Final bar carries volume but NaN OHLC, mimicking yfinance's partial row.
        closes[-1] = highs[-1] = lows[-1] = opens[-1] = float("nan")
        mock_download.return_value = pd.DataFrame(
            {
                "Close": closes,
                "High": highs,
                "Low": lows,
                "Open": opens,
                "Volume": volumes,
            },
            index=dates,
        )

        agent = ScannerAgent()
        df = agent.fetch_stock_data("AAPL")

        assert df is not None
        assert len(df) == 100  # the NaN bar was dropped
        assert not df[["open", "high", "low", "close"]].isna().any().any()

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

    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
        return_value={"eps_growth": None, "roe": None, "inst_ownership_pct": None},
    )
    @patch("yfinance.download")
    def test_scan_watchlist(self, mock_download, _mock_fundamentals_yf, _mock_fill):
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
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result",
        return_value=ScreenerResult([], "empty"),
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result_uk",
        return_value=ScreenerResult([], "empty"),
    )
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
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
    @patch("yfinance.download")
    def test_run_method(
        self,
        mock_download,
        mock_congress,
        _mock_vcp,
        _mock_fund,
        _mock_fill,
        _mock_tv_uk,
        _mock_tv,
    ):
        """Test the run method."""
        mock_congress.get_stats_many.return_value = {"TSLA": None}
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
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result",
        return_value=ScreenerResult([], "empty"),
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result_uk",
        return_value=ScreenerResult([], "empty"),
    )
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
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
    def test_run_method_default_watchlist(
        self, mock_congress, _mock_vcp, _mock_fund, _mock_fill, _mock_tv_uk, _mock_tv
    ):
        """Test run method with default watchlist."""
        mock_congress.get_stats_many.return_value = {}
        agent = ScannerAgent()

        # This would normally fetch data, but we'll just check it doesn't crash
        # In a real test, we'd mock yfinance
        with patch("yfinance.download") as mock_download:
            mock_download.return_value = pd.DataFrame()  # Empty to avoid processing
            results = agent.run()
            # Should return empty list for insufficient data
            assert isinstance(results, list)

    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result",
        return_value=ScreenerResult(["AAPL"], "ok"),
    )
    @patch(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result_uk",
        return_value=ScreenerResult([], "empty"),
    )
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
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
        return_value=SourceResult.from_items(SourceName.VCP_FMP, ["AAPL"]),
    )
    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("yfinance.download")
    def test_run_dedups_cross_source_ticker_and_combines_labels(
        self,
        mock_download,
        mock_congress,
        _mock_vcp,
        _mock_fund,
        _mock_fill,
        _mock_tv_uk,
        _mock_tv,
    ):
        """AAPL appears in WW payload, VCP screener, and TV screener; expect one record
        with all three source labels combined."""
        mock_congress.get_stats_many.return_value = {"AAPL": None}
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

    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
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
    @patch("yfinance.download")
    def test_scan_watchlist_assembles_records_after_parallel_fetch(
        self, mock_download, _mock_fundamentals_yf, _mock_fill, mock_congress
    ):
        """Phase A runs concurrently; congress is fetched in one batched call.

        Verifies that output order matches input order and that congress stats
        are fetched via a single get_stats_many call for all tickers while
        provider enrichments are overlapped.
        """
        mock_congress.get_stats_many.return_value = {
            "AAPL": None,
            "GOOGL": None,
            "MSFT": None,
        }
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

        events: list[tuple[str, str, int | None]] = []
        agent = ScannerAgent(
            progress_callback=lambda stage, state, count: events.append(
                (stage, state, count)
            )
        )
        results = agent.scan_watchlist(
            ["AAPL", "GOOGL", "MSFT"], spy_uptrend=True, spy_52w_return=10.0
        )

        assert len(results) == 3
        assert [r.ticker for r in results] == ["AAPL", "GOOGL", "MSFT"]
        mock_congress.get_stats_many.assert_called_once_with(
            ["AAPL", "GOOGL", "MSFT"], failed_tickers=[]
        )
        assert events == [
            ("market_data", "running", 3),
            ("market_data", "complete", 3),
            ("enrichment", "running", 3),
            ("enrichment", "complete", 3),
        ]

    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
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
    @patch("yfinance.download")
    def test_partial_congress_failures_stay_ok(
        self, mock_download, _mock_fundamentals_yf, _mock_fill, mock_congress
    ):
        """A few per-ticker Congress failures must not mark the source FAILED.

        A near-complete run (only some tickers failing) should report OK with a
        ``partial_ticker_failures`` code so it never raises a "Source failed"
        alert — only an all-failed run is a genuine outage.
        """

        def _partial(tickers, failed_tickers=None):
            # One ticker's request fails; the rest return data.
            from app.integrations.congress import CongressStats

            if failed_tickers is not None:
                failed_tickers.append(tickers[0])
            return {
                t: (None if t == tickers[0] else CongressStats(buys=1)) for t in tickers
            }

        mock_congress.get_stats_many.side_effect = _partial
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
        agent.scan_watchlist(
            ["AAPL", "GOOGL", "MSFT"], spy_uptrend=True, spy_52w_return=10.0
        )

        health = agent.source_health[SourceName.CONGRESS]
        assert health.state is SourceState.OK
        assert health.detail_code == "partial_ticker_failures"
        assert "1 of 3 Congress requests failed" in health.display_message


class _StubTicker:
    """Yields queued ``.info`` payloads to exercise the retry backoff."""

    def __init__(self, payloads):
        self._payloads = list(payloads)

    @property
    def info(self):
        return self._payloads.pop(0)


def test_fetch_ticker_info_retries_until_non_empty():
    """An empty first read is retried and the recovered payload is returned."""
    from app.agents.scanner.scanner_agent import _fetch_ticker_info

    stub = _StubTicker([{}, {}, {"sector": "Technology"}])
    with patch("tenacity.nap.time.sleep") as mock_sleep:
        info = _fetch_ticker_info(stub)

    assert info == {"sector": "Technology"}
    assert mock_sleep.call_count == 2  # backed off before each retry


def test_fetch_ticker_info_gives_up_after_retries():
    """Persistent throttling returns an empty dict rather than raising."""
    from app.agents.scanner.scanner_agent import _fetch_ticker_info

    stub = _StubTicker([{}, {}, {}])
    with patch("tenacity.nap.time.sleep"):
        assert _fetch_ticker_info(stub) == {}


def test_fetch_ticker_info_recovers_from_exception():
    """A raised .info (throttling) is retried, then the good payload returns."""
    from app.agents.scanner.scanner_agent import _fetch_ticker_info

    class _RaisingTicker:
        def __init__(self):
            self._calls = 0

        @property
        def info(self):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("rate limited")
            return {"sector": "Energy"}

    with patch("tenacity.nap.time.sleep"):
        assert _fetch_ticker_info(_RaisingTicker()) == {"sector": "Energy"}


class TestSectorCacheBackfill:
    """scan_watchlist reuses cached sectors when yfinance drops them (#106)."""

    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
        return_value={
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": None,  # simulate a throttled run: no sector this time
        },
    )
    @patch("yfinance.download")
    def test_null_sector_backfilled_from_cache(
        self, mock_download, _fund, _fill, mock_congress, tmp_path
    ):
        cache_path = tmp_path / "sector_cache.json"
        cache_path.write_text('{"AAPL": "Technology"}', encoding="utf-8")
        mock_congress.get_stats_many.return_value = {"AAPL": None}
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
        with patch("app.agents.scanner.scanner_agent.SECTOR_CACHE_JSON", cache_path):
            results = agent.scan_watchlist(
                ["AAPL"], spy_uptrend=True, spy_52w_return=10.0
            )

        assert results[0].sector == "Technology"

    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
        return_value={
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": "Healthcare",  # a fresh, populated run
        },
    )
    @patch("yfinance.download")
    def test_fresh_sector_persisted_to_cache(
        self, mock_download, _fund, _fill, mock_congress, tmp_path
    ):
        cache_path = tmp_path / "sector_cache.json"
        mock_congress.get_stats_many.return_value = {"DGX": None}
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
        with patch("app.agents.scanner.scanner_agent.SECTOR_CACHE_JSON", cache_path):
            agent.scan_watchlist(["DGX"], spy_uptrend=True, spy_52w_return=10.0)

        from app.agents.scanner.sector_cache import SectorCache

        assert SectorCache(cache_path).get("DGX") == "Healthcare"


class TestCurrencyNormalization:
    """LSE pence (GBp) prices are normalised to pounds and stamped (#123)."""

    def test_gbp_pence_converted_to_pounds(self):
        from app.agents.scanner.scanner_agent import _resolve_currency

        technicals = {
            "price": 4500.0,
            "sma50": 4200.0,
            "atr14": 50.0,
            "high_52w": 5000.0,
            "low_52w": 3000.0,
            "high_base": 4800.0,
            "handle_low": 4100.0,
            "price_history": [4000.0, 4500.0],
            "ohlcv_history": [
                {
                    "open": 4400.0,
                    "high": 4600.0,
                    "low": 4300.0,
                    "close": 4500.0,
                    "volume": 1000,
                },
            ],
        }
        currency = _resolve_currency("ULVR.L", technicals, "GBp")

        assert currency == "GBP"
        assert technicals["price"] == 45.0
        assert technicals["sma50"] == 42.0
        assert technicals["high_52w"] == 50.0
        assert technicals["price_history"] == [40.0, 45.0]
        assert technicals["ohlcv_history"][0]["close"] == 45.0
        # Non-monetary fields (volume) are untouched.
        assert technicals["ohlcv_history"][0]["volume"] == 1000

    def test_usd_passes_through_unchanged(self):
        from app.agents.scanner.scanner_agent import _resolve_currency

        technicals = {"price": 100.0}
        assert _resolve_currency("AAPL", technicals, "USD") == "USD"
        assert technicals["price"] == 100.0

    def test_unknown_currency_uk_ticker_defaults_to_pence(self):
        from app.agents.scanner.scanner_agent import _resolve_currency

        technicals = {"price": 250.0}
        assert _resolve_currency("BP.L", technicals, None) == "GBP"
        assert technicals["price"] == 2.5

    def test_unknown_currency_us_ticker_defaults_to_usd(self):
        from app.agents.scanner.scanner_agent import _resolve_currency

        technicals = {"price": 100.0}
        assert _resolve_currency("AAPL", technicals, None) == "USD"
        assert technicals["price"] == 100.0

    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
        return_value={
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": None,  # yfinance has no sector for the LSE ticker
        },
    )
    @patch("yfinance.download")
    def test_uk_sector_filled_from_screener_and_cached(
        self, mock_download, _fund, _fill, mock_congress, tmp_path
    ):
        """A UK ticker with no yfinance sector uses the screener sector (#122)."""
        cache_path = tmp_path / "sector_cache.json"
        mock_congress.get_stats_many.return_value = {"ULVR.L": None}
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
        with patch("app.agents.scanner.scanner_agent.SECTOR_CACHE_JSON", cache_path):
            results = agent.scan_watchlist(
                ["ULVR.L"],
                spy_uptrend=True,
                spy_52w_return=10.0,
                screener_sectors={"ULVR.L": "Consumer Defensive"},
            )

        assert results[0].sector == "Consumer Defensive"

        from app.agents.scanner.sector_cache import SectorCache

        assert SectorCache(cache_path).get("ULVR.L") == "Consumer Defensive"

    @patch("app.agents.scanner.scanner_agent._congress_client")
    @patch("app.agents.scanner.scanner_agent._fill_from_alpha_vantage")
    @patch(
        "app.agents.scanner.scanner_agent._fetch_fundamentals_yf",
        return_value={
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": None,
            "currency": "GBp",  # LSE pence
        },
    )
    @patch("yfinance.download")
    def test_uk_record_stamped_gbp_and_price_in_pounds(
        self, mock_download, _fund, _fill, mock_congress
    ):
        mock_congress.get_stats_many.return_value = {"ULVR.L": None}
        dates = pd.date_range(start="2023-01-01", periods=100, freq="D")
        # Pence-scale OHLC (~4500p): after /100 the price should read ~£45.
        mock_download.return_value = pd.DataFrame(
            {
                "Close": [4500 + i for i in range(100)],
                "High": [4550 + i for i in range(100)],
                "Low": [4450 + i for i in range(100)],
                "Open": [4480 + i for i in range(100)],
                "Volume": [1000000] * 100,
            },
            index=dates,
        )

        agent = ScannerAgent()
        results = agent.scan_watchlist(
            ["ULVR.L"], spy_uptrend=True, spy_52w_return=10.0
        )

        assert results[0].currency == "GBP"
        # Latest close ~4599p → ~£45.99, i.e. converted out of pence.
        assert 40 < results[0].price < 60
