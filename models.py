from __future__ import annotations

from pydantic import BaseModel, Field


class StockScan(BaseModel):
    ticker: str
    as_of: str
    price: float
    price_history: list[float] = []  # Weekly closes, past 52 weeks (oldest → newest)
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
    high_base: float | None = None   # Highest daily high over the past 10 weeks (pivot entry reference)
    handle_low: float | None = None  # Lowest daily low over the past 3 weeks (handle stop reference)
    pct_from_52w_high: float
    pct_change_week: float
    # Fundamental data (fetched from yfinance Ticker.info)
    eps_growth: float | None = None          # Quarterly EPS growth YoY (0.25 = 25%)
    roe: float | None = None                 # Return on equity (0.17 = 17%)
    inst_ownership_pct: float | None = None  # Institutional ownership fraction (0.0–1.0)
    pe_ratio: float | None = None            # Trailing twelve-month P/E ratio
    funds_buying: int | None = None          # # institutions that added/initiated in latest 13F quarter
    # Market context
    rel_strength_vs_spy: float | None = None  # Stock 52w return minus SPY 52w return (pct pts)
    spy_uptrend: bool | None = None           # True if SPY is above its 200-day SMA
    # Watchlist source flags
    in_stocktwits: bool = False
    in_whale_wisdom: bool = False


class MomentumScore(BaseModel):
    """Per-letter technical momentum scores, each 0–2 (max 14).

    Uses price/volume proxies for CANSLIM letters — not fundamental data.
    """

    C: int = Field(ge=0, le=2)  # Weekly price change (current momentum proxy)
    A: int = Field(ge=0, le=2)  # Position within 52w range (annual strength proxy)
    N: int = Field(ge=0, le=2)  # Proximity to 52w high (new-high proxy)
    S: int = Field(ge=0, le=2)  # Relative volume (supply & demand proxy)
    L: int = Field(ge=0, le=2)  # RSI (leader vs laggard proxy)
    I: int = Field(ge=0, le=2)  # Weinstein stage / SMA structure (institutional proxy)
    M: int = Field(ge=0, le=2)  # SMA alignment stack (market direction proxy)

    @property
    def total(self) -> int:
        """Sum of all component scores (max 14)."""
        return self.C + self.A + self.N + self.S + self.L + self.I + self.M


class CANSLIMScore(BaseModel):
    """True CANSLIM scores using fundamental and market data, each 0–2 (max 14).

    Based on the O'Neil CAN SLIM methodology (https://en.wikipedia.org/wiki/CAN_SLIM).
    Missing fundamental data scores 0 for that letter.
    """

    C: int = Field(ge=0, le=2)  # Current quarterly EPS growth ≥25% YoY
    A: int = Field(ge=0, le=2)  # Annual earnings quality — return on equity ≥17%
    N: int = Field(ge=0, le=2)  # New highs — proximity to 52w high (breakout proxy)
    S: int = Field(ge=0, le=2)  # Supply & demand — relative volume on up-days
    L: int = Field(ge=0, le=2)  # Leader — 12m price return vs S&P 500
    I: int = Field(ge=0, le=2)  # Institutional sponsorship — % held by institutions
    M: int = Field(ge=0, le=2)  # Market direction — S&P 500 in confirmed uptrend

    @property
    def total(self) -> int:
        """Sum of all component scores (max 14)."""
        return self.C + self.A + self.N + self.S + self.L + self.I + self.M


class StockAnalysis(BaseModel):
    score: int = Field(ge=1, le=10)
    stage: str
    near_entry: bool
    entry_price: float | None = None
    stop_loss: float | None = None
    canslim: CANSLIMScore | None = None    # Fundamental CANSLIM score
    momentum: MomentumScore | None = None  # Technical momentum score
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
