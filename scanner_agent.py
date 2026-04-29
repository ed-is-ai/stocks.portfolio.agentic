"""
Scanner Agent — pulls price/volume via yfinance, computes technicals with pandas-ta.
Outputs typed scan results for the Analyst Agent using the local MS Agent framework.
"""

import json
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd
import yfinance as yf

from ms_agent_framework import Agent
from models import StockRecord


WATCHLIST = [
    # StockTwits Top 25 Momentum — S&P 500 (2026-04-06)
    "SNDK",  # Sandisk
    "LYB",   # LyondellBasell
    "DOW",   # Dow Inc.
    "APA",   # APA Corporation
    "WDC",   # Western Digital
    "GLW",   # Corning Inc.
    "CF",    # CF Industries
    "MRNA",  # Moderna
    "TER",   # Teradyne
    "STX",   # Seagate Technology
    "TPL",   # Texas Pacific Land
    "OXY",   # Occidental Petroleum
    "FIX",   # Comfort Systems
    "VLO",   # Valero Energy
    "MPC",   # Marathon Petroleum
    "BG",    # Bunge Global
    "KEYS",  # Keysight Technologies
    "Q",     # Qnity Electronics
    "GNRC",  # Generac
    "COP",   # ConocoPhillips
    "DELL",  # Dell Technologies
    "GEV",   # GE Vernova
    "PSX",   # Phillips 66
    "INTC",  # Intel (also Nasdaq 100)
    "EOG",   # EOG Resources
    # StockTwits Top 25 Momentum — Nasdaq 100 (2026-04-06)
    "ARM",   # Arm Holdings
    "AMAT",  # Applied Materials
    "BKR",   # Baker Hughes
    "FANG",  # Diamondback Energy
    "MU",    # Micron Technology
    "LRCX",  # Lam Research
    "ODFL",  # Old Dominion Freight
    "MRVL",  # Marvell Technology
    "KLAC",  # KLA Corporation
    "MPWR",  # Monolithic Power Systems
    "ASML",  # ASML Holding
    "ROST",  # Ross Stores
    "LIN",   # Linde plc
    "COST",  # Costco
    "HON",   # Honeywell
    "ADI",   # Analog Devices
    "FAST",  # Fastenal
    "AEP",   # American Electric Power
    "GILD",  # Gilead Sciences
    "CSX",   # CSX Corporation
    "EXC",   # Exelon
    "WMT",   # Walmart
    # StockTwits Top 25 Momentum — Russell 2000 (2026-04-06)
    "ERAS",  # Erasca Inc
    "IBRX",  # ImmunityBio Inc
    "SATL",  # Satellogic Inc
    "FSLY",  # Fastly Inc
    "KOS",   # Kosmos Energy
    "AAOI",  # Applied Optoelectronics
    "ICHR",  # Ichor Holdings
    "ELVN",  # Enliven Therapeutics
    "UCTT",  # Ultra Clean Holdings
    "TNGX",  # Tango Therapeutics
    "ALMS",  # Alumis Inc
    "DAWN",  # Day One Biopharmaceuticals
    "TROX",  # Tronox Holdings
    "AEHR",  # Aehr Test Systems
    "SPIR",  # Spire Global
    "DNTH",  # Dianthus Therapeutics
    "VIAV",  # Viavi Solutions
    "ELDN",  # Eledon Pharmaceuticals
    "AMPX",  # Amprius Technologies
    "VAL",   # Valaris Ltd
    "WTI",   # W&T Offshore
    "CRVS",  # Corvus Pharmaceuticals
    "TSSI",  # TSS Inc
    "DOCN",  # DigitalOcean
    "CURV",  # Torrid Holdings
]

PERIOD_DAYS = 252  # ~1 trading year for stage analysis
FETCH_BUFFER_DAYS = 100  # extra calendar days so SMA200 has enough history


def _fetch_spy_context() -> tuple[bool, float]:
    """Return (spy_uptrend, spy_52w_return_pct) from SPY price history.

    spy_uptrend  — True when SPY's latest close is above its 200-day SMA.
    spy_52w_return_pct — SPY percentage return over the past ~52 weeks.
    """
    try:
        end = datetime.today()
        start = end - timedelta(days=PERIOD_DAYS + FETCH_BUFFER_DAYS)
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


def _fetch_fundamentals(ticker: str) -> dict:
    """Return fundamental fields from yfinance Ticker.info (best-effort, None on failure)."""
    try:
        info = yf.Ticker(ticker).info
        return {
            "eps_growth": info.get("earningsGrowth"),          # float or None
            "roe": info.get("returnOnEquity"),                  # float or None
            "inst_ownership_pct": info.get("heldPercentInstitutions"),  # float or None
        }
    except Exception:
        return {"eps_growth": None, "roe": None, "inst_ownership_pct": None}


class ScannerAgent(Agent):
    name: str = "ScannerAgent"

    def run(self, payload: Iterable[str] | None = None) -> list[StockRecord]:
        tickers = list(payload) if payload else WATCHLIST
        spy_uptrend, spy_52w_return = _fetch_spy_context()
        return self.scan_watchlist(tickers, spy_uptrend, spy_52w_return)

    def fetch_stock_data(self, ticker: str) -> pd.DataFrame | None:
        end = datetime.today()
        start = end - timedelta(days=PERIOD_DAYS + FETCH_BUFFER_DAYS)
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

        sma200_now = df["sma200"].iloc[-1]
        sma200_20d = df["sma200"].iloc[-21] if len(df) > 21 else None
        sma200_rising: bool | None = (
            bool(sma200_now > sma200_20d)
            if sma200_20d is not None and pd.notna(sma200_now) and pd.notna(sma200_20d)
            else None
        )

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
            "pct_from_52w_high": round((float(latest["close"]) / float(high_52w) - 1) * 100, 1),
            "pct_change_week": round((float(latest["close"]) / float(prev_week["close"]) - 1) * 100, 1),
            "sma200_rising": sma200_rising,
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
                record = StockRecord(
                    ticker=ticker,
                    as_of=datetime.today().strftime("%Y-%m-%d"),
                    spy_uptrend=spy_uptrend,
                    rel_strength_vs_spy=rel_strength,
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
    agent = ScannerAgent()
    scan_results = agent.run()
    with open("scan_results.json", "w", encoding="utf-8") as handle:
        json.dump([item.model_dump() for item in scan_results], handle, indent=2)
    print(f"\nSaved {len(scan_results)} tickers to scan_results.json")
