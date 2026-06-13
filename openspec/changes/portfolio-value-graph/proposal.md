## Why

The portfolio tab currently shows a static table of positions with no historical context. Adding a value-over-time graph makes it immediately visible whether the portfolio is growing, how cash compares to equities, and whether recent trades improved the overall picture.

## What Changes

- Add a time-series chart at the top of the portfolio tab showing total portfolio value history
- The chart renders two lines: **Total Value** (stocks + cash) and **Cash** (cash balance alone)
- Portfolio value snapshots are recorded to `portfolio_value.csv` each time prices are refreshed
- Cash balance is already stored in `account_state`; each snapshot row will also include the cash component
- The chart is rendered via Chart.js (already loaded on the page)

## Capabilities

### New Capabilities
- `portfolio-value-chart`: A line chart at the top of the portfolio tab showing total portfolio value and cash balance over time, built from snapshot history

### Modified Capabilities
- `portfolio-cash-position`: Extend the snapshot CSV format to include a `cash_value` column so the chart can plot cash as a separate line

## Impact

- `portfolio_value.csv`: New `cash_value` column added (existing rows treated as 0 for backwards compatibility)
- `web/app.py`: `_load_portfolio_history()` reads the new column; snapshot writes include cash
- `web/templates/_portfolio.html`: Chart section updated — two datasets instead of one value line
- `agents/trader/trader_agent.py`: No changes needed (cash already in `account_state`)
