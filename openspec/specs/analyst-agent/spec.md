## ADDED Requirements

### Requirement: Analyst Agent SHALL score stocks using CANSLIM methodology
Analyst evaluates each stock against William O'Neil's CANSLIM framework (Current earnings, Annual growth, New products, Supply/demand, Leader, Institutional, Market). Each component is scored 0-10, with overall score as weighted average.

#### Scenario: Score strong growth stock on CANSLIM
- **WHEN** Analyst processes stock with strong fundamentals
- **THEN** evaluates:
  - C (Current): earnings growth this quarter (0-10 scale)
  - A (Annual): 3-year EPS CAGR (0-10 scale)
  - N (New): innovation/new product catalysts (0-10 scale)
  - S (Supply/Demand): institutional ownership, insider buying (0-10 scale)
  - L (Leader): relative strength vs. sector (0-10 scale)
  - I (Institutional): 13F filer concentration, funds buying (0-10 scale)
  - M (Market): market timing, SPY alignment (0-10 scale)
- **AND** computes overall score as weighted average (weights per official CANSLIM methodology)
- **AND** generates canslim_score object with all 7 component scores

#### Scenario: Reject stock with weak earnings
- **WHEN** stock has declining earnings or low C score
- **THEN** overall score capped at 5-6 regardless of other strengths
- **AND** "strengths" field emphasizes earnings issue
- **AND** recommended_action may be "WATCH" pending next earnings

### Requirement: Analyst Agent SHALL classify stock by Weinstein Stage (1-4)
Analyst determines which stage of market cycle stock is in: Stage 1 (accumulation/basing), Stage 2 (uptrend), Stage 3 (distribution), Stage 4 (downtrend). Classification guides entry/exit decisions.

#### Scenario: Identify Stage 2 stock approaching breakout
- **WHEN** Analyst detects: price above SMA50, SMA50 > SMA150, price > SMA200, within 35% of 52w high
- **THEN** classifies as "Stage 2" (uptrend)
- **AND** notes stock is in ideal zone for entry (trending higher but not extended)

#### Scenario: Identify Stage 1 stock in accumulation
- **WHEN** Analyst detects: price oscillating in base, low institutional ownership, forming saucer pattern
- **THEN** classifies as "Stage 1" (accumulation)
- **AND** notes stock is early-stage, higher risk, opportunity if breakout succeeds

### Requirement: Analyst Agent SHALL detect VCP (Volatility Contraction Pattern) for entry timing
Analyst uses VCP screener skill to identify VCP setups: stocks with declining volatility (ATR shrinking) and consolidating price (contraction), signaling imminent breakout. Pattern detection calls vcp-screener calculator modules directly.

#### Scenario: Detect VCP pattern with entry zone
- **WHEN** Analyst processes StockRecord with sufficient data (252+ trading days)
- **THEN** calls vcp-screener modules:
  - calculate_trend_template() → evaluates 7-point Minervini trend template
  - calculate_vcp_pattern() → detects contraction and computes pivot price
  - calculate_volume_pattern() → checks dry-up and breakout volume
  - calculate_pivot_proximity() → computes stop-loss below contraction low
  - compute_execution_state() → classifies as "Pre-breakout", "Breakout", "Failed", etc.
- **AND** stores momentum_score with pattern details (contraction_depth, base_length, breakout estimate)

#### Scenario: Stock not suitable for VCP entry
- **WHEN** stock is extended (>35% above 52w high) or in downtrend
- **THEN** entry_zone is "extended" or "far"
- **AND** recommended_action does not trigger buy alert (wait for pullback)

### Requirement: Analyst Agent SHALL determine entry zone (actionable proximity to entry)
Analyst classifies stock's distance to optimal entry as: "broken_out" (breakout just occurred), "approaching" (1-2 weeks to entry), "getting_close" (2-4 weeks), "extended" (above entry, risk/reward poor), "far" (too far away, low near-term probability).

#### Scenario: "Approaching" entry classification
- **WHEN** stock at 95-99% of breakout level (high_base)
- **THEN** entry_zone = "approaching"
- **AND** confidence is high that breakout is imminent
- **AND** Alert Agent triggers alert (actionable for trader)

#### Scenario: "Extended" classification
- **WHEN** stock >10% above high_base
- **THEN** entry_zone = "extended"
- **AND** risk/reward is poor
- **AND** recommended_action is "HOLD" (wait for pullback)

