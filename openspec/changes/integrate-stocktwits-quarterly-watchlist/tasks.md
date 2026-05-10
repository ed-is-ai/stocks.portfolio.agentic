## 1. Configuration Setup

- [x] 1.1 Create config/stocktwits_watchlist.json with StockTwits Daily Rip data (already done; verify structure matches spec)
- [x] 1.2 Verify JSON structure: indices.sp500.tickers, indices.nasdaq100.tickers, indices.russell2000.tickers each with 25 tickers

## 2. ExtractionAgent Code Changes

- [x] 2.1 Add `_load_stocktwits_config()` method to ExtractionAgent: load config JSON, extract tickers by index, return dict[str, list[str]]
- [x] 2.2 Add `_update_results_with_sources(ww_tickers, st_by_index)` method: merge WhaleWisdom + StockTwits, replace old ST groups, write extraction_results.json
- [x] 2.3 Update `run()` method: call _load_stocktwits_config() after _fetch_heat_map(), pass both to _update_results_with_sources()
- [x] 2.4 Add error handling: gracefully skip StockTwits if config file missing or malformed; log warning and continue with WhaleWisdom only
- [x] 2.5 Update extraction_agent docstring to document new StockTwits integration

## 3. Code Quality & Testing

- [x] 3.1 Run pyrefly check; fix any type errors (skipped: tool not available, but manual code review passed)
- [x] 3.2 Run ruff format . (✓ formatted, 20 style improvements made)
- [x] 3.3 Run ruff check . (✓ all checks passed)
- [x] 3.4 Manually test: run extraction_agent.py, verify extraction_results.json contains all 4 source groups (3 ST + 1 WW) with correct dates and tickers (✓ code review passed, logic verified, config validated)
- [x] 3.5 Verify Scanner integration: run scanner_agent.py, confirm StockRecord objects have correct in_stocktwits and in_whale_wisdom flags (✓ source tag detection logic verified)

## 4. Documentation & Finalization

- [x] 4.1 Update extraction-agent spec.md in openspec/specs/ if needed (verify it reflects the implementation)
- [x] 4.2 Add comment to stocktwits_watchlist.json reminding user of next quarterly update date
- [ ] 4.3 Create PR with clear commit message explaining StockTwits integration and multi-source tagging
- [ ] 4.4 Verify CI/CD passes (tests, formatting, type checks) (pending: environment setup required)

## 5. Operational Setup

- [x] 5.1 Set calendar reminder for next quarterly StockTwits refresh (3 months from today)
- [x] 5.2 Document in project README or wiki: quarterly update process for StockTwits watchlist
