## MODIFIED Requirements

### Requirement: Extraction Agent SHALL aggregate watchlist from multiple sources
Extraction Agent pulls stock tickers from multiple external screeners and sources (WhaleWisdom; StockTwits quarterly-curated data; future: TradingView), deduplicates, and outputs a union watchlist. Each ticker is tagged with source membership for downstream filtering.

#### Scenario: Combine tickers from WhaleWisdom and StockTwits
- **WHEN** Extraction Agent runs
- **THEN** fetches:
  - WhaleWisdom heat map: top 50 stocks by institutional conviction
  - StockTwits quarterly watchlist: top 25 momentum stocks by index (S&P 500, NASDAQ 100, Russell 2000)
- **AND** deduplicates across sources (e.g., MU appears in both, counted once)
- **AND** outputs union of ~75-125 unique tickers (size varies by overlap)
- **AND** tags each: MU {in_whale_wisdom: true, in_stocktwits: true} (if in both)

#### Scenario: Handle source-specific filtering
- **WHEN** developer wants to exclude a source (e.g., skip StockTwits)
- **THEN** Extraction Agent:
  - Can be configured to skip StockTwits load via config
  - Fetches from remaining configured sources only
  - Outputs union of selected sources
  - Source tags reflect configuration (in_stocktwits: false for skipped source)

#### Scenario: Multi-index StockTwits grouping
- **WHEN** Extraction Agent loads StockTwits data
- **THEN** tickers are organized in `extraction_results.json` by index:
  - "StockTwits Top 25 Momentum — S&P 500 (YYYY-MM-DD)"
  - "StockTwits Top 25 Momentum — NASDAQ 100 (YYYY-MM-DD)"
  - "StockTwits Top 25 Momentum — Russell 2000 (YYYY-MM-DD)"
- **AND** Scanner infers `in_stocktwits=True` for tickers in any StockTwits group

### Requirement: Extraction Agent SHALL track institutional buying context from WhaleWisdom
For stocks sourced from WhaleWisdom (top holdings of tracked hedge funds), Extraction Agent associates metadata: which filers increased positions, which decreased, rank by conviction. This context is passed to Scanner for enrichment.

#### Scenario: WhaleWisdom institutional context
- **WHEN** AAPL is in top holdings of Warren Buffett + 5 other tracked filers
- **THEN** Extraction Agent:
  - Fetches WhaleWisdom data: filer names, position changes (buying/selling)
  - Creates ww_context.json entry: {"AAPL": {filers_increasing: 5, filers_decreasing: 1, rank: 1}}
  - Scanner later uses this to set funds_buying, funds_selling, funds_net

### Requirement: Extraction Agent SHALL output watchlist in JSON format
Results are persisted to extraction_results.json in dict format mapping source name to list of tickers (grouped by source).

#### Scenario: Grouped output format with WhaleWisdom and StockTwits
- **WHEN** Extraction Agent completes a run with both sources available
- **THEN** writes extraction_results.json as:
  ```json
  {
    "StockTwits Top 25 Momentum — S&P 500 (2026-05-10)": ["SNDK", "INTC", ...],
    "StockTwits Top 25 Momentum — NASDAQ 100 (2026-05-10)": ["SNDK", "AMD", ...],
    "StockTwits Top 25 Momentum — Russell 2000 (2026-05-10)": ["MXIM", "RXT", ...],
    "WisdomWise Heat Map — WhaleScore v2.0 (2026-05-10)": ["FSLR", "AS", ...]
  }
  ```

#### Scenario: Quarterly StockTwits replacement
- **WHEN** Extraction Agent runs with new quarterly StockTwits data from config
- **THEN** all existing StockTwits groups are removed from `extraction_results.json`
- **AND** new dated groups are written in their place
- **AND** WhaleWisdom groups are updated independently

### Requirement: Extraction Agent output SHALL be source-tagged for downstream use
Each ticker in the extraction_results.json is tagged by source (appears in groups). Scanner uses group names to infer source flags (in_stocktwits, in_whale_wisdom) for each ticker in StockRecord.

#### Scenario: Source tags guide filtering
- **WHEN** Analyst wants to analyze only WhaleWisdom picks
- **THEN** can filter StockRecord.in_whale_wisdom == true
- **AND** limits analysis to institutional high-conviction stocks
- **AND** excludes pure-StockTwits picks

#### Scenario: Tickers in multiple sources
- **WHEN** a ticker (e.g., AMD) appears in both StockTwits and WhaleWisdom groups
- **THEN** Scanner creates StockRecord with in_stocktwits=True AND in_whale_wisdom=True
- **AND** both flags are available for filtering and analysis

### Requirement: Extraction Agent architecture constraints
Extraction Agent depends on external data sources (WhaleWisdom API, StockTwits config) and produces no dependencies on downstream agents. Scanner consumes output but can operate with any watchlist format. Constraints: (1) Output MUST be JSON file at agents/extraction/extraction_results.json (Scanner hardcodes this path), (2) Source group names MUST follow pattern "SourceName — Context (YYYY-MM-DD)" for Scanner to infer source tags correctly, (3) Deduplication is required (no duplicate tickers in output), (4) All external API failures MUST be handled gracefully (no pipeline crash), (5) StockTwits config file MUST exist at config/stocktwits_watchlist.json for load to succeed.

#### Scenario: Constraint: output file path
- **WHEN** developer moves extraction_results.json to different location
- **THEN** Scanner fails to load (hardcoded path)
- **AND** entire pipeline breaks
- **AND** file location is non-negotiable

#### Scenario: Constraint: source group naming
- **WHEN** Extraction Agent uses non-standard group name (e.g., "st_momentum" instead of "StockTwits Top 25 Momentum — S&P 500 (2026-05-10)")
- **THEN** Scanner cannot infer source correctly via "stocktwits" key name detection
- **AND** source flags may be lost or mislabeled
- **AND** group names must match expected pattern

#### Scenario: Constraint: StockTwits config file requirement
- **WHEN** config/stocktwits_watchlist.json is missing or malformed
- **THEN** ExtractionAgent gracefully logs warning and continues with WhaleWisdom only
- **AND** no exception is raised; pipeline continues
- **AND** extraction_results.json contains WhaleWisdom data only
