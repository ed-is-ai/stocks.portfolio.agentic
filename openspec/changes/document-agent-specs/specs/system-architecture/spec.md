## ADDED Requirements

### Requirement: System shall orchestrate 5 autonomous agents on market schedule
The Agents.Stocks system SHALL coordinate 5 independent agents (Scanner, Analyst, Alert, Trader, Extraction) in a fixed sequence during market hours. Each run executes: Extraction → Scanner → Analyst → Alert → Trader, with each agent consuming output of the previous stage.

#### Scenario: Successful pipeline execution during market hours
- **WHEN** orchestrator triggers on market schedule (9:30 AM - 4:00 PM ET, weekdays)
- **THEN** agents execute in sequence: Extraction → Scanner → Analyst → Alert → Trader
- **AND** each agent's output becomes the next agent's input
- **AND** run duration, counts, and status are logged to pipeline_runs.csv

#### Scenario: Skipped runs outside market hours
- **WHEN** orchestrator trigger fires outside market hours (before 9:30 AM or after 4:00 PM ET, or weekends)
- **THEN** run is logged with status="skipped" to pipeline_runs.csv
- **AND** no agents execute

### Requirement: System SHALL maintain data lineage across agent pipeline
Data produced by each agent (scan results, analysis, alerts) SHALL be persisted to JSON files in agent-specific directories, forming an immutable audit trail. Each file is timestamped and corresponds to one orchestrator run.

#### Scenario: Scan results persisted and available to analyst
- **WHEN** Scanner Agent completes
- **THEN** results are written to agents/scanner/scan_results.json
- **AND** file contains array of StockRecord objects
- **AND** Analyst Agent reads this file as its input

#### Scenario: Analysis results persisted for alert and trader
- **WHEN** Analyst Agent completes
- **THEN** results are written to agents/analyst/analysis_results.json and .xlsx
- **AND** file contains array of StockAnalysis objects
- **AND** Alert and Trader agents read this file as input

### Requirement: System SHALL track pipeline execution history
The orchestrator SHALL maintain a CSV log (pipeline_runs.csv) with one row per execution, recording start time, end time, duration, agent results (count of stocks scanned, analyzed, buy alerts, sell alerts), data sources used, and execution status (success/skipped/error).

#### Scenario: Successful run logged with metrics
- **WHEN** pipeline completes successfully
- **THEN** orchestrator appends row to pipeline_runs.csv with:
  - start: ISO timestamp
  - end: ISO timestamp
  - duration_seconds: integer
  - scanned: integer (count of stocks)
  - analysed: integer (count analyzed)
  - buy_alerts: integer (count of buy signals)
  - sell_alerts: integer (count of sell signals)
  - sources: comma-separated list (ww_extraction, vcp_screener, tv_screener, etc.)
  - status: "success"
  - errors: empty string

#### Scenario: Skipped run logged without metrics
- **WHEN** pipeline is skipped (outside market hours)
- **THEN** orchestrator appends row with:
  - start/end: same timestamp
  - duration_seconds: 0
  - other counts: 0
  - status: "skipped"

### Requirement: System SHALL manage portfolio value tracking
The system SHALL maintain a CSV log (portfolio_value.csv) recording portfolio total value at key intervals (end of each trading day, or each pipeline run), with timestamp, total value, and asset breakdown.

#### Scenario: Portfolio snapshot recorded after each pipeline run
- **WHEN** pipeline completes (whether it executed or was skipped)
- **THEN** trader agent (or equivalent) appends portfolio snapshot to portfolio_value.csv
- **AND** row includes: timestamp, total_value, positions_open, positions_closed, net_realized_pnl

### Requirement: System SHALL handle errors without crashing entire pipeline
If any agent fails, the orchestrator SHALL log the error, prevent subsequent agents from executing, and mark the run as failed. Failed runs remain queryable for debugging.

#### Scenario: Scanner failure halts pipeline
- **WHEN** Scanner Agent raises an exception
- **THEN** exception is caught and logged to pipeline_runs.csv with status="error"
- **AND** Analyst, Alert, and Trader agents do not execute
- **AND** run log contains error message and traceback

#### Scenario: Analyst failure allows Alert/Trader to skip gracefully
- **WHEN** Analyst Agent fails but Scanner succeeded
- **THEN** Alert and Trader agents receive empty analysis results
- **AND** pipeline status="error" but agents complete without crashing

### Requirement: System SHALL support multiple data source cohorts
The extraction agent can consume watchlists from multiple sources (TradingView, StockTwits, WhaleWisdom). Scanner applies union logic: any ticker appearing in any source is included. Source membership is tracked per ticker for analysis.

#### Scenario: Multi-source union creates combined watchlist
- **WHEN** Extraction Agent processes sources: TradingView (500 tickers), StockTwits (200 tickers), WhaleWisdom (150 tickers)
- **THEN** Scanner receives watchlist of ~700 tickers (union, deduplicated)
- **AND** source flags are stored: ticker.in_stocktwits=true if StockTwits included it

#### Scenario: New data source added to extraction
- **WHEN** developer adds new source integration to Extraction Agent (e.g., new screener)
- **THEN** tickers from new source are added to union automatically
- **AND** source flags are updated to track membership

### Requirement: System SHALL integrate with external APIs for data enrichment
Scanner, Analyst, and Extraction agents SHALL make calls to external services (yfinance for OHLCV, FMP API for fundamentals, Alpha Vantage for earnings, Congress API for insider trades, WhaleWisdom for institutional flow). Failures in external APIs SHALL degrade gracefully (missing fields, not blocking entire scan).

#### Scenario: yfinance unavailable does not block scan
- **WHEN** yfinance API is unreachable
- **THEN** Scanner catches exception and sets price/volume fields to null
- **AND** scan continues with available data

#### Scenario: FMP API rate limit is respected
- **WHEN** FMP API rate limit is hit
- **THEN** Scanner throttles requests and retries with backoff
- **AND** missing fundamental data is recorded (field = null) rather than failing
