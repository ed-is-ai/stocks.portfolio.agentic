## ADDED Requirements

### Requirement: Extraction Agent SHALL aggregate watchlist from multiple sources
Extraction Agent pulls stock tickers from multiple external screeners and sources (TradingView, StockTwits, WhaleWisdom), deduplicates, and outputs a union watchlist. Each ticker is tagged with source membership for downstream filtering.

#### Scenario: Combine tickers from 3 sources
- **WHEN** Extraction Agent runs
- **THEN** fetches:
  - TradingView screener: 500 S&P 500 stocks meeting Stage 2 criteria (price > SMA200, SMA50 > SMA150, within 35% of 52w high)
  - StockTwits trending: 200 stocks with high social sentiment
  - WhaleWisdom: 150 stocks with recent institutional buying
- **AND** deduplicates (e.g., AAPL appears in all 3, counted once)
- **AND** outputs union of ~700 unique tickers
- **AND** tags each: AAPL {source: ["tv_screener", "stocktwits", "whale_wisdom"]}

#### Scenario: Handle source-specific filtering
- **WHEN** developer wants only TradingView + WhaleWisdom (skip StockTwits)
- **THEN** Extraction Agent:
  - Accepts source configuration
  - Fetches from configured sources only
  - Outputs union of selected sources
  - Source tags reflect configuration

### Requirement: Extraction Agent SHALL track institutional buying context from WhaleWisdom
For stocks sourced from WhaleWisdom (top holdings of tracked hedge funds), Extraction Agent associates metadata: which filers increased positions, which decreased, rank by conviction. This context is passed to Scanner for enrichment.

#### Scenario: WhaleWisdom institutional context
- **WHEN** AAPL is in top holdings of Warren Buffett + 5 other tracked filers
- **THEN** Extraction Agent:
  - Fetches WhaleWisdom data: filer names, position changes (buying/selling)
  - Creates ww_context.json entry: {"AAPL": {filers_increasing: 5, filers_decreasing: 1, rank: 1}}
  - Scanner later uses this to set funds_buying, funds_selling, funds_net

### Requirement: Extraction Agent SHALL output watchlist in JSON format
Results are persisted to extraction_results.json in two possible formats: (1) array of strings (simple list), or (2) dict mapping source name to list of tickers (grouped by source).

#### Scenario: Simple list output format
- **WHEN** Extraction Agent configured for simple output
- **THEN** writes extraction_results.json as:
  ```json
  ["AAPL", "MSFT", "TSLA", ..., "XYZ"]
  ```

#### Scenario: Grouped output format
- **WHEN** Extraction Agent configured for source-grouped output
- **THEN** writes extraction_results.json as:
  ```json
  {
    "tv_screener": ["AAPL", "MSFT", "TSLA", ...],
    "stocktwits": ["NVDA", "AMC", ...],
    "whale_wisdom": ["BRK.B", "AAPL", ...]
  }
  ```

### Requirement: Extraction Agent SHALL enforce minimum quality gates per source
Each source has configurable minimum criteria (e.g., StockTwits must have sentiment threshold, TradingView must meet technical criteria). Tickers not meeting minimum gates are excluded.

#### Scenario: TradingView screener quality gate
- **WHEN** TradingView returns 600 candidates
- **THEN** Extraction Agent filters by:
  - Price > SMA200 (in uptrend)
  - SMA50 > SMA150 (momentum)
  - Within 35% of 52w high (not extended)
- **AND** retains ~500 meeting all criteria
- **AND** excludes ~100 that fail gates

#### Scenario: StockTwits sentiment gate
- **WHEN** StockTwits trending stocks include low-conviction picks
- **THEN** Extraction Agent:
  - Filters by minimum bullish sentiment threshold (e.g., >60% bullish)
  - Filters by minimum discussion volume (e.g., >1000 posts)
  - Retains highest-conviction stocks only

