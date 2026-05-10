## ADDED Requirements

### Requirement: Scanner Agent SHALL fetch price and volume data via yfinance
Scanner Agent retrieves OHLCV (open, high, low, close, volume) data from yfinance for all tickers in the watchlist. It computes 52-week highs/lows, weekly price history, and volume moving averages.

#### Scenario: Scan 50 tickers and produce price data
- **WHEN** Scanner Agent receives watchlist of 50 tickers
- **THEN** for each ticker, fetches historical data from yfinance (252 trading days, plus 52-week extrema)
- **AND** computes price_history as array of 52 weekly closes (oldest → newest)
- **AND** computes vol_ma50 (50-day average volume)
- **AND** stores ohlcv_history as list of daily OHLCV dicts (most recent first)

#### Scenario: Handle yfinance API failures gracefully
- **WHEN** yfinance returns no data for a ticker (delisted, symbol error)
- **THEN** ticker is skipped with warning logged
- **AND** scan continues for remaining tickers

### Requirement: Scanner Agent SHALL compute technical indicators using pandas-ta
For each stock, Scanner calculates momentum and trend indicators: Simple Moving Averages (10, 30, 50, 150, 200-day), RSI(14), ATR(14).

#### Scenario: Full technical analysis on stock with complete history
- **WHEN** Scanner has 252+ trading days of OHLCV data
- **THEN** computes:
  - sma10, sma30, sma50, sma150, sma200 (simple moving averages)
  - rsi14 (14-period relative strength index)
  - atr14 (14-period average true range, for volatility)
- **AND** all values stored to StockRecord

#### Scenario: Partial technical analysis on stock with <252 days
- **WHEN** stock has <252 days of data (new IPO, recent listing)
- **THEN** computes available indicators on available data
- **AND** indicators requiring full period (e.g., sma200 if <200 days) are set to None

### Requirement: Scanner Agent SHALL enrich data with fundamentals from external APIs
Scanner integrates fundamental data from multiple sources: yfinance (EPS, ROE, P/E), Alpha Vantage (earnings surprises), Congress API (insider trading), WhaleWisdom (institutional flow).

#### Scenario: Fetch fundamentals from yfinance
- **WHEN** Scanner processes stock
- **THEN** calls yfinance Ticker.info for:
  - eps_growth (current quarter YoY)
  - annual_eps_growth (3-year CAGR, if available)
  - roe (return on equity)
  - pe_ratio (trailing twelve-month P/E)
  - inst_ownership_pct (institutional ownership %)
- **AND** stores as StockRecord fields (None if unavailable)

#### Scenario: Fetch insider trading from Congress API
- **WHEN** Scanner processes stock
- **THEN** calls Congress API for congressional/senate trades in past 12 months
- **AND** stores congress_buys, congress_sells, senate_buys, senate_sells
- **AND** counts represent number of transactions (not dollars)

#### Scenario: Fetch institutional flow from WhaleWisdom
- **WHEN** Extraction Agent provides WhaleWisdom context data
- **THEN** Scanner reads ww_context.json and associates funds_buying, funds_selling, funds_net
- **AND** context includes filer names/hedge fund identities

### Requirement: Scanner Agent SHALL track data source membership for each ticker
Scanner stores source flags indicating which screener/source included each ticker (TradingView, StockTwits, WhaleWisdom). This enables downstream filtering and provides context for analysis.

#### Scenario: Ticker appears in multiple sources
- **WHEN** watchlist includes ticker from TradingView AND StockTwits
- **THEN** StockRecord stores:
  - in_stocktwits: true
  - in_whale_wisdom: false
- **AND** Alert Agent can filter on source (e.g., "only alert for WhaleWisdom picks")

### Requirement: Scanner Agent SHALL compute market context (relative strength, SPY alignment)
Scanner calculates how stock performance compares to market (SPY). This provides market timing context for entry decisions.

#### Scenario: Compute relative strength vs SPY
- **WHEN** Scanner processes stock with full 52-week data
- **THEN** computes:
  - rel_strength_vs_spy: 52w stock return minus 52w SPY return (percentage points)
  - spy_uptrend: boolean (true if SPY > SPY 200-day SMA)
- **AND** stores in StockRecord

### Requirement: Scanner Agent SHALL compute VCP-specific pivots for entry/exit
Scanner pre-computes VCP-specific reference points used by Analyst for entry signal and stop-loss placement: high_base (highest daily high in past 10 weeks for entry reference) and handle_low (lowest daily low in past 3 weeks for stop placement).

#### Scenario: Compute pivot points for VCP entry
- **WHEN** Scanner has 252 days of daily OHLCV
- **THEN** computes:
  - high_base: max daily high in past 50 trading days (≈10 weeks)
  - handle_low: min daily low in past 15 trading days (≈3 weeks)
- **AND** stores as reference for breakout entry and stop calculation

### Requirement: Scanner Agent output SHALL be persisted as JSON file
Scanner writes all scan results to agents/scanner/scan_results.json, with each run appending a new array of StockRecord objects.

#### Scenario: Scan results written to JSON
- **WHEN** Scanner completes scan of 100 stocks
- **THEN** writes agents/scanner/scan_results.json containing array of 100 StockRecord objects
- **AND** file is valid JSON, parseable by downstream agents
- **AND** file can contain results from multiple runs (append semantics)

### Requirement: Scanner Agent SHALL define extension points for new data sources
To add a new data source (e.g., new fundamentals provider, new insider trading feed), follow this pattern: (1) implement a client class (see congress_client.py, alpha_vantage_client.py), (2) add data fetching call to Scanner main loop, (3) store results as new StockRecord fields.

#### Scenario: Add new earnings surprise data source
- **WHEN** developer wants to integrate earnings surprise provider
- **THEN** creates new_provider_client.py with fetch_surprises() method
- **AND** adds call to Scanner.scan() loop: `surprises = new_provider.fetch(...)`
- **AND** stores result to StockRecord.earnings_surprise_pct field
- **AND** no changes to Analyst, Alert, or Trader agents required

### Requirement: Scanner Agent architecture constraints
Scanner operates independently and has no direct dependencies on other agents. Constraints: (1) Output MUST be StockRecord objects (Analyst depends on this schema), (2) OHLCV history MUST be oldest→newest order (Analyst's technical calculations depend on this), (3) all_numeric fields MUST use float type (not int, not Decimal), (4) missing data MUST be None (not 0, not empty string).

#### Scenario: Constraint: output schema
- **WHEN** developer changes StockRecord schema (e.g., renames sma50)
- **THEN** Analyst Agent fails to load or crashes (tight coupling)
- **AND** must coordinate with Analyst team before schema changes

#### Scenario: Constraint: price history order
- **WHEN** developer reverses price_history to newest→oldest
- **THEN** Analyst's pandas-ta calculations produce incorrect results
- **AND** scores become invalid
