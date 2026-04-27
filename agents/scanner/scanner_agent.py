"""
Scanner Agent — pulls price/volume via yfinance, computes technicals with pandas-ta.
Outputs typed scan results for the Analyst Agent using the local MS Agent framework.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yfinance as yf

from ms_agent_framework import Agent
from models import StockRecord


_EXTRACTION_RESULTS = (
    Path(__file__).parent.parent / "extraction" / "extraction_results.json"
)
_WW_CONTEXT = Path(__file__).parent.parent / "extraction" / "ww_context.json"


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


PERIOD_DAYS = 252  # ~1 trading year for stage analysis



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


def _fetch_fundamentals(ticker: str) -> dict[str, Any]:
    """Return fundamental fields from yfinance (best-effort, None on failure)."""
    try:
        t = yf.Ticker(ticker)
        info: Any = t.info  # yfinance stubs type this as dict[Unknown, Unknown]
        return {
            "eps_growth": _quarterly_eps_growth(t, info),
            "annual_eps_growth": _annual_eps_growth(t),
            "roe": info.get("returnOnEquity"),
            "inst_ownership_pct": info.get("heldPercentInstitutions"),
            "pe_ratio": info.get("trailingPE"),
            "inst_count": _inst_count(t),
        }
    except Exception:
        return {
            "eps_growth": None, "annual_eps_growth": None, "roe": None,
            "inst_ownership_pct": None, "pe_ratio": None, "inst_count": None,
        }


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
        eps_row = "Diluted EPS" if "Diluted EPS" in qi.index else (
            "Basic EPS" if "Basic EPS" in qi.index else None
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
        eps_row = "Diluted EPS" if "Diluted EPS" in ai.index else (
            "Basic EPS" if "Basic EPS" in ai.index else None
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

    def run(self, payload: Iterable[str] | None = None) -> list[StockRecord]:
        tickers = list(payload) if payload else load_watchlist()
        spy_uptrend, spy_52w_return = _fetch_spy_context()
        return self.scan_watchlist(tickers, spy_uptrend, spy_52w_return)

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
            df.columns = df.columns.get_level_values(0)  # Get the field names (Close, High, etc.)
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
        rel_volume = (latest["volume"] / latest["vol_ma50"]) if latest["vol_ma50"] > 0 else 1.0

        # Weekly closes for the past 52 weeks (oldest → newest)
        weekly = df["close"].resample("W").last().dropna().tail(52)
        price_history = [round(float(v), 2) for v in weekly]

        return {
            "price": round(float(latest["close"]), 2),
            "price_history": price_history,
            "sma10": round(float(latest["sma10"]), 2) if pd.notna(latest["sma10"]) else None,
            "sma30": round(float(latest["sma30"]), 2) if pd.notna(latest["sma30"]) else None,
            "sma50": round(float(latest["sma50"]), 2) if pd.notna(latest["sma50"]) else None,
            "sma150": round(float(latest["sma150"]), 2) if pd.notna(latest["sma150"]) else None,
            "sma200": round(float(latest["sma200"]), 2) if pd.notna(latest["sma200"]) else None,
            "rsi14": round(float(latest["rsi"]), 1) if pd.notna(latest["rsi"]) else None,
            "atr14": round(float(latest["atr"]), 2) if pd.notna(latest["atr"]) else None,
            "volume": int(latest["volume"]),
            "vol_ma50": int(latest["vol_ma50"]) if pd.notna(latest["vol_ma50"]) else None,
            "rel_volume": round(float(rel_volume), 2),
            "high_52w": round(float(high_52w), 2),
            "low_52w": round(float(low_52w), 2),
            "high_base": round(float(df["high"].tail(50).max()), 2),
            "handle_low": round(float(df["low"].tail(15).min()), 2),
            "pct_from_52w_high": round((float(latest["close"]) / float(high_52w) - 1) * 100, 1),
            "pct_change_week": round((float(latest["close"]) / float(prev_week["close"]) - 1) * 100, 1),
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

    def scan_watchlist(
        self, tickers: list[str], spy_uptrend: bool, spy_52w_return: float
    ) -> list[StockRecord]:
        """Scan tickers, enriching each with fundamentals and SPY market context."""
        ww = load_ww_context()
        results: list[StockRecord] = []
        for ticker in tickers:
            try:
                df = self.fetch_stock_data(ticker)
                if df is None:
                    print(f"  [skip] {ticker}: insufficient data")
                    continue
                technicals = self.compute_technicals(df)
                fundamentals = _fetch_fundamentals(ticker)
                rel_strength = self.compute_rel_strength(
                    technicals["price_history"], spy_52w_return
                )
                ww_data = ww.get(ticker, {})
                record = StockRecord(
                    ticker=ticker,
                    as_of=datetime.today().strftime("%Y-%m-%d"),
                    spy_uptrend=spy_uptrend,
                    rel_strength_vs_spy=rel_strength,
                    funds_buying=ww_data.get("filers_increasing"),
                funds_selling=ww_data.get("filers_decreasing"),
                funds_net=(
                    ww_data["filers_increasing"] - ww_data["filers_decreasing"]
                    if "filers_increasing" in ww_data and "filers_decreasing" in ww_data
                    else None
                ),
                    **technicals,
                    **fundamentals,
                )
                results.append(record)
                print(
                    f"  [ok]   {ticker}: ${record.price}  RSI={record.rsi14}  "
                    f"RelVol={record.rel_volume}"
                )
            except Exception as error:
                print(f"  [err]  {ticker}: {error}")
        return results


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    agent = ScannerAgent(name="ScannerAgent")
    scan_results = agent.run()
    out = Path(__file__).parent / "scan_results.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump([item.model_dump() for item in scan_results], handle, indent=2)
    print(f"\nSaved {len(scan_results)} tickers to {out}")
