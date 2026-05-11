## Why

The portfolio mixes GBP and USD values: cost basis is recorded in GBP (SIPP imports via a UK broker), but live prices for USD-denominated positions are in USD. This causes incorrect market value and P&L figures for USD positions and shows `$` for all values regardless of currency. The portfolio should display everything in GBP using the live exchange rate, with the `£` symbol throughout.

## What Changes

- Fetch the live GBP/USD exchange rate from yfinance (`GBPUSD=X`) once per price refresh
- Convert USD-denominated live prices to GBP before storing in `price_cache`
- Store the exchange rate used alongside the price cache (for display and audit)
- Replace `$` with `£` throughout the portfolio template
- Show the exchange rate used in the portfolio toolbar

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `portfolio-price-refresh`: USD prices are converted to GBP at fetch time using live rate; all cached prices are GBP; exchange rate stored with cache
- `ticker-alias-config`: no change to config format; conversion applied to aliased tickers using their yfinance currency

## Impact

- **Code**: `web/app.py` — fetch `GBPUSD=X`, divide USD prices by rate; pass rate to template
- **Code**: `agents/trader/trader_agent.py` — store exchange rate in `price_cache` table or separate field
- **UI**: `_portfolio.html` — replace `$` with `£`; show exchange rate in toolbar
- **Data**: cached prices overwritten with GBP values on next refresh; no schema change to trades table
