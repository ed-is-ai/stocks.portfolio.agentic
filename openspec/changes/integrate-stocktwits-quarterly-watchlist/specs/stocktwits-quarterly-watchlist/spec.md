## ADDED Requirements

### Requirement: StockTwits watchlist configuration SHALL be stored and maintained quarterly
The system SHALL maintain a quarterly-curated list of StockTwits top 25 momentum stocks by market index in `config/stocktwits_watchlist.json`. Configuration SHALL include S&P 500, NASDAQ 100, and Russell 2000 tickers. The config file is user-maintained and updated manually when new Daily Rip data is available (typically quarterly).

#### Scenario: Config file structure with three indices
- **WHEN** the StockTwits watchlist config is loaded
- **THEN** it contains `indices.sp500.tickers`, `indices.nasdaq100.tickers`, and `indices.russell2000.tickers`
- **AND** each index has exactly 25 tickers (or fewer if fewer available from Daily Rip)
- **AND** all tickers are valid US equity symbols (uppercase, 1-5 characters)

#### Scenario: Config timestamp tracking
- **WHEN** user manually updates the config file
- **THEN** `last_updated` field is updated to the date of the update
- **AND** the date format is ISO 8601 (YYYY-MM-DD)

#### Scenario: Quarterly refresh cycle
- **WHEN** a quarterly refresh is due (approximately every 90 days)
- **THEN** user copies the latest top 25 from StockTwits Daily Rip for each index
- **AND** updates corresponding sections in `config/stocktwits_watchlist.json`
- **AND** the new tickers replace the previous quarter's list entirely (no accumulation)

### Requirement: Extraction Agent SHALL load and merge StockTwits tickers from config
The Extraction Agent SHALL read the StockTwits quarterly watchlist config, load tickers by index, and merge them with WhaleWisdom results for a multi-source extraction pipeline. StockTwits tickers SHALL appear in `extraction_results.json` under separate dated groups (one per index).

#### Scenario: Load StockTwits config during extraction run
- **WHEN** ExtractionAgent.run() is called
- **THEN** it loads `config/stocktwits_watchlist.json`
- **AND** extracts tickers from each index (sp500, nasdaq100, russell2000)
- **AND** proceeds with merging regardless of WhaleWisdom success (StockTwits can be loaded independently)

#### Scenario: Merge and deduplicate tickers
- **WHEN** both WhaleWisdom and StockTwits tickers are loaded
- **THEN** the agent produces a union of all tickers (no duplicates)
- **AND** tickers appearing in both sources are tracked (for source tagging downstream)
- **AND** each source group in `extraction_results.json` contains only tickers from that source

#### Scenario: Output StockTwits groups by index
- **WHEN** ExtractionAgent completes a run with StockTwits data
- **THEN** `extraction_results.json` contains three new groups:
  - `"StockTwits Top 25 Momentum — S&P 500 (YYYY-MM-DD)"`
  - `"StockTwits Top 25 Momentum — NASDAQ 100 (YYYY-MM-DD)"`
  - `"StockTwits Top 25 Momentum — Russell 2000 (YYYY-MM-DD)"`
- **AND** each group maps to its corresponding tickers from config
- **AND** the date is today's date in ISO format

#### Scenario: Replace old StockTwits groups
- **WHEN** ExtractionAgent runs with new StockTwits data
- **THEN** any existing StockTwits groups are removed from `extraction_results.json`
- **AND** new dated groups are written in their place
- **AND** WhaleWisdom groups are preserved (not replaced unless from a fresh WhaleWisdom fetch)

### Requirement: Scanner SHALL detect StockTwits source tag from extraction results
The Scanner SHALL infer `in_stocktwits` flag for each ticker based on presence in StockTwits groups in `extraction_results.json`. Ticker appearing in any StockTwits group (by index) SHALL be tagged `in_stocktwits=True`.

#### Scenario: Multi-source tagging
- **WHEN** a ticker (e.g., MU) appears in both "StockTwits Top 25 Momentum" and "WisdomWise Heat Map" groups
- **THEN** Scanner creates StockRecord with `in_stocktwits=True` and `in_whale_wisdom=True`
- **AND** both flags are preserved in the StockRecord for downstream analysis

#### Scenario: StockTwits-only ticker
- **WHEN** a ticker (e.g., SNDK) appears only in StockTwits groups
- **THEN** Scanner creates StockRecord with `in_stocktwits=True` and `in_whale_wisdom=False`

#### Scenario: WhaleWisdom-only ticker
- **WHEN** a ticker (e.g., FSLR) appears only in WhaleWisdom group
- **THEN** Scanner creates StockRecord with `in_stocktwits=False` and `in_whale_wisdom=True`
