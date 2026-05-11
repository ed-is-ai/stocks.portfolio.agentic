## Why

Portfolio positions are imported from SIPP CSV with trade history and cost basis, but lack current market prices, unrealised P&L, and profit targets. Users must manually refresh to see live position valuations. Adding a one-click refresh button fetches live prices from yfinance and recalculates all derived metrics, enabling real-time portfolio monitoring without manual CSV re-imports.

## What Changes

- Add **Refresh** button to portfolio UI that triggers live price fetch
- Query yfinance for current prices for all open position tickers
- Recalculate derived fields: market value, unrealised P&L, P&L %, profit targets (20% / 25%)
- Display recalculated metrics without page reload (AJAX response)
- Store latest prices and calculations in memory/cache for dashboard display

## Capabilities

### New Capabilities
- `portfolio-price-refresh`: One-click button in portfolio UI to fetch live prices from yfinance and recalculate position metrics (market value, unrealised P&L, stop/profit targets)

### Modified Capabilities
- `trader-agent`: Enhanced to support live price lookup and recalculation of portfolio metrics independent of SIPP imports

## Impact

- **Code**: TraderAgent gains new methods for price refresh; web UI gains refresh button and AJAX endpoint
- **UI**: Portfolio tab will display price fetch button and refresh status
- **Dependencies**: yfinance (already a dependency, verify version)
- **Data Flow**: Refresh flow: UI → `/api/portfolio/refresh` → yfinance → recalculate → response → UI update
