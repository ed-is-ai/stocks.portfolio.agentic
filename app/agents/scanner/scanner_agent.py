"""
Scanner Agent — pulls price/volume via yfinance, computes technicals with pandas-ta.
Outputs typed scan results for the Analyst Agent using the local MS Agent framework.
"""

import glob as _glob
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import yfinance as yf

from app.agents.base import Agent
from app.core.config import (
    EXTRACTION_RESULTS_JSON,
    SKILLS_DIR,
    WW_CONTEXT_JSON,
)
from app.integrations.alpha_vantage import AlphaVantageClient
from app.integrations.congress import CongressClient
from app.integrations.tv_screener import (
    fetch_tv_screener_result,
    fetch_tv_screener_result_uk,
)
from app.schemas import StockRecord
from app.schemas.source_health import (
    SourceHealth,
    SourceName,
    SourceResult,
    SourceState,
)

_av_client = AlphaVantageClient()
_congress_client = CongressClient()


_EXTRACTION_RESULTS = EXTRACTION_RESULTS_JSON
_WW_CONTEXT = WW_CONTEXT_JSON


def load_watchlist() -> list[str]:
    """Load tickers from extraction_results.json, flattening grouped sources."""
    with open(_EXTRACTION_RESULTS, encoding="utf-8") as fh:
        data: list[str] | dict[str, list[str]] = json.load(fh)
    if isinstance(data, list):
        return data
    seen: set[str] = set()
    result: list[str] = []
    for group in data.values():
        for ticker in group:
            if ticker not in seen:
                result.append(ticker)
                seen.add(ticker)
    return result


def load_ww_context() -> dict[str, dict[str, int]]:
    """Return per-ticker WhalWisdom context: {ticker: {filers_increasing, filers_decreasing, ww_rank}}."""
    try:
        return json.loads(_WW_CONTEXT.read_text(encoding="utf-8"))
    except OSError:
        return {}


def load_source_map() -> dict[str, tuple[bool, bool]]:
    """Return per-ticker source flags: (in_stocktwits, in_whale_wisdom)."""
    with open(_EXTRACTION_RESULTS, encoding="utf-8") as fh:
        data: list[str] | dict[str, list[str]] = json.load(fh)
    if isinstance(data, list):
        return {ticker: (False, False) for ticker in data}
    sources: dict[str, tuple[bool, bool]] = {}
    for key, tickers in data.items():
        is_st = "stocktwits" in key.lower()
        is_ww = "wisdom" in key.lower()
        for ticker in tickers:
            st, ww = sources.get(ticker, (False, False))
            sources[ticker] = (st or is_st, ww or is_ww)
    return sources


def is_tv_source(source: str) -> bool:
    """Return True if the source label includes the TradingView screener."""
    return "tv_screener" in source.split(",")


PERIOD_DAYS = 252  # ~1 trading year for stage analysis

_SKILLS_DIR = SKILLS_DIR
_VCP_SCRIPT = _SKILLS_DIR / "vcp-screener" / "scripts" / "screen_vcp.py"


class VcpScreenerError(RuntimeError):
    """Raised when the VCP/FMP subprocess ran but its output can't be trusted.

    Callers must not treat this the same as a genuine zero-candidate result
    — a subprocess failure or missing/unreadable output means the screen
    never actually completed, so reporting it as "empty" would hide a real
    outage behind what looks like a quiet, successful run.
    """

    def __init__(self, detail_code: str, message: str) -> None:
        self.detail_code = detail_code
        super().__init__(message)


def _vcp_unavailable_reason() -> tuple[str, str] | None:
    """Return (detail_code, message) if VCP/FMP cannot run at all, else None."""
    if not os.environ.get("FMP_API_KEY"):
        return "missing_configuration", "FMP API key is not configured."
    if not _VCP_SCRIPT.exists() or not shutil.which("uv"):
        return "dependency_missing", "VCP screener dependency is unavailable."
    return None


