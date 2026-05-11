## Context

Portfolio UI displays positions imported from SIPP CSV with cost basis, shares, and average cost. Current prices, market values, and P&L are not displayed. Users want a refresh button to fetch live prices and recalculate derived metrics without re-importing the SIPP CSV or restarting the web server.

Current state:
- TraderAgent loads positions from trades.db
- get_portfolio() calculates average cost and total cost
- Web UI displays these static values
- Prices come from yfinance during scanner/analyst pipeline (not available in web UI)

## Goals / Non-Goals

**Goals:**
- Add Refresh button to portfolio UI that fetches live prices
- Integrate yfinance to get current prices for all open position tickers
- Recalculate market value, unrealised P&L, P&L %, profit targets on button click
- Display results in-place (AJAX) without page reload or server restart
- Handle missing tickers gracefully (partial updates)

**Non-Goals:**
- Automatic background price refresh (manual button only)
- Store prices in database (in-memory caching acceptable)
- Add database columns for current prices (computed on-demand)
- Support options, futures, or crypto (stocks only)

## Decisions

### 1. Price fetch method: yfinance direct vs cached prices
**Decision**: Fetch from yfinance directly on each refresh button click (no persistent cache).

**Rationale**: 
- Ensures always-current prices
- Simpler implementation (no cache invalidation)
- Acceptable latency for manual refresh action
- Portfolio is small (7-100 positions)

**Alternatives**:
- Cache prices + TTL: More complex, minimal benefit for manual-trigger use case
- Use scanner/analyst pipeline prices: Requires polling/scheduling, out of scope

### 2. Recalculation scope: Full portfolio vs incremental
**Decision**: Recalculate full portfolio for each position (including closed positions handling).

**Rationale**:
- Consistent with existing get_portfolio() logic
- Simple to understand and maintain
- Full replay avoids partial-state issues

### 3. API endpoint placement
**Decision**: New endpoint POST `/api/portfolio/refresh` returns updated portfolio partial HTML.

**Rationale**:
- Mirrors existing partials pattern (history, runlog, etc.)
- AJAX response updates portfolio table in-place
- No page reload required

**Alternatives**:
- Separate endpoint for prices + endpoint for recalculation: More complex
- WebSocket update: Over-engineered for manual refresh

### 4. Error handling: Missing tickers
**Decision**: Partial update. Tickers missing from yfinance (removed tickers, delisted) are skipped with warning. Portfolio displays prices for successful fetches only.

**Rationale**:
- Graceful degradation
- User can manually check missing tickers
- No halt on single ticker failure

## Risks / Trade-offs

| Risk | Mitigation |
|------|-----------|
| **API rate limits** (yfinance can throttle on bulk requests) | Fetch all tickers in single batch call; acceptable for portfolio size; warn user if rate-limited |
| **Network latency** (yfinance fetch delays response) | Show loading spinner during fetch; acceptable for manual action (~1-2s typical) |
| **Stale session prices** (if scanner runs during UI session) | Refresh captures latest yfinance data; overrides any scanner-pipeline prices |
| **Invalid tickers in portfolio** (e.g., typo in Symbol) | yfinance returns NaN; skip with warning; user corrects via manual trade record |
| **No persistence across sessions** (prices recalculated each refresh) | Acceptable; refresh is explicit action; prices not critical to trade history |

## Migration Plan

1. Add `refresh_portfolio_prices()` method to TraderAgent
2. Add POST `/api/portfolio/refresh` endpoint to web/app.py
3. Add Refresh button to portfolio UI template
4. Wire button to AJAX call → endpoint → update partial
5. Test with actual portfolio (7 positions)
6. No database migration needed (prices not persisted)

## Open Questions

1. Should refresh also update `portfolio_value.csv` with new snapshot? (Decide: No, only for SIPP imports)
2. What about dividend/corporate action prices? (yfinance closing prices are sufficient)
3. Should profit targets recalculate on price change? (Yes, via Position.profit_target_20/25)
