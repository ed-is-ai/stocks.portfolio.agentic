## ADDED Requirements

### Requirement: Trader Agent SHALL execute buy orders for analysis-recommended stocks
Trader Agent reads analysis_results.json, filters for recommended_action="BUY", and submits buy orders to broker API (Alpaca). Order execution includes position sizing based on risk management rules.

#### Scenario: Execute buy order on high-conviction signal
- **WHEN** Analyst recommends "BUY" with score 8-10 for AAPL at $180
- **THEN** Trader Agent:
  - Calculates position size based on stop-loss level (handle_low at $170)
  - Risk per trade: 2% of portfolio (configurable)
  - Position size: portfolio_size × 0.02 / (entry_price - stop_price) shares
  - Submits BUY order to Alpaca API
  - Order type: market order or limit order (configurable)
  - Creates Position record with entry_date, entry_price, shares

#### Scenario: Suppress buy order if portfolio already holds position
- **WHEN** Analyst recommends "BUY" for ticker already in open positions
- **THEN** Trader Agent:
  - Checks positions database
  - Skips order (existing position, may average up if configured)
  - Logs: "AAPL already held, skipping new order"

### Requirement: Trader Agent SHALL execute sell orders for analysis-recommended exits
When Analyst recommends "SELL" or stop-loss is triggered, Trader Agent submits sell order to close position and realize P&L.

#### Scenario: Execute sell order on Analyst recommendation
- **WHEN** Analyst recommends "SELL" for open AAPL position at $200
- **THEN** Trader Agent:
  - Finds matching open Position record
  - Calculates realized P&L: (exit_price - entry_price) × shares
  - Submits SELL order for all shares
  - Updates Position record: exit_date, exit_price, status="closed"

#### Scenario: Execute stop-loss order below support level
- **WHEN** stock price drops below handle_low (stop-loss level)
- **THEN** Trader Agent:
  - Detects breach (price < handle_low from real-time data or end-of-day check)
  - Submits SELL order immediately (market order to ensure fill)
  - Logs: "Stop triggered on AAPL at $168 (handle_low=$170)"

### Requirement: Trader Agent SHALL enforce portfolio-level risk limits
Trader Agent restricts position sizing to prevent excessive concentration. Limits: (1) no single position >5% of portfolio, (2) max loss per trade = 2% of portfolio, (3) max total open positions = 10.

#### Scenario: Reject order due to concentration limit
- **WHEN** buy signal for large-cap stock + portfolio is 10% AAPL already
- **THEN** Trader Agent:
  - Calculates new position size
  - Detects would exceed 5% limit
  - Rejects order
  - Logs: "Order rejected: AAPL position would exceed 5% concentration"

#### Scenario: Reject order due to max loss constraint
- **WHEN** buy signal for very volatile stock with wide stop (5% risk per trade)
- **THEN** Trader Agent:
  - Calculates max loss: position_size × (entry - stop)
  - Detects exceeds 2% portfolio limit
  - Reduces position size to comply
  - OR rejects if minimum viable position size still exceeds limit

### Requirement: Trader Agent SHALL manage open positions and track P&L
Trader Agent maintains Position records for all active and closed trades. Each Position tracks entry/exit timing, quantity, prices, and realized/unrealized P&L. Daily, Trader calculates total portfolio value from current prices.

#### Scenario: Track open position and unrealized P&L
- **WHEN** AAPL entered at $180, currently $190
- **THEN** Position record shows:
  - ticker: "AAPL"
  - entry_date: "2025-02-01"
  - entry_price: 180.00
  - shares: 50
  - status: "open"
  - unrealized_pnl: (190 - 180) × 50 = $500

#### Scenario: Close position and lock realized P&L
- **WHEN** AAPL exits at $195
- **THEN** Position record updated:
  - exit_date: "2025-02-05"
  - exit_price: 195.00
  - status: "closed"
  - realized_pnl: (195 - 180) × 50 = $750
  - unrealized_pnl: 0

### Requirement: Trader Agent SHALL persist positions to database and update portfolio value log
All trades are recorded to a positions database (SQLite or equivalent). Daily portfolio snapshots are appended to portfolio_value.csv with total portfolio value, open positions count, and P&L breakdown.

#### Scenario: Write position to database
- **WHEN** buy order executes
- **THEN** INSERT to positions table:
  - id: auto-increment
  - ticker, entry_date, entry_price, shares, status="open", unrealized_pnl

