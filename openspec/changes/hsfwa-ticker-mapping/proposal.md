## Why

Some portfolio positions use internal ticker symbols that do not exist on Yahoo Finance (e.g. a fund assigned a synthetic ticker during SIPP import), so live price refresh always returns `$nan` for those positions. A configurable ticker alias map is needed so each internal ticker can be mapped to its real market data symbol.

## What Changes

- Add a JSON config file (`ticker_aliases.json`) where users can define `{ "INTERNAL": "YF_SYMBOL" }` mappings
- Update the `/api/portfolio/refresh` endpoint to apply aliases before fetching from yfinance
- Tickers with no alias and no yfinance result continue to show `—` gracefully

## Capabilities

### New Capabilities
- `ticker-alias-config`: User-editable JSON file mapping internal tickers to their Yahoo Finance equivalents; read by the price refresh endpoint at runtime

### Modified Capabilities
- (none — existing behavior for standard tickers is unchanged)

## Impact

- **Code**: `web/app.py` refresh endpoint reads alias file before yfinance fetch
- **Config**: New `data/ticker_aliases.json` file (gitignored; user-managed)
- **UI**: No UI changes required
- **Dependencies**: None new
