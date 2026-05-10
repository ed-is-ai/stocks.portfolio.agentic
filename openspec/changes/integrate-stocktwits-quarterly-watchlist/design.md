## Context

The Extraction Agent currently pulls only WhaleWisdom institutional heat map data (~50 stocks). StockTwits Daily Rip publishes weekly top 25 momentum stocks across three indices. The direct API is blocked by Cloudflare, making automated scraping unreliable. However, manual quarterly curation is lightweight and preserves data quality.

Scanner already has multi-source tagging infrastructure: it infers `in_stocktwits` and `in_whale_wisdom` flags from group key names in `extraction_results.json`. No Scanner changes needed.

## Goals / Non-Goals

**Goals:**
- Integrate StockTwits data as a second extraction source
- Maintain extraction agent's existing WhaleWisdom integration without disruption
- Support multi-source tagging: tickers appearing in both sources should be tagged with both flags
- Keep quarterly maintenance burden minimal (manual config edit, no API management)

**Non-Goals:**
- Automate StockTwits data collection (Cloudflare blocking makes this fragile)
- Real-time sentiment scoring (Daily Rip is weekly, not real-time)
- Create new Scanner specs or modify Scanner code (reuse existing multi-source logic)
- Add API authentication or credential management

## Decisions

### 1. Quarterly Manual Curation vs. Automated Scraping
**Decision**: Manual quarterly refresh of `config/stocktwits_watchlist.json`.

**Rationale**: 
- StockTwits Daily Rip is blocked by Cloudflare Challenge
- Web scraping with browser automation (Selenium/Playwright) adds runtime overhead and fragility
- Manual curation is low-friction: copy 75 tickers quarterly, store in config
- User has calendar reminder; no implementation risk

**Alternatives Considered**:
- Cloudflare bypass library (cf-clearance): maintenance burden, library must track CF updates
- RapidAPI third-party wrapper: depends on external service, potential costs
- Real-time StockTwits API: same Cloudflare blocking, plus adds complexity for real-time updates that aren't necessary

### 2. Config File Structure: By Index
**Decision**: Organize StockTwits tickers in config by index (sp500, nasdaq100, russell2000), matching Daily Rip's structure.

**Rationale**:
- Preserves curation context: user knows which index each list came from
- Natural alignment with Daily Rip's presentation (S&P 500, NASDAQ 100, Russell 2000 tabs)
- Easy for quarterly manual updates: copy from one source, paste to corresponding config section
- Extraction agent flattens to 3 groups in `extraction_results.json` (one per index)

**Alternatives Considered**:
- Flat list: simpler structure, but loses index context; harder to update quarterly
- Database table: over-engineered for static quarterly data

### 3. Replacement vs. Accumulation in extraction_results.json
**Decision**: Replace old StockTwits groups on each run; keep WhaleWisdom group.

**Rationale**:
- StockTwits data is quarterly snapshots, not cumulative history
- Old data becomes stale; replacement is cleaner than versioning
- Matches WhaleWisdom's approach (updates each run)
- Prevents file bloat from accumulating dated groups

**Alternatives Considered**:
- Keep all historical dated groups: preserves audit trail, but file grows unbounded
- Hybrid with retention limit: adds complexity for marginal benefit

### 4. Multi-Source Tagging: Automatic Detection
**Decision**: Scanner's existing `load_source_map()` detects sources from key names in `extraction_results.json`.

**Rationale**:
- Scanner already checks for "stocktwits" and "wisdom" in group key names
- Zero code changes in Scanner; reuse existing contract
- Clear key naming ensures correct tag assignment

**Alternatives Considered**:
- Explicit metadata file mapping tickers to sources: redundant with key names, harder to maintain
- Scanner changes to support dedicated source flags: unnecessary; existing logic works

### 5. No Context Metadata File for StockTwits
**Decision**: Skip creating `st_context.json` (unlike `ww_context.json`).

**Rationale**:
- WhaleWisdom context tracks filer data (filers_increasing, ww_rank): rich metadata
- StockTwits is just a curated list of symbols; no detailed metadata to track
- Minimizes file footprint and maintenance

## Risks / Trade-offs

[Risk] Manual update discipline: quarterly refresh might be forgotten
→ Mitigation: User relies on calendar reminder; lightweight update process (edit JSON file)

[Risk] StockTwits data quality: social sentiment ≠ institutional conviction
→ Mitigation: Data is separate source tag; analyst and trader agents can filter if needed

[Risk] Quarterly lag: latest momentum data is 1-3 weeks old by update time
→ Mitigation: Acceptable for portfolio strategy that rebalances monthly; real-time updates not required

[Risk] Index-specific grouping increases extraction_results.json verbosity
→ Mitigation: File remains human-readable; deduplication logic handles group count gracefully

## Migration Plan

1. Create `config/stocktwits_watchlist.json` with initial data (done: user provided 75 stocks from Daily Rip)
2. Update ExtractionAgent to load config and merge with WhaleWisdom
3. Deploy and test: verify extraction_results.json has both source groups, Scanner tags tickers correctly
4. Establish quarterly refresh schedule (calendar reminder for user)

Rollback: Remove StockTwits groups from extraction_results.json, revert ExtractionAgent changes. Zero data migration needed (JSON-based config).

## Open Questions

1. **When to perform first quarterly refresh?** Suggest: every 3 months from deployment date, e.g., 2026-08-10.
2. **Should we add metrics to track source overlap?** (e.g., "3 tickers in both sources") Could be useful for analyst insights but not critical for MVP.
3. **Future: full Daily Rip integration?** Once StockTwits API stability improves or we have browser automation in place, could automate this. For now, manual is pragmatic.
