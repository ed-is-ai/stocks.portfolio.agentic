from __future__ import annotations

from pydantic import BaseModel, Field


class StockScan(BaseModel):
    ticker: str
    as_of: str
    price: float
    sma10: float | None = None
    sma30: float | None = None
    sma50: float | None = None
    sma150: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    volume: int
    vol_ma50: int | None = None
    rel_volume: float
    high_52w: float
    low_52w: float
    pct_from_52w_high: float
    pct_change_week: float


class StockAnalysis(BaseModel):
    score: int = Field(ge=1, le=10)
    stage: str
    near_entry: bool
    strengths: list[str] = []
    risks: list[str] = []
    summary: str


class StockRecord(StockScan):
    analysis: StockAnalysis | None = None


class EmailConfig(BaseModel):
    host: str
    port: int
    user: str
    password: str
    recipient: str
