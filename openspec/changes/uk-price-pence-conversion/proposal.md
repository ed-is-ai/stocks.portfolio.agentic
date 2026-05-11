## Why

Yahoo Finance returns prices for LSE-listed securities in pence (GBp), not pounds (GBP). The portfolio's cost basis is recorded in GBP, so market value, unrealised P&L, and P&L % are inflated by 100× for affected UK-listed positions. The fix is to divide GBp prices by 100 at fetch time so all prices are on the same pound scale as the cost basis.

## What Changes

- Detect `GBp` currency from yfinance `fast_info` at fetch time and divide price by 100
- No changes to stored cost basis, trade history, or Position model — only the fetched price is normalised

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `portfolio-price-refresh`: fetched prices for GBp-denominated securities are divided by 100 before being stored in the price cache and used in P&L calculations

## Impact

- **Code**: `web/app.py` `_fetch_last_price()` — add currency check and conversion
- **Data**: `price_cache` in `trades.db` will now hold correct GBP values; previously cached GBp values will be overwritten on next refresh
- **UI**: Market value, unrealised P&L, and P&L % for affected UK-listed positions will drop to correct (100× lower) figures
