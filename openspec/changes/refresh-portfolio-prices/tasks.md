## 1. TraderAgent Enhancement

- [x] 1.1 Add `refresh_portfolio_prices(prices: dict[str, float])` method to TraderAgent
- [x] 1.2 Implement recalculation logic: market_value = current_price * shares
- [x] 1.3 Implement unrealised P&L calculation: current_value - total_cost
- [x] 1.4 Implement unrealised P&L % calculation: (unrealised_pnl / total_cost) * 100
- [x] 1.5 Implement profit targets: entry_price * 1.20 and entry_price * 1.25
- [x] 1.6 Handle missing/None prices gracefully (partial updates, logging)
- [x] 1.7 Update Position model to use recalculated values in returned objects

## 2. Web API Endpoint

- [x] 2.1 Create POST `/api/portfolio/refresh` endpoint in web/app.py
- [x] 2.2 Import yfinance and implement price fetch: yfinance.download(tickers)
- [x] 2.3 Parse yfinance response into dict[str, float] format
- [x] 2.4 Call TraderAgent.refresh_portfolio_prices() with fetched prices
- [x] 2.5 Render updated portfolio partial HTML (reuse existing _portfolio.html partial)
- [x] 2.6 Return partial on success (200), error message on failure (500)
- [x] 2.7 Add error handling for network timeouts, rate limits, missing tickers

## 3. Web UI Button & AJAX

- [x] 3.1 Add Refresh button to portfolio.html template
- [x] 3.2 Style button to match existing portfolio UI
- [x] 3.3 Add click handler JavaScript that:
  - Shows loading spinner/state
  - POSTs to `/api/portfolio/refresh`
  - Updates portfolio table with response
  - Shows error message on failure
  - Clears loading state
- [x] 3.4 Add loading spinner CSS (or reuse existing spinner if available)
- [x] 3.5 Add error message display area for missing tickers/fetch failures

## 4. Testing & Validation

- [ ] 4.1 Test refresh with actual portfolio (7 positions): verify all prices fetch
- [ ] 4.2 Verify market value calculated correctly: price * shares
- [ ] 4.3 Verify unrealised P&L: market_value - total_cost
- [ ] 4.4 Verify unrealised P&L %: (pnl / total_cost) * 100
- [ ] 4.5 Verify profit targets: entry_price * 1.20 and * 1.25
- [ ] 4.6 Test with missing ticker (typo): verify partial update + warning
- [ ] 4.7 Test with network timeout: verify error handling and retry
- [ ] 4.8 Test button click flow end-to-end: button → fetch → display → update
- [x] 4.9 Run type checks: `pyrefly check`
- [x] 4.10 Run formatter: `uv run ruff format .` and `uv run ruff check . --fix`

## 5. Code Quality & Documentation

- [x] 5.1 Add docstring to `refresh_portfolio_prices()` method
- [x] 5.2 Add error handling comments for yfinance edge cases
- [x] 5.3 Ensure no unused imports
- [x] 5.4 Verify no console logs/print statements remain
- [x] 5.5 Add logging for refresh operations (start, success, failure)

## 6. Integration & Deployment

- [ ] 6.1 Verify web server restarts without errors
- [ ] 6.2 Test refresh button in browser (manual QA)
- [ ] 6.3 Commit changes with message: `feat(trader): add portfolio price refresh with yfinance`
- [ ] 6.4 Create PR with description of feature and testing steps
- [x] 6.5 Verify type checks pass in CI
- [x] 6.6 Verify formatter checks pass in CI
