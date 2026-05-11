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
    sector: str | None = None        # GICS sector from yfinance (e.g. "Technology")
    pct_from_52w_high: float
    pct_change_week: float
    # Fundamental data (fetched from yfinance Ticker.info)
    eps_growth: float | None = None          # Current quarter EPS growth YoY (0.25 = 25%)
    annual_eps_growth: float | None = None   # 3-year annual EPS CAGR (0.25 = 25%)
    roe: float | None = None                 # Return on equity (0.17 = 17%)
    inst_ownership_pct: float | None = None  # Institutional ownership fraction (0.0–1.0)
    pe_ratio: float | None = None            # Trailing twelve-month P/E ratio
    inst_count: int | None = None            # Total number of institutions holding shares (13F)
    funds_buying: int | None = None          # WhalWisdom: # filers that increased position last quarter
    funds_selling: int | None = None         # WhalWisdom: # filers that decreased position last quarter
    funds_net: int | None = None             # WhalWisdom: funds_buying minus funds_selling
    congress_buys: int | None = None          # QuiverQuant: congressional buy transactions last 12 months
    congress_sells: int | None = None         # QuiverQuant: congressional sell transactions last 12 months
    senate_buys: int | None = None            # QuiverQuant: Senate-only buy transactions last 12 months
    senate_sells: int | None = None           # QuiverQuant: Senate-only sell transactions last 12 months
    # Market context
    rel_strength_vs_spy: float | None = None  # Stock 52w return minus SPY 52w return (pct pts)
    spy_uptrend: bool | None = None           # True if SPY is above its 200-day SMA
    # Daily OHLCV history in FMP-compatible format (most recent first)
    # Each entry: {"date": "YYYY-MM-DD", "open": f, "high": f, "low": f, "close": f, "volume": i}
    ohlcv_history: list[dict[str, float | int | str]] = []
    # Watchlist source flags
    in_stocktwits: bool = False
    in_whale_wisdom: bool = False
    # Screener source: "ww_extraction" | "vcp_screener" | "finviz_screener" | comma-joined if multiple
    source: str = ""


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
    A: int = Field(ge=0, le=2)  # Annual EPS CAGR ≥25% over 3 years (fallback: ROE ≥17%)
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
    entry_zone: str = "far"  # "broken_out"|"approaching"|"getting_close"|"extended"|"far"
    entry_price: float | None = None       # next entry: 0.5% above current VCP pivot
    prev_entry_price: float | None = None  # prior pivot: T1 high (top of base)
    stop_loss: float | None = None
    risk_pct: float | None = None                 # (entry - stop) / entry, e.g. 0.06 = 6%
    r_multiples: dict[str, float] | None = None   # {"1.0R": x, "2.0R": y, "3.0R": z}
    reward_risk_ratio: float | None = None         # 2.0 targeting 2R by convention
    volume_confirmed: bool = False         # rel_volume >= 1.4 at scan time
    fresh_breakout: bool = False           # zone just transitioned to broken_out this run
    multiyear_breakout: bool = False       # price 0-8% above a significant multi-year base pivot
    multiyear_pivot: float | None = None   # the historical pivot level being broken
    sepa_template: dict[str, bool] | None = None  # 8 Minervini trend template conditions
    canslim: CANSLIMScore | None = None    # Fundamental CANSLIM score
    momentum: MomentumScore | None = None  # Technical momentum score
    strengths: list[str] = []
    risks: list[str] = []
    summary: str


class StockRecord(StockScan):
    analysis: StockAnalysis | None = None


class ExitSignal(BaseModel):
    """Exit or add signal for a held position."""

    action: str  # "EXIT" | "ADD"
    reason: str


class EmailConfig(BaseModel):
    host: str
    port: int
    user: str
    password: str
    recipient: str


class Trade(BaseModel):
    """A single recorded buy or sell transaction."""

    id: int | None = None
    ticker: str
    action: str  # "BUY" or "SELL"
    shares: float
    price: float
    date: str
    notes: str = ""
    stop_loss: float | None = None    # initial stop from analysis at time of entry
    entry_price: float | None = None  # analyst pivot entry price


class Position(BaseModel):
    """An open portfolio position with live P&L and Minervini exit metrics."""

    ticker: str
    shares: float
    avg_cost: float
    current_price: float | None = None
    total_cost: float
    current_value: float | None = None
    unrealised_pnl: float | None = None
    unrealised_pnl_pct: float | None = None
    entry_price: float | None = None       # pivot entry from first BUY
    entry_date: str | None = None          # date of first BUY
    stop_loss: float | None = None         # initial stop from first BUY
    profit_target_20: float | None = None  # entry_price * 1.20
    profit_target_25: float | None = None  # entry_price * 1.25
    next_pivot: float | None = None        # current analyst pivot entry (populated in web layer)
    exit_signal: ExitSignal | None = None  # populated by ExitEvaluator in web layer
    price_currency: str = "GBP"            # ISO currency code for current_price/current_value/pnl
