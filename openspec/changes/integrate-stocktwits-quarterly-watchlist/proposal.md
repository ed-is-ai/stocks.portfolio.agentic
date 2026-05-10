## Why

The Extraction Agent currently sources watchlists from WhaleWisdom only. Adding StockTwits sentiment-driven data provides complementary momentum signals and social conviction data, diversifying the source mix. A quarterly-curated approach avoids Cloudflare API blocking while leveraging StockTwits' high-signal top 25 momentum lists across major indices.

## What Changes

- Create `config/stocktwits_watchlist.json` with quarterly curated top 25 stocks from StockTwits Daily Rip (S&P 500, NASDAQ 100, Russell 2000)
- Update ExtractionAgent to load StockTwits tickers from config and merge with WhaleWisdom results
- Update `extraction_results.json` to include StockTwits groups (one per index) alongside existing WhaleWisdom group
- Scanner automatically detects source tags (in_stocktwits, in_whale_wisdom) from group key names in extraction_results.json

## Capabilities

### New Capabilities
- `stocktwits-quarterly-watchlist`: Quarterly manual curation of StockTwits top 25 momentum stocks by index. Config-based source integration with automatic deduplication at extraction stage.

### Modified Capabilities
- `extraction-agent`: Now aggregates from two sources (WhaleWisdom + StockTwits) instead of WhaleWisdom only. Maintains multi-source tagging contract with Scanner.

## Impact

- **Code**: ExtractionAgent gains `_load_stocktwits_config()` and refactored `_update_results_with_sources()` methods. ~30 lines of code.
- **Files**: New `config/stocktwits_watchlist.json` (user-maintained quarterly), modified `agents/extraction/extraction_agent.py`.
- **Data contracts**: `extraction_results.json` format unchanged (dict of source groups → lists). Scanner's `load_source_map()` already handles multi-source tagging.
- **Operations**: Quarterly manual refresh of config file (copy top 25 from StockTwits Daily Rip page).
- **Risk**: Minimal. Config-based approach avoids API fragility. No backward compatibility concerns.