#### Scenario: Write portfolio snapshot to CSV
- **WHEN** day ends or pipeline completes
- **THEN** APPEND to portfolio_value.csv:
  - timestamp: ISO date
  - total_value: sum of all position values + cash
  - open_positions: count of status="open"
  - closed_today: count of positions closed this day
  - realized_pnl_today: sum of realized_pnl for closed positions
  - unrealized_pnl_total: sum of unrealized_pnl for all open positions

### Requirement: Trader Agent SHALL support both market and limit order types
Trader Agent can execute orders via market orders (immediate execution at best price) or limit orders (specified price, may not fill). Entry orders are typically limit (control entry price); exit orders (stops, profit targets) may be market (ensure exit).

#### Scenario: Buy via limit order at entry zone
- **WHEN** entry zone is $840-850 and current price is $852
- **THEN** Trader Agent:
  - Submits limit buy order for middle of entry zone: $845
  - May not fill if price rises above zone
  - If filled, entry_price = $845 (actual execution price)

#### Scenario: Sell via market order for stop-loss
- **WHEN** stop-loss triggered
- **THEN** Trader Agent:
  - Submits market sell order (no limit)
  - Ensures execution even if price gaps through stop
  - Actual exit_price = market price at execution

### Requirement: Trader Agent SHALL define extension points for new broker integrations
Current integration is Alpaca API. To support new brokers (Interactive Brokers, TD Ameritrade, etc.), follow this pattern: (1) create broker_adapter.py with standard interface (place_order, get_positions, etc.), (2) add broker configuration to settings, (3) swap adapter at runtime.

#### Scenario: Add Interactive Brokers support
- **WHEN** developer wants Trader to support Interactive Brokers
- **THEN** creates ib_adapter.py implementing IBrokerAdapter interface:
  - place_order(side, ticker, qty, price, order_type) → order_id
  - get_positions() → list of Position
  - get_cash() → float
- **AND** updates config: BROKER="ib" or BROKER="alpaca"
- **AND** Trader uses selected adapter at runtime
- **AND** no changes to position sizing, risk management, or P&L logic

### Requirement: Trader Agent SHALL handle broker API failures gracefully
If Alpaca API is unreachable or returns error, Trader logs failure and does not execute order. Failed orders are retried on next pipeline run or flagged for manual review.

#### Scenario: Broker API timeout on order submission
- **WHEN** Alpaca API times out during buy order
- **THEN** Trader Agent:
  - Catches exception
  - Logs: "Failed to place order for AAPL: API timeout"
  - Skips order (does not create Position record)
  - Next run retries signal if Analyst re-recommends

#### Scenario: Insufficient cash to cover order
- **WHEN** buy signal arrives but portfolio cash < required margin
- **THEN** Broker API returns error
- **AND** Trader Agent catches, logs, and skips order
- **AND** alerts user via Alert Agent (portfolio is under-capitalized)

### Requirement: Trader Agent SHALL support dry-run mode for testing
Trader Agent can run in dry-run mode: calculates orders but does not submit them to broker. Useful for backtesting or paper trading.

#### Scenario: Dry-run order simulation
- **WHEN** Trader configured with dry_run=true
- **THEN** Agent:
  - Calculates position size and order details as normal
  - Logs to debug: "DRY RUN: would place order for 50 AAPL at $180"
  - Creates Position record with simulated entry/exit
  - Never calls Alpaca API
  - Results saved for analysis/backtesting

### Requirement: Trader Agent architecture constraints
Trader Agent depends on Analyst Agent (reads analysis_results.json) and broker API (Alpaca). Constraints: (1) Input MUST be valid StockAnalysis objects (Analyst dependency), (2) Position records MUST persist across runs (enable continuity), (3) recommended_action values MUST be BUY/SELL (exact match, filters fail otherwise), (4) Entry/exit prices come from Analyst (high_base, handle_low) or current market price (must never invent prices).

#### Scenario: Constraint: analysis_results dependency
- **WHEN** Analyst stops producing analysis_results.json
- **THEN** Trader has no signals and no orders execute
- **AND** must ensure Analyst runs before Trader

#### Scenario: Constraint: recommended_action must be exact match
- **WHEN** Analyst introduces new action like "AVERAGE_UP"
- **THEN** Trader filters for "BUY" and "SELL" only
- **AND** new action is ignored (must update Trader filter if intentional)
