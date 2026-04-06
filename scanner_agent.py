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
    "NVDA",
    "MSFT",
    "AAPL",
    "META",
    "GOOGL",
    "AMZN",
    "TSLA",
    "AMD",
    "AVGO",
    "CRM",
]

PERIOD_DAYS = 252  # ~1 trading year for stage analysis


class ScannerAgent(Agent):
    name: str = "ScannerAgent"

    def run(self, payload: Iterable[str] | None = None) -> list[StockRecord]:
        tickers = list(payload) if payload else WATCHLIST
        return self.scan_watchlist(tickers)

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

        return {
            "price": round(float(latest["close"]), 2),
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
        }

    def scan_watchlist(self, tickers: list[str]) -> list[StockRecord]:
        results: list[StockRecord] = []
        for ticker in tickers:
            try:
                df = self.fetch_stock_data(ticker)
                if df is None:
                    print(f"  [skip] {ticker}: insufficient data")
                    continue
                record = StockRecord(
                    ticker=ticker,
                    as_of=datetime.today().strftime("%Y-%m-%d"),
                    **self.compute_technicals(df),
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
