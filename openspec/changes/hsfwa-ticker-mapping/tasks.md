## 1. Alias Config Support

- [x] 1.1 Create `data/ticker_aliases.json` with initial fund mapping (user fills in correct yfinance symbol)
- [x] 1.2 Add `data/ticker_aliases.json` to `.gitignore`
- [x] 1.3 Add `_load_ticker_aliases()` helper to `web/app.py` that reads the file and returns `dict[str, str]`; returns `{}` if file absent

## 2. Price Fetch Integration

- [x] 2.1 In `refresh_portfolio_prices` endpoint, call `_load_ticker_aliases()` before building the tickers list
- [x] 2.2 Replace any aliased tickers in the fetch list with their Yahoo Finance equivalents
- [x] 2.3 After fetching, map aliased results back to original internal ticker names

## 3. Validation & Commit

- [x] 3.1 Manually verify aliased fund price appears after entering its correct yfinance symbol
- [x] 3.2 Verify non-aliased tickers are unaffected
- [x] 3.3 Commit with message: `feat(trader): add ticker alias config for non-standard yfinance symbols`