def fetch_vcp_screener_tickers() -> list[str]:
    """Run vcp-screener against S&P 500 via FMP API and return candidate symbols.

    Returns an empty list only when preconditions are missing (no API key,
    no script, no ``uv`` on PATH) or when the screen genuinely found zero
    candidates. Raises ``VcpScreenerError`` when the subprocess ran but
    failed, timed out, or produced no readable output — those cases must
    not be silently reported as zero candidates.
    """
    reason = _vcp_unavailable_reason()
    if reason is not None:
        _code, message = reason
        print(f"  [skip] vcp_screener: {message}")
        return []
    uv = shutil.which("uv")
    assert uv is not None  # guaranteed by _vcp_unavailable_reason() above
    api_key = os.environ["FMP_API_KEY"]
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result = subprocess.run(
                [
                    uv,
                    "run",
                    "python",
                    str(_VCP_SCRIPT),
                    "--api-key",
                    api_key,
                    "--output-dir",
                    tmpdir,
                    "--max-candidates",
                    "250",
                    "--top",
                    "100",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise VcpScreenerError("timeout", "VCP / FMP timed out.") from exc
        if result.returncode != 0:
            message = f"vcp_screener subprocess failed: {result.stderr[:200]}"
            print(f"  [warn] {message}")
            raise VcpScreenerError("provider_failure", message)
        json_files = _glob.glob(f"{tmpdir}/vcp_screener_*.json")
        if not json_files:
            message = "vcp_screener produced no output JSON"
            print(f"  [warn] {message}")
            raise VcpScreenerError("output_missing", message)
        try:
            data = json.loads(Path(json_files[0]).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            message = "vcp_screener output was not valid JSON"
            print(f"  [warn] {message}")
            raise VcpScreenerError("invalid_output", message) from exc
        return [r["symbol"] for r in data.get("results", []) if r.get("symbol")]


def fetch_vcp_screener_result() -> SourceResult:
    """Return VCP candidates with an explicit unavailable/empty/failed distinction."""
    started_at = datetime.now(timezone.utc)
    reason = _vcp_unavailable_reason()
    if reason is not None:
        code, message = reason
        return SourceResult.unavailable(
            SourceName.VCP_FMP,
            SourceState.SKIPPED,
            code,
            message,
            started_at=started_at,
        )
    try:
        tickers = fetch_vcp_screener_tickers()
    except VcpScreenerError as exc:
        return SourceResult.unavailable(
            SourceName.VCP_FMP,
            SourceState.FAILED,
            exc.detail_code,
            str(exc),
            started_at=started_at,
        )
    return SourceResult.from_items(SourceName.VCP_FMP, tickers, started_at=started_at)


def _fetch_spy_context() -> tuple[bool, float]:
    """Return (spy_uptrend, spy_52w_return_pct) from SPY price history.

    spy_uptrend  — True when SPY's latest close is above its 200-day SMA.
    spy_52w_return_pct — SPY percentage return over the past ~52 weeks.
    """
    try:
        end = datetime.today()
        start = end - timedelta(days=PERIOD_DAYS + 60)
        df = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
        if df.empty or len(df) < 200:
            return True, 0.0
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"] if "Close" in df.columns else df["close"]
        sma200 = float(close.rolling(200).mean().iloc[-1])
        latest = float(close.iloc[-1])
        oldest = float(close.iloc[max(0, len(close) - 252)])
        spy_uptrend = latest > sma200
        spy_52w_return = (latest / oldest - 1) * 100
        return spy_uptrend, round(spy_52w_return, 2)
    except Exception:
        return True, 0.0


def _fetch_fundamentals_yf(ticker: str) -> dict[str, Any]:
    """Return fundamental fields from yfinance only (no Alpha Vantage).

    Network: yfinance only. Safe to call from worker threads. None fields are
    left for the (rate-limited, serial) Alpha Vantage back-fill to fill later.
    """
    try:
        t = yf.Ticker(ticker)
        info: Any = t.info
        return {
            "eps_growth": _quarterly_eps_growth(t, info),
            "annual_eps_growth": _annual_eps_growth(t),
            "roe": info.get("returnOnEquity"),
            "inst_ownership_pct": info.get("heldPercentInstitutions"),
            "pe_ratio": info.get("trailingPE"),
            "inst_count": _inst_count(t),
            "sector": info.get("sector"),
        }
    except Exception:
        return {
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "inst_ownership_pct": None,
            "pe_ratio": None,
            "inst_count": None,
            "sector": None,
        }


def _fetch_fundamentals(ticker: str) -> dict[str, Any]:
    """Return fundamentals from yfinance, back-filled from Alpha Vantage.

    Behavior unchanged: yfinance first, then the (serial, rate-limited) Alpha
    Vantage fallback for any field still None.
    """
    result = _fetch_fundamentals_yf(ticker)
    _fill_from_alpha_vantage(ticker, result)
    return result


def _fill_from_alpha_vantage(ticker: str, result: dict[str, Any]) -> None:
    """Back-fill None fields in *result* from Alpha Vantage (modifies in place).

    Only runs when ALPHA_VANTAGE_API_KEY is set and at least one field is missing.
    Uses EARNINGS endpoint for precise YoY EPS (preferred) then OVERVIEW as fallback.
    """
    if not _av_client.enabled:
        return

    needs_eps = result.get("eps_growth") is None
    needs_annual = result.get("annual_eps_growth") is None
    needs_basics = any(result.get(k) is None for k in ("roe", "pe_ratio", "sector"))

    if not (needs_eps or needs_annual or needs_basics):
        return

    # EARNINGS gives the most accurate quarterly/annual EPS numbers
    if needs_eps:
        result["eps_growth"] = _av_client.get_quarterly_eps_growth(ticker)
    if needs_annual:
        result["annual_eps_growth"] = _av_client.get_annual_eps_cagr(ticker)

    # OVERVIEW fills the remaining fields (one API call)
    if needs_basics or (needs_eps and result["eps_growth"] is None):
        av_data = _av_client.get_fundamentals(ticker)
        if result.get("roe") is None:
            result["roe"] = av_data.get("roe")
        if result.get("pe_ratio") is None:
            result["pe_ratio"] = av_data.get("pe_ratio")
        if result.get("sector") is None:
            result["sector"] = av_data.get("sector")
        if result.get("eps_growth") is None:
            result["eps_growth"] = av_data.get("eps_growth")


def _quarterly_eps_growth(t: yf.Ticker, info: Any) -> float | None:
    """Compute most-recent-quarter YoY EPS growth from quarterly income statement.

    Compares Q0 Diluted EPS to Q4 (same quarter one year ago).
    Falls back to info.earningsGrowth when insufficient quarterly data.
    Skips the calculation when the year-ago EPS was negative (sign flip distorts %).
    """
    try:
        qi = t.quarterly_income_stmt
        if qi.empty:
            return info.get("earningsGrowth")
        eps_row = (
            "Diluted EPS"
            if "Diluted EPS" in qi.index
            else ("Basic EPS" if "Basic EPS" in qi.index else None)
        )
        if eps_row is None:
            return info.get("earningsGrowth")
        # .values gives a plain numpy array, avoiding Series[Any] | Any iloc typing issues
        eps_vals: list[float] = [float(v) for v in qi.loc[eps_row].values]
        if len(eps_vals) < 5:
            return info.get("earningsGrowth")
        recent, year_ago = eps_vals[0], eps_vals[4]
        if pd.isna(recent) or pd.isna(year_ago) or year_ago <= 0:
            return info.get("earningsGrowth")
        return round((recent - year_ago) / year_ago, 4)
    except Exception:
        return info.get("earningsGrowth")


def _annual_eps_growth(t: yf.Ticker) -> float | None:
    """Compute multi-year annual EPS CAGR from the annual income statement.

    Uses up to 4 years of Diluted EPS data (3 growth periods).
    Returns None when fewer than 3 data points are available or EPS was
    negative in the base year (CAGR undefined for sign-flip scenarios).
    """
    try:
        ai = t.income_stmt
        if ai.empty:
            return None
        eps_row = (
            "Diluted EPS"
            if "Diluted EPS" in ai.index
            else ("Basic EPS" if "Basic EPS" in ai.index else None)
        )
        if eps_row is None:
            return None
        # Drop NaN and convert to plain floats to sidestep Series[Any] | Any iloc typing
        eps_vals: list[float] = [
            float(v) for v in ai.loc[eps_row].values if not pd.isna(v)
        ]
        if len(eps_vals) < 3:
            return None
        n = min(len(eps_vals), 4)  # cap at 4 data points (3 growth periods)
        newest, oldest = eps_vals[0], eps_vals[n - 1]
        if oldest <= 0 or newest <= 0:
            return None
        cagr = (newest / oldest) ** (1 / (n - 1)) - 1
        return round(cagr, 4)
    except Exception:
        return None


def _inst_count(ticker_obj: yf.Ticker) -> int | None:
    """Return total number of institutions holding shares from major_holders."""
    try:
        mh = ticker_obj.major_holders
        if mh is None or mh.empty:
            return None
        row = mh[mh.index == "institutionsCount"]
        if row.empty:
            return None
        return int(float(row.iloc[0, 0]))
    except Exception:
        return None


class ScannerAgent(Agent):
    name: str = "ScannerAgent"
    source_health: dict[SourceName, SourceHealth] = {}
    progress_callback: Callable[[str, str, int | None], None] | None = None

    def _report_progress(
        self, stage: str, state: str, count: int | None = None
    ) -> None:
        if self.progress_callback:
            self.progress_callback(stage, state, count)

    def run(self, payload: Iterable[str] | None = None) -> list[StockRecord]:
        ww_tickers = list(payload) if payload else load_watchlist()
        print(f"\n[Scanner] WW extraction:    {len(ww_tickers)} tickers")

        def _fetch_tradingview_sources() -> tuple[SourceResult, SourceResult]:
            # Keep TradingView requests serial: its polite rate limit applies to
            # the shared endpoint, unlike the independent Yahoo and FMP work.
            print("[Scanner] TradingView screener (US)...")
            tv_result = fetch_tv_screener_result()
            print(
                f"[Scanner] TV screener US:   {len(tv_result.tickers)} tickers "
                f"({tv_result.status})"
            )

            print("[Scanner] TradingView screener (UK)...")
            uk_result = fetch_tv_screener_result_uk()
            print(
                f"[Scanner] TV screener UK:   {len(uk_result.tickers)} tickers "
                f"({uk_result.status})"
            )
            return tv_result, uk_result

        # These are independent network-bound tasks.  Running them together
        # avoids making the scan wait for Yahoo, FMP, and TradingView in turn.
        with ThreadPoolExecutor(max_workers=3) as pool:
            spy_future = pool.submit(_fetch_spy_context)
            vcp_future = pool.submit(fetch_vcp_screener_result)
            tv_future = pool.submit(_fetch_tradingview_sources)
            spy_uptrend, spy_52w_return = spy_future.result()
            vcp_result = vcp_future.result()
            tv_result, uk_result = tv_future.result()

        vcp_tickers = vcp_result.tickers
        tv_tickers = tv_result.tickers
        uk_tickers = [
            ticker if ticker.endswith(".L") else f"{ticker}.L"
            for ticker in uk_result.tickers
        ]
        self.source_health = {
            SourceName.VCP_FMP: vcp_result.health,
            SourceName.TRADINGVIEW_US: tv_result.health.model_copy(
                update={"source": SourceName.TRADINGVIEW_US}
            ),
            SourceName.TRADINGVIEW_UK: uk_result.health.model_copy(
                update={"source": SourceName.TRADINGVIEW_UK}
            ),
        }

        print(f"[Scanner] VCP screener:     {len(vcp_tickers)} tickers")

        # Build the deduplicated union, recording each ticker's sources in
        # first-seen order. Each ticker is scanned exactly once.
        sources_by_ticker: dict[str, list[str]] = {}
        for label, tickers in (
            ("ww_extraction", ww_tickers),
            ("vcp_screener", vcp_tickers),
            ("tv_screener", tv_tickers),
            ("tv_screener_uk", uk_tickers),
        ):
            for ticker in tickers:
                labels = sources_by_ticker.setdefault(ticker, [])
                if label not in labels:
                    labels.append(label)

        all_tickers = list(sources_by_ticker)
        self._report_progress("sources", "complete", len(all_tickers))
        records = self.scan_watchlist(all_tickers, spy_uptrend, spy_52w_return)
        for r in records:
            r.source = ",".join(sources_by_ticker[r.ticker])
        return records

    def fetch_stock_data(self, ticker: str) -> pd.DataFrame | None:
        end = datetime.today()
        start = end - timedelta(days=PERIOD_DAYS + 60)
        df = yf.download(
            ticker,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
        )
        if df.empty or len(df) < 50:
            return None
        # Handle MultiIndex columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(
                0
            )  # Get the field names (Close, High, etc.)
        df.columns = [c.lower() for c in df.columns]
        return df

    def compute_technicals(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        volume = df["volume"]

        # Calculate SMAs manually (simple moving averages)
        df["sma10"] = close.rolling(window=10).mean()
        df["sma30"] = close.rolling(window=30).mean()
        df["sma50"] = close.rolling(window=50).mean()
        df["sma150"] = close.rolling(window=150).mean()
        df["sma200"] = close.rolling(window=200).mean()

        # Calculate RSI manually (simplified version)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))

        # Calculate ATR manually (simplified)
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - close.shift()).abs()
        low_close = (df["low"] - close.shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr"] = tr.rolling(window=14).mean()

        df["vol_ma50"] = volume.rolling(window=50).mean()

        latest = df.iloc[-1]
        prev_week = df.iloc[-6] if len(df) > 6 else df.iloc[0]
        year_slice = df.tail(252)
        high_52w = year_slice["high"].max()
        low_52w = year_slice["low"].min()
        rel_volume = (
            (latest["volume"] / latest["vol_ma50"]) if latest["vol_ma50"] > 0 else 1.0
        )

        # Weekly closes for the past 52 weeks (oldest → newest)
        weekly = df["close"].resample("W").last().dropna().tail(52)
        price_history = [round(float(v), 2) for v in weekly]

        # Daily OHLCV in FMP-compatible format (most recent first) for VCP calculators
        ohlcv_history: list[dict[str, float | int | str]] = []
        for date, row in df.iloc[::-1].iterrows():
            ohlcv_history.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4),
                    "close": round(float(row["close"]), 4),
                    "volume": int(row["volume"]),
                }
            )

        return {
            "price": round(float(latest["close"]), 2),
            "price_history": price_history,
            "sma10": round(float(latest["sma10"]), 2)
            if pd.notna(latest["sma10"])
            else None,
            "sma30": round(float(latest["sma30"]), 2)
            if pd.notna(latest["sma30"])
            else None,
            "sma50": round(float(latest["sma50"]), 2)
            if pd.notna(latest["sma50"])
            else None,
            "sma150": round(float(latest["sma150"]), 2)
            if pd.notna(latest["sma150"])
            else None,
            "sma200": round(float(latest["sma200"]), 2)
            if pd.notna(latest["sma200"])
            else None,
            "rsi14": round(float(latest["rsi"]), 1)
            if pd.notna(latest["rsi"])
            else None,
            "atr14": round(float(latest["atr"]), 2)
            if pd.notna(latest["atr"])
            else None,
            "volume": int(latest["volume"]),
            "vol_ma50": int(latest["vol_ma50"])
            if pd.notna(latest["vol_ma50"])
            else None,
            "rel_volume": round(float(rel_volume), 2),
            "high_52w": round(float(high_52w), 2),
            "low_52w": round(float(low_52w), 2),
            "high_base": round(float(df["high"].tail(50).max()), 2),
            "handle_low": round(float(df["low"].tail(15).min()), 2),
            "pct_from_52w_high": round(
                (float(latest["close"]) / float(high_52w) - 1) * 100, 1
            ),
            "pct_change_week": round(
                (float(latest["close"]) / float(prev_week["close"]) - 1) * 100, 1
            ),
            "ohlcv_history": ohlcv_history,
        }

    def compute_rel_strength(
        self, price_history: list[float], spy_52w_return: float
    ) -> float | None:
        """Return stock's 52w return minus SPY's 52w return (pct points)."""
        if len(price_history) < 2:
            return None
        oldest = price_history[0]
        newest = price_history[-1]
        if oldest <= 0:
            return None
        stock_52w_return = (newest / oldest - 1) * 100
        return round(stock_52w_return - spy_52w_return, 2)

    def _fetch_ticker_yf(
        self, ticker: str, spy_52w_return: float
    ) -> dict[str, Any] | None:
        """Phase A (thread-safe): fetch yfinance data + technicals for a ticker.

        Returns a partial bundle, or None when data is insufficient or the
        fetch errors (matching the old skip/err handling). No rate-limited
        client (Alpha Vantage, congress) is touched here.
        """
        try:
            df = self.fetch_stock_data(ticker)
            if df is None:
                print(f"  [skip] {ticker}: insufficient data")
                return None
            technicals = self.compute_technicals(df)
            fundamentals = _fetch_fundamentals_yf(ticker)
            rel_strength = self.compute_rel_strength(
                technicals["price_history"], spy_52w_return
            )
            return {
                "ticker": ticker,
                "technicals": technicals,
                "fundamentals": fundamentals,
                "rel_strength": rel_strength,
            }
        except Exception as error:
            print(f"  [err]  {ticker}: {error}")
            return None

    def scan_watchlist(
        self, tickers: list[str], spy_uptrend: bool, spy_52w_return: float
    ) -> list[StockRecord]:
        """Scan tickers, enriching each with fundamentals and SPY market context.

        Phase A fetches all yfinance data concurrently.  The Alpha Vantage and
        congressional enrichments are each serial to honour their provider
        limits, but run alongside one another because they use independent
        clients.  Records are assembled in input order.
        """
        if not tickers:
            return []
        ww = load_ww_context()

        # Phase A: concurrent yfinance fetch (no rate-limited clients).
        self._report_progress("market_data", "running", len(tickers))

        def _worker(t: str) -> dict[str, Any] | None:
            return self._fetch_ticker_yf(t, spy_52w_return)

        with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
            fetched = list(pool.map(_worker, tickers))

        # Phase B: keep each provider's requests serial, while overlapping the
        # two independent, rate-limited providers.  Running them one after the
        # other makes a large watchlist pay both wait budgets end-to-end.
        valid_fetched = [data for data in fetched if data is not None]
        self._report_progress("market_data", "complete", len(valid_fetched))
        self._report_progress("enrichment", "running", len(valid_fetched))

        yahoo_failures = len(tickers) - len(valid_fetched)
        yahoo_state = (
            SourceState.FAILED
            if tickers and not valid_fetched
            else (SourceState.OK if valid_fetched else SourceState.EMPTY)
        )
        self.source_health[SourceName.YAHOO_MARKET_DATA] = SourceHealth(
            source=SourceName.YAHOO_MARKET_DATA,
            state=yahoo_state,
            count=len(valid_fetched),
            detail_code=(
                "partial_ticker_failures"
                if yahoo_failures and valid_fetched
                else ("ticker_failures" if yahoo_failures else "")
            ),
            display_message=(
                f"{yahoo_failures} ticker request(s) failed; "
                f"{len(valid_fetched)} succeeded."
                if yahoo_failures
                else "Yahoo market data completed successfully."
            ),
        )

        def _fill_all_from_alpha_vantage() -> None:
            _av_client.reset_call_stats()
            for data in valid_fetched:
                _fill_from_alpha_vantage(data["ticker"], data["fundamentals"])

        failed_congress_tickers: list[str] = []

        def _fetch_all_congress() -> dict[str, Any]:
            return _congress_client.get_stats_many(
                [data["ticker"] for data in valid_fetched],
                failed_tickers=failed_congress_tickers,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            alpha_future = pool.submit(_fill_all_from_alpha_vantage)
            congress_future = pool.submit(_fetch_all_congress)
            # Propagate an unexpected provider/programming failure as before.
            alpha_future.result()
            congress_by_ticker = congress_future.result()
        self._report_progress("enrichment", "complete", len(valid_fetched))

        av_attempts, av_failures = _av_client.call_stats
        if not _av_client.enabled:
            av_state, av_code, av_message = (
                SourceState.SKIPPED,
                "missing_configuration",
                "Alpha Vantage API key is not configured.",
            )
        elif av_attempts == 0:
            av_state, av_code, av_message = (
                SourceState.EMPTY,
                "",
                "No Alpha Vantage backfill was required.",
            )
        elif av_failures >= av_attempts:
            av_state, av_code, av_message = (
                SourceState.FAILED,
                "provider_failure",
                "All Alpha Vantage requests failed.",
            )
        elif av_failures:
            av_state, av_code, av_message = (
                SourceState.OK,
                "partial_ticker_failures",
                f"{av_failures} of {av_attempts} Alpha Vantage requests failed.",
            )
        else:
            av_state, av_code, av_message = (
                SourceState.OK,
                "",
                "Alpha Vantage backfill completed.",
            )
        self.source_health[SourceName.ALPHA_VANTAGE] = SourceHealth(
            source=SourceName.ALPHA_VANTAGE,
            state=av_state,
            count=max(0, av_attempts - av_failures),
            detail_code=av_code,
            display_message=av_message,
        )

        congress_success = sum(
            value is not None for value in congress_by_ticker.values()
        )
        congress_failed = len(failed_congress_tickers)
        if congress_failed and congress_failed >= len(valid_fetched):
            congress_state, congress_code = SourceState.FAILED, "provider_failure"
            congress_message = "All Congress requests failed."
        elif congress_failed:
            congress_state, congress_code = (
                SourceState.FAILED,
                "partial_ticker_failures",
            )
            congress_message = (
                f"{congress_failed} of {len(valid_fetched)} Congress requests failed; "
                f"{congress_success} ticker(s) had data."
            )
        elif congress_success:
            congress_state, congress_code = SourceState.OK, ""
            congress_message = (
                f"Congress data completed for {congress_success} of "
                f"{len(valid_fetched)} tickers."
            )
        else:
            congress_state, congress_code = SourceState.EMPTY, ""
            congress_message = (
                "No congressional trading data found for these tickers."
                if valid_fetched
                else "No tickers required Congress enrichment."
            )
        self.source_health[SourceName.CONGRESS] = SourceHealth(
            source=SourceName.CONGRESS,
            state=congress_state,
            count=congress_success,
            detail_code=congress_code,
            display_message=congress_message,
        )

        # Phase C: record assembly preserves the watchlist order.
        results: list[StockRecord] = []
        for data in valid_fetched:
            ticker = data["ticker"]
            technicals = data["technicals"]
            fundamentals = data["fundamentals"]
            ww_data = ww.get(ticker, {})
            congress = congress_by_ticker[ticker]
            record = StockRecord(
                ticker=ticker,
                as_of=datetime.today().strftime("%Y-%m-%d"),
                spy_uptrend=spy_uptrend,
                rel_strength_vs_spy=data["rel_strength"],
                funds_buying=ww_data.get("filers_increasing"),
                funds_selling=ww_data.get("filers_decreasing"),
                funds_net=(
                    ww_data["filers_increasing"] - ww_data["filers_decreasing"]
                    if "filers_increasing" in ww_data and "filers_decreasing" in ww_data
                    else None
                ),
                congress_buys=congress.buys if congress else None,
                congress_sells=congress.sells if congress else None,
                senate_buys=congress.senate_buys if congress else None,
                senate_sells=congress.senate_sells if congress else None,
                **technicals,
                **fundamentals,
            )
            results.append(record)
            print(
                f"  [ok]   {ticker}: ${record.price}  RSI={record.rsi14}  "
                f"RelVol={record.rel_volume}"
            )
        return results


if __name__ == "__main__":
    from app.core.config import SCAN_RESULTS_JSON

    agent = ScannerAgent(name="ScannerAgent")
    scan_results = agent.run()
    out = SCAN_RESULTS_JSON
    with open(out, "w", encoding="utf-8") as handle:
        json.dump([item.model_dump() for item in scan_results], handle, indent=2)
    print(f"\nSaved {len(scan_results)} tickers to {out}")