### Requirement: Analyst Agent SHALL assign actionable recommendation
Based on score and entry_zone, Analyst assigns: "BUY" (immediate entry candidate), "WATCH" (monitor for entry), "HOLD" (good stock but poor timing), "SELL" (exit existing position).

#### Scenario: High-conviction buy
- **WHEN** score 8-10 AND entry_zone is "approaching" or "broken_out"
- **THEN** recommended_action = "BUY"
- **AND** Alert Agent generates buy alert

#### Scenario: Good stock but poor timing
- **WHEN** score 7-8 AND entry_zone is "extended"
- **THEN** recommended_action = "HOLD"
- **AND** Alert Agent suppresses alert (wait for pullback)

### Requirement: Analyst Agent SHALL explain reasoning with strengths and risks
Each analysis includes 2-3 key strengths (why score is high) and 1-2 key risks (potential issues), plus one-sentence summary.

#### Scenario: Clear strengths-and-risks narrative
- **WHEN** Analyst scores stock 8/10 in Stage 2
- **THEN** generates:
  - strengths: ["Earnings accelerating (C=9)", "Institutional buying (S=9)", "Breaking above 52w high"]
  - risks: ["Extended above 50-day MA", "Sector facing headwinds"]
  - summary: "Strong earnings growth and institutional support; approaching resistance but breakout likely."

### Requirement: Analyst Agent SHALL handle fallback to rule-based scoring if historical data unavailable
If ohlcv_history is missing (e.g., new IPO, yfinance failure), Analyst falls back to rule-based logic using available technicals and fundamentals, scoring lower confidence.

#### Scenario: Rule-based score for IPO with no price history
- **WHEN** stock has price/volume but no 252-day history
- **THEN** Analyst:
  - Skips VCP pattern detection (requires history)
  - Scores based on available fundamentals (C, A, N, I, M) only
  - overall score capped at 6 (lower confidence without technicals)
- **AND** entry_zone defaults to "getting_close" (conservative estimate)

### Requirement: Analyst Agent output SHALL be persisted as JSON and Excel
Analyst writes analysis results to agents/analyst/analysis_results.json (for Alert/Trader agents) and analysis_results.xlsx (human-readable report).

#### Scenario: Analysis results persisted
- **WHEN** Analyst completes analysis of 80 stocks
- **THEN** writes agents/analyst/analysis_results.json containing array of 80 StockAnalysis objects
- **AND** writes agents/analyst/analysis_results.xlsx with columns: ticker, score, stage, entry_zone, recommended_action, summary
- **AND** Excel includes color-coding (green for BUY, yellow for WATCH, red for SELL)

### Requirement: Analyst Agent SHALL define extension points for new scoring methods
To add a new scoring framework (e.g., quality score, dividend metrics, growth profiling), follow this pattern: (1) define new_score.py with ScoreCalculator class, (2) add calculation call to Analyst main loop, (3) store result as new StockAnalysis field, (4) update weight in overall score if needed.

#### Scenario: Add quality score dimension
- **WHEN** developer wants to add balance-sheet quality metrics
- **THEN** creates quality_calculator.py with calculate_quality_score(stock_data) method
- **AND** adds call to Analyst.analyze(): `quality_score = quality_calc.calculate(...)`
- **AND** stores in StockAnalysis.quality_score field
- **AND** can incorporate into overall score weighting without changing existing CANSLIM logic

### Requirement: Analyst Agent architecture constraints
Analyst depends on Scanner Agent (reads scan_results.json) and VCP/BTP skill modules. Constraints: (1) Input MUST be valid StockRecord objects with ohlcv_history in oldest→newest order (Scanner dependency), (2) Output MUST be StockAnalysis objects (Alert/Trader depend on this schema), (3) VCP calculations are non-negotiable for Stage 2/3 stocks (replacing this logic requires design review), (4) recommended_action values MUST be one of: BUY, WATCH, HOLD, SELL (Alert Agent filters on these exact strings).

#### Scenario: Constraint: input dependency on Scanner schema
- **WHEN** Scanner changes StockRecord fields (e.g., renames ohlcv_history to daily_ohlc)
- **THEN** Analyst fails to load (JSON parse error or field error)
- **AND** must coordinate schema change with Analyst team

#### Scenario: Constraint: output dependency by downstream agents
- **WHEN** developer changes recommended_action value (e.g., to "STRONG_BUY")
- **THEN** Alert Agent filters fail (expects exact string match)
- **AND** change breaks alert logic, must be coordinated with Alert team
