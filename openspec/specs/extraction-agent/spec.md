## ADDED Requirements

### Requirement: Extraction Agent SHALL aggregate watchlist from multiple sources
Extraction Agent pulls stock tickers from multiple external screeners and sources (WhaleWisdom institutional data and StockTwits quarterly-curated lists), deduplicates, and outputs a union watchlist. Each ticker is tagged with source membership for downstream filtering.

#### Scenario: Combine tickers from WhaleWisdom and StockTwits
- **WHEN** Extraction Agent runs
- **THEN** fetches:
  - WhaleWisdom heat map: top 50 stocks by institutional conviction
  - StockTwits quarterly watchlist: top 25 momentum stocks by index (S&P 500, NASDAQ 100, Russell 2000)
- **AND** deduplicates (e.g., MU appears in both, counted once)
- **AND** outputs union of ~75-125 unique tickers (size varies by overlap)
- **AND** tags each: MU {in_whale_wisdom: true, in_stocktwits: true} (if in both)

#### Scenario: Handle source-specific filtering
- **WHEN** developer wants to skip StockTwits (WhaleWisdom only)
- **THEN** Extraction Agent:
  - Gracefully skips StockTwits if config file missing
  - Fetches WhaleWisdom from API
  - Outputs WhaleWisdom tickers only
  - Source tags reflect available sources (in_stocktwits=False, in_whale_wisdom=True)

### Requirement: Extraction Agent SHALL track institutional buying context from WhaleWisdom
For stocks sourced from WhaleWisdom (top holdings of tracked hedge funds), Extraction Agent associates metadata: which filers increased positions, which decreased, rank by conviction. This context is passed to Scanner for enrichment.

#### Scenario: WhaleWisdom institutional context
- **WHEN** AAPL is in top holdings of Warren Buffett + 5 other tracked filers
- **THEN** Extraction Agent:
  - Fetches WhaleWisdom data: filer names, position changes (buying/selling)
  - Creates ww_context.json entry: {"AAPL": {filers_increasing: 5, filers_decreasing: 1, rank: 1}}
  - Scanner later uses this to set funds_buying, funds_selling, funds_net

### Requirement: Extraction Agent SHALL output watchlist in JSON format
Results are persisted to extraction_results.json as a dict mapping source group name to list of tickers (grouped by source and index for StockTwits).

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
- **THEN** all existing StockTwits groups are removed from extraction_results.json
- **AND** new dated groups are written in their place
- **AND** WhaleWisdom groups are updated independently

### Requirement: Extraction Agent SHALL handle source-specific data formats
Each source produces data in its native format; Extraction Agent normalizes to a common ticker list format for downstream consumption.

#### Scenario: WhaleWisdom normalization
- **WHEN** WhaleWisdom API returns structured holdings data
- **THEN** Extraction Agent:
  - Extracts ticker symbols from response
  - Ranks by overall_rank field
  - Returns top 50 as normalized list

#### Scenario: StockTwits normalization
- **WHEN** quarterly StockTwits config is loaded
- **THEN** Extraction Agent:
  - Reads tickers from config by index
  - All tickers pre-curated by StockTwits (no additional filtering)
  - Returns tickers as-is from config

### Requirement: Extraction Agent SHALL handle failures and continue operation
If an external source is unavailable, Extraction Agent gracefully excludes that source and continues with available sources.

#### Scenario: WhaleWisdom API unavailable
- **WHEN** WhaleWisdom API returns error or timeout
- **THEN** Extraction Agent:
  - Logs warning: "WhaleWisdom fetch failed"
  - Continues with StockTwits config
  - Outputs watchlist from StockTwits only

#### Scenario: StockTwits config missing or malformed
- **WHEN** config/stocktwits_watchlist.json is missing or fails to parse
- **THEN** Extraction Agent:
  - Logs warning: "Failed to load StockTwits config"
  - Continues with WhaleWisdom only
  - Outputs watchlist from WhaleWisdom only

#### Scenario: All sources unavailable
- **WHEN** both WhaleWisdom and StockTwits fail
- **THEN** Extraction Agent:
  - Logs critical error
  - Returns empty watchlist
  - Pipeline will use previous results or fallback if available

### Requirement: Extraction Agent SHALL report extraction results
Each extraction run updates extraction_results.json with source-grouped tickers and optionally logs metadata.

#### Scenario: Log extraction status
- **WHEN** Extraction Agent completes run
- **THEN** writes to extraction_results.json with dated source groups:
  - StockTwits groups (by index): S&P 500, NASDAQ 100, Russell 2000
  - WhaleWisdom group: heat map ranking data
  - All groups include today's date (YYYY-MM-DD format)
  - status: "success" (or empty file if all sources failed)

### Requirement: Extraction Agent SHALL support adding new sources
To add a new watchlist source, follow the pattern established by StockTwits (config-based) or WhaleWisdom (API-based).

#### Scenario: Add new quarterly-curated source
- **WHEN** developer wants to include a manually-curated list (e.g., VCP screener results)
- **THEN** creates config/[source]_watchlist.json following same structure
- **AND** adds _load_[source]_config() method to ExtractionAgent
- **AND** modifies _update_results_with_sources() to include new source
- **AND** Scanner automatically detects source from group key name in extraction_results.json

#### Scenario: Add new API-based source
- **WHEN** developer wants to integrate an automated API source (e.g., future TradingView integration)
- **THEN** creates _fetch_[source]() method in ExtractionAgent
- **AND** returns list[str] of tickers matching WhaleWisdom API response format
- **AND** adds source-specific context file if needed (like ww_context.json)
- **AND** modifies _update_results_with_sources() to include new source

### Requirement: Extraction Agent output SHALL be source-tagged for downstream use
Each ticker in the extraction_results.json includes source tag(s) indicating which source(s) included it. Scanner uses these tags (in_stocktwits, in_whale_wisdom, etc.) to annotate StockRecord for downstream analysis.

#### Scenario: Source tags guide filtering
- **WHEN** Analyst wants to analyze only WhaleWisdom picks
- **THEN** can filter StockRecord.in_whale_wisdom == true
- **AND** limits analysis to high-conviction institutional picks

### Requirement: Extraction Agent architecture constraints
Extraction Agent depends on external APIs (WhaleWisdom) and config files (StockTwits) and produces no dependencies on downstream agents (Scanner consumes output but can operate with any watchlist format). Constraints: (1) Output MUST be JSON file at agents/extraction/extraction_results.json (Scanner hardcodes this path), (2) Source group names MUST follow pattern "SourceName — Context (YYYY-MM-DD)" for Scanner source detection, (3) Deduplication is required (no duplicate tickers in output), (4) All external failures MUST be handled gracefully (no pipeline crash), (5) StockTwits config file MUST exist at config/stocktwits_watchlist.json or source gracefully skips.

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

#### Scenario: Constraint: StockTwits config file requirement
- **WHEN** config/stocktwits_watchlist.json is missing or malformed
- **THEN** Extraction Agent logs warning and continues with WhaleWisdom only
- **AND** no exception is raised; pipeline continues
- **AND** extraction_results.json contains WhaleWisdom data only
