## Why

The portfolio tab shows open stock positions but not the cash held in the SIPP account. Without the cash balance, Total Cost and Market Value are understated and the user has no single view of their full account value.

## What Changes

- Store the SIPP Running Balance in a new `account_state` SQLite table at import time
- Expose `get_cash_balance()` on `TraderAgent`
- Render a CASH row in the portfolio positions table (no price feed needed — always £1)
- Include cash in the GBP summary totals (Total Cost, Market Value)

## Capabilities

### New Capabilities
- `portfolio-cash-position`: Cash balance stored at SIPP import time and displayed as a position row in the portfolio tab, included in aggregate totals

### Modified Capabilities

## Impact

- **Code**: `agents/trader/trader_agent.py` — new `account_state` table, `get_cash_balance()`, `set_cash_balance()`, update `import_sipp` to persist Running Balance
- **Code**: `web/app.py` — fetch cash balance and pass to `_render_portfolio`
- **UI**: `web/templates/_portfolio.html` — render CASH row, include in GBP totals