### Requirement: Extraction Agent SHALL handle API failures and maintain fallback watchlist
If an external source is unavailable, Extraction Agent gracefully excludes that source and continues with available sources. A fallback watchlist (static or cached) can be used if all sources fail.

#### Scenario: TradingView API unavailable
- **WHEN** TradingView API returns error
- **THEN** Extraction Agent:
  - Logs warning: "TradingView fetch failed, excluding from union"
  - Continues with StockTwits + WhaleWisdom
  - Outputs watchlist from 2 sources instead of 3

#### Scenario: All sources unavailable, use fallback
- **WHEN** all 3 sources are down
- **THEN** Extraction Agent:
  - Checks for fallback watchlist (e.g., previous day's results, hardcoded list)
  - Uses fallback if available
  - Logs: "All sources unavailable, using fallback watchlist"
  - Alerts user (portfolio update delayed)

### Requirement: Extraction Agent SHALL track extraction history and quality metrics
Each extraction run is logged with source availability, tickers added/removed, and extraction timestamp. This enables tracking of watchlist evolution and data source health.

#### Scenario: Log extraction metadata
- **WHEN** Extraction Agent completes run
- **THEN** appends to extraction_history.log:
  - timestamp: ISO datetime
  - sources_available: ["tv_screener", "stocktwits", "whale_wisdom"]
  - total_tickers: 700
  - new_tickers: 50
  - removed_tickers: 30
  - status: "success"

### Requirement: Extraction Agent SHALL define extension points for new sources
To add a new watchlist source (e.g., Finviz, FinanceAI screener, custom API), follow this pattern: (1) implement source_fetcher.py with get_tickers() method, (2) register in Extraction Agent configuration, (3) add to extraction pipeline.

#### Scenario: Add Finviz screener source
- **WHEN** developer wants to include Finviz stock list
- **THEN** creates finviz_fetcher.py with:
  - get_tickers() → list of strings
  - get_quality_gates() → list of filter criteria
  - get_context() → optional metadata per ticker
- **AND** registers in config: sources = ["tv_screener", "stocktwits", "whale_wisdom", "finviz"]
- **AND** Extraction Agent adds Finviz tickers to union automatically
- **AND** no changes to deduplication, quality gate, or output logic

### Requirement: Extraction Agent output SHALL be source-tagged for downstream use
Each ticker in the extraction_results.json includes source tag(s) indicating which source(s) included it. Scanner uses these tags (in_stocktwits, in_whale_wisdom, etc.) to annotate StockRecord for downstream analysis.

#### Scenario: Source tags guide filtering
- **WHEN** Analyst wants to analyze only WhaleWisdom picks
- **THEN** can filter StockRecord.in_whale_wisdom == true
- **AND** limits analysis to high-conviction institutional picks

### Requirement: Extraction Agent architecture constraints
Extraction Agent depends on external APIs (TradingView, StockTwits, WhaleWisdom) and produces no dependencies on downstream agents (Scanner consumes output but can operate with any watchlist format). Constraints: (1) Output MUST be JSON file at agents/extraction/extraction_results.json (Scanner hardcodes this path), (2) Source tags MUST match Scanner's field names (in_stocktwits, in_whale_wisdom, etc.), (3) Deduplication is required (no duplicate tickers in output), (4) All external API failures MUST be handled gracefully (no pipeline crash).

#### Scenario: Constraint: output file path
- **WHEN** developer moves extraction_results.json to different location
- **THEN** Scanner fails to load (hardcoded path)
- **AND** entire pipeline breaks
- **AND** file location is non-negotiable

#### Scenario: Constraint: source tag naming
- **WHEN** Extraction Agent changes tag name (e.g., in_whale_wisdom → in_ww)
- **THEN** Scanner cannot parse tag (hardcodes "in_whale_wisdom")
- **AND** source flags are lost downstream
- **AND** tag names must match Scanner's field definitions exactly
