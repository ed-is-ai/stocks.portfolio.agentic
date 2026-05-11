## ADDED Requirements

### Requirement: Portfolio refresh button fetches live prices from yfinance
The portfolio UI SHALL display a Refresh button that, when clicked, fetches current market prices for all open position tickers from yfinance and recalculates derived metrics (market value, unrealised P&L, profit targets) without requiring a page reload or SIPP CSV re-import.

#### Scenario: User clicks refresh button and prices load successfully
- **WHEN** user clicks the Refresh button on the portfolio tab
- **THEN** button shows loading state (spinner or "Fetching prices...")
- **AND** system fetches current prices from yfinance for all open position tickers
- **AND** system recalculates market value, unrealised P&L, P&L %, profit_target_20%, and profit_target_25% for each position
- **AND** portfolio table updates in-place with new price and derived values
- **AND** loading state clears and button returns to normal

#### Scenario: Price fetch completes with some missing tickers
- **WHEN** yfinance fetch completes but some tickers return no price data (delisted, removed, or typo)
- **THEN** positions with valid prices update successfully in the portfolio table
- **AND** positions with missing prices show "N/A" or last-known value
- **AND** a warning message displays: "Could not fetch prices for: [ticker list]"
- **AND** user can manually correct ticker symbols or contact support

#### Scenario: Price fetch fails or times out
- **WHEN** yfinance fetch fails (network error, rate limit, timeout)
- **THEN** loading state clears
- **AND** an error message displays: "Failed to fetch prices. Please try again."
- **AND** portfolio table retains previous values (no data loss)
- **AND** button returns to normal state, allowing user to retry

### Requirement: TraderAgent supports live price refresh without SIPP re-import
TraderAgent SHALL provide a method to refresh portfolio prices from yfinance and recalculate position metrics, independent of SIPP CSV imports.

#### Scenario: Refresh prices for open positions
- **WHEN** `TraderAgent.refresh_portfolio_prices(prices)` is called with a dict of {ticker: price}
- **THEN** system recalculates market value for each position: `current_price * shares`
- **AND** unrealised P&L: `market_value - total_cost`
- **AND** unrealised P&L %: `(unrealised_pnl / total_cost) * 100`
- **AND** profit targets: `entry_price * 1.20` and `entry_price * 1.25`
- **AND** method returns updated Position objects with current values

#### Scenario: Refresh handles missing or invalid prices
- **WHEN** prices dict is incomplete or contains NaN/None values
- **THEN** positions with valid prices are updated with calculations
- **AND** positions with missing prices retain previous current_price value
- **AND** positions with missing prices set unrealised_pnl and unrealised_pnl_pct to None
- **AND** method logs warning for missing tickers

### Requirement: Web UI provides refresh endpoint
The web UI SHALL provide a POST endpoint `/api/portfolio/refresh` that accepts a price fetch trigger, calls yfinance, and returns the updated portfolio partial HTML.

#### Scenario: Refresh endpoint processes price fetch
- **WHEN** POST request sent to `/api/portfolio/refresh`
- **THEN** endpoint fetches prices from yfinance for all current portfolio tickers
- **AND** passes prices to TraderAgent.refresh_portfolio_prices()
- **AND** returns updated portfolio HTML partial (table with refreshed values)
- **AND** response includes status: 200 on success, 500 on fetch failure

#### Scenario: Refresh endpoint handles yfinance errors gracefully
- **WHEN** yfinance fetch raises exception (rate limit, network error, timeout)
- **THEN** endpoint catches exception and returns 500 status
- **AND** response includes error message for UI to display
- **AND** partial HTML is empty or retains previous data
