"""Pure technical calculations with live-compatibility and historical adapters."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Protocol, cast

import pandas as pd

from app.services.backtest.historical_scan_record import TechnicalsV1
from app.services.backtest.market_planes import DETERMINISTIC_DECIMAL_CONTEXT


class HistoricalTechnicalRow(Protocol):
    @property
    def session(self) -> date: ...

    @property
    def open(self) -> Decimal: ...

    @property
    def high(self) -> Decimal: ...

    @property
    def low(self) -> Decimal: ...

    @property
    def close(self) -> Decimal: ...

    @property
    def volume(self) -> Decimal | None: ...


def compute_live_technicals(df: pd.DataFrame) -> dict[str, Any]:
    """Preserve the Scanner's established pandas outputs byte-for-byte."""
    close = df["close"]
    volume = df["volume"]

    df["sma10"] = close.rolling(window=10).mean()
    df["sma30"] = close.rolling(window=30).mean()
    df["sma50"] = close.rolling(window=50).mean()
    df["sma150"] = close.rolling(window=150).mean()
    df["sma200"] = close.rolling(window=200).mean()

    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

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
        latest["volume"] / latest["vol_ma50"] if latest["vol_ma50"] > 0 else 1.0
    )

    weekly = df["close"].resample("W").last().dropna().tail(52)
    price_history = [round(float(value), 2) for value in weekly]
    ohlcv_history: list[dict[str, float | int | str]] = []
    for row_date, row in df.iloc[::-1].iterrows():
        date_value = cast(Any, row_date)
        ohlcv_history.append(
            {
                "date": date_value.strftime("%Y-%m-%d"),
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
        "rsi14": round(float(latest["rsi"]), 1) if pd.notna(latest["rsi"]) else None,
        "atr14": round(float(latest["atr"]), 2) if pd.notna(latest["atr"]) else None,
        "volume": int(latest["volume"]),
        "vol_ma50": int(latest["vol_ma50"]) if pd.notna(latest["vol_ma50"]) else None,
        "rel_volume": round(float(rel_volume), 2),
        "high_52w": round(float(high_52w), 2),
        "low_52w": round(float(low_52w), 2),
        "high_base": round(float(df["high"].tail(50).max()), 2),
        "handle_low": round(float(df["low"].tail(15).min()), 2),
        "pct_from_52w_high": round(
            (float(latest["close"]) / float(high_52w) - 1) * 100, 1
        ),
        "pct_change_week": round(
            (float(latest["close"]) / float(prev_week["close"]) - 1) * 100,
            1,
        ),
        "ohlcv_history": ohlcv_history,
    }


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def compute_reconstruction_technicals(
    rows: tuple[HistoricalTechnicalRow, ...],
) -> TechnicalsV1:
    """Calculate deterministic Decimal technicals over exactly 252 bounded rows."""
    if len(rows) != 252:
        raise ValueError("required_data_missing")
    if any(row.volume is None for row in rows):
        raise ValueError("required_data_missing")
    if tuple(row.session for row in rows) != tuple(sorted(row.session for row in rows)):
        raise ValueError("integrity_error")

    with localcontext(DETERMINISTIC_DECIMAL_CONTEXT):
        closes = tuple(row.close for row in rows)
        highs = tuple(row.high for row in rows)
        lows = tuple(row.low for row in rows)
        volumes = tuple(cast(Decimal, row.volume) for row in rows)

        def sma(window: int) -> Decimal:
            return _mean(closes[-window:])

        deltas = tuple(closes[index] - closes[index - 1] for index in range(1, 252))
        recent_deltas = deltas[-14:]
        average_gain = _mean(tuple(max(delta, Decimal(0)) for delta in recent_deltas))
        average_loss = _mean(tuple(max(-delta, Decimal(0)) for delta in recent_deltas))
        if average_loss == 0:
            rsi = Decimal(100) if average_gain > 0 else Decimal(0)
        else:
            relative_strength = average_gain / average_loss
            rsi = Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)

        true_ranges = tuple(
            max(
                rows[index].high - rows[index].low,
                abs(rows[index].high - rows[index - 1].close),
                abs(rows[index].low - rows[index - 1].close),
            )
            for index in range(1, 252)
        )
        atr = _mean(true_ranges[-14:])
        vol_ma50 = _mean(volumes[-50:])
        rel_volume = volumes[-1] / vol_ma50 if vol_ma50 > 0 else Decimal(1)
        high_52w = max(highs)
        low_52w = min(lows)
        latest = closes[-1]

        return TechnicalsV1(
            price=latest,
            sma10=sma(10),
            sma30=sma(30),
            sma50=sma(50),
            sma150=sma(150),
            sma200=sma(200),
            rsi14=rsi,
            atr14=atr,
            volume=volumes[-1],
            vol_ma50=vol_ma50,
            rel_volume=rel_volume,
            high_52w=high_52w,
            low_52w=low_52w,
            high_base=max(highs[-50:]),
            handle_low=min(lows[-15:]),
            pct_from_52w_high=(latest / high_52w - Decimal(1)) * Decimal(100),
            pct_change_week=(latest / closes[-6] - Decimal(1)) * Decimal(100),
        )


def weekly_closes(
    rows: tuple[HistoricalTechnicalRow, ...],
) -> tuple[Decimal, ...]:
    """Return oldest-first exchange-session weekly closes without wall-clock data."""
    grouped: dict[tuple[int, int], Decimal] = {}
    for row in rows:
        iso = row.session.isocalendar()
        grouped[(iso.year, iso.week)] = row.close
    return tuple(grouped[key] for key in sorted(grouped))[-52:]


__all__ = [
    "HistoricalTechnicalRow",
    "compute_live_technicals",
    "compute_reconstruction_technicals",
    "weekly_closes",
]
