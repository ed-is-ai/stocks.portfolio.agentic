## ADDED Requirements

### Requirement: StockRecord SHALL represent price, volume, and technical data for a single stock scan
StockRecord is the primary data model produced by Scanner Agent. It represents a complete snapshot of a single stock at a point in time, including OHLCV data, computed technicals, and fundamental data. All numeric fields are floats unless otherwise specified; NoneType is acceptable for optional fields.

#### Scenario: StockRecord with full data
- **WHEN** Scanner Agent completes scan on a stock with all data sources available
- **THEN** StockRecord contains:
  - ticker: string (e.g., "AAPL")
  - as_of: ISO date string (e.g., "2025-02-15")
  - price: float (current price)
  - price_history: list of floats, oldest→newest (52 weeks of weekly closes)
  - sma10, sma30, sma50, sma150, sma200: float or None (simple moving averages)
  - rsi14, atr14: float or None (relative strength, average true range)
  - volume: int (current volume)
  - vol_ma50: int or None (50-day average volume)
  - rel_volume: float (current_volume / vol_ma50, decimal)
  - high_52w, low_52w: float (52-week range)
  - high_base: float or None (highest daily high in past 10 weeks, for VCP entry reference)
  - handle_low: float or None (lowest daily low in past 3 weeks, for stop-loss reference)
  - sector: string or None (GICS sector, e.g., "Technology")
  - pct_from_52w_high: float (how far below 52w high, as percentage, 0-1)
  - pct_change_week: float (week-over-week % change, decimal)
  - Fundamental data: eps_growth, annual_eps_growth, roe, inst_ownership_pct, pe_ratio (all float or None)
  - inst_count: int or None (number of institutional holders)
  - funds_buying, funds_selling, funds_net: int or None (WhaleWisdom institutional flow)
  - congress_buys, congress_sells, senate_buys, senate_sells: int or None (insider trading)
  - rel_strength_vs_spy, spy_uptrend: float or bool or None (market context)
  - ohlcv_history: list of dicts with date, open, high, low, close, volume (recent 252 trading days)
  - in_stocktwits, in_whale_wisdom: bool (source flags)

#### Scenario: StockRecord with partial data (API failures)
- **WHEN** Scanner Agent processes stock but some external APIs fail
- **THEN** StockRecord is created with available fields; missing fields are set to None
- **AND** ohlcv_history is preserved (required for technical analysis)
- **AND** fundamental fields (eps_growth, roe) can be None without invalidating scan

### Requirement: StockAnalysis SHALL represent analyst's assessment and actionable signal
StockAnalysis is produced by Analyst Agent and consumed by Alert/Trader agents. It contains a score (1-10), stage (1-4), entry recommendation, and risk/opportunity summary.

#### Scenario: High-conviction buy signal
- **WHEN** Analyst scores stock 8-10 and entry_zone is "approaching" or "broken_out"
- **THEN** StockAnalysis contains:
  - ticker: string
  - as_of: ISO date
  - score: int (8-10)
  - stage: string ("Stage 1" | "Stage 2" | "Stage 3" | "Stage 4")
  - entry_zone: string ("broken_out" | "approaching" | "getting_close" | "extended" | "far")
  - strengths: list of 2-3 bullet strings explaining score
  - risks: list of 1-2 bullet strings
  - summary: one-sentence summary
  - canslim_score: dict with component scores (C, A, N, S, L, I, M, each 0-10)
  - momentum_score: dict with stage and pattern details
  - recommended_action: string ("BUY" | "HOLD" | "SELL" | "WATCH")

#### Scenario: Weak signal that doesn't trigger alert
- **WHEN** Analyst scores stock 4-6 or entry_zone is "far"
- **THEN** StockAnalysis is created but recommended_action is "WATCH" or "HOLD"
- **AND** Alert Agent does not generate alert

### Requirement: Position SHALL represent open or closed trade
Position represents a single stock trade (long position). It tracks entry price, quantity, current value, realized/unrealized P&L, and exit details.

#### Scenario: Open long position
- **WHEN** Trader Agent executes buy order
- **THEN** Position is created with:
  - ticker: string
  - entry_date: ISO date
  - entry_price: float
  - shares: int
  - exit_date: None
  - exit_price: None
  - status: "open"
  - unrealized_pnl: float (current_price - entry_price) * shares
  - realized_pnl: None (not applicable for open)

#### Scenario: Closed position
- **WHEN** Trader Agent exits position via sell order
- **THEN** Position is updated with:
  - exit_date: ISO date
  - exit_price: float
  - status: "closed"
  - realized_pnl: float (exit_price - entry_price) * shares
  - unrealized_pnl: 0

### Requirement: CANSLIMScore SHALL break down CANSLIM component scores
CANSLIMScore provides granular scoring for each CANSLIM letter (C, A, N, S, L, I, M), each rated 0-10. Analyst uses these to justify overall score.

#### Scenario: CANSLIM breakdown for analysis
- **WHEN** Analyst evaluates stock
- **THEN** StockAnalysis.canslim_score contains:
  - c_current_earnings: float (0-10, earnings growth this quarter)
  - a_annual_growth: float (0-10, 3-year EPS CAGR)
  - n_new_product: float (0-10, innovation/new catalysts)
  - s_supply_demand: float (0-10, institutional ownership, insider buying)
  - l_leader: float (0-10, relative strength vs. sector)
  - i_institutional: float (0-10, 13F filer concentration)
  - m_market: float (0-10, market timing, SPY trend)
- **AND** overall score is derived as weighted average

### Requirement: MomentumScore SHALL detail stage and VCP pattern analysis
MomentumScore captures Weinstein Stage (1-4), VCP pattern details (contraction depth, base length), volume pattern, and entry proximity.

#### Scenario: Stage 2 breakout setup
- **WHEN** Analyst detects VCP pattern approaching breakout
- **THEN** StockAnalysis.momentum_score contains:
  - stage: int (2, indicating Stage 2)
  - pattern: string ("VCP", "Cup-with-Handle", "Flat-Base", etc.)
  - contraction_depth: float (% decline into support)
  - base_length_days: int (how long consolidation has lasted)
  - volume_pattern: dict (dry-up ratio, breakout volume %)
  - days_to_entry: int (estimated days until breakout at current volatility)
  - stop_below: float (pivot low for stop-loss)

### Requirement: EmailConfig SHALL configure SMTP credentials for alerts
EmailConfig encapsulates email server settings for Alert Agent to send notifications.

#### Scenario: Alert email delivery
- **WHEN** Alert Agent composes and sends email
- **THEN** uses EmailConfig containing:
  - host: string (SMTP server, e.g., "smtp.gmail.com")
  - port: int (SMTP port, e.g., 587)
  - user: string (email address)
  - password: string (SMTP password or app-specific token)
  - recipient: string (destination email address)
