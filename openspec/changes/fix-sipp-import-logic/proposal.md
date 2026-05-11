## Why

The SIPP import logic incorrectly categorizes transactions, creating 96 phantom open positions instead of the actual 7 holdings. Rows with `Symbol='n/a'` (dividends, contributions, tax relief) are being imported as trades, creating ghost tickers. Additionally, the portfolio snapshot does not track cash balance separately, making it impossible to reconcile account value.

## What Changes

- **Fix trade filtering**: Only import rows where `Symbol` is valid (not 'n/a') as stock trades. Rows with `Symbol='n/a'` are non-trade cash flows.
- **Add cash_flows table**: Track dividends, contributions, tax relief, interest, transfers, and withdrawals separately from trades.
- **Use Running Balance as source of truth**: Extract the final running balance from the SIPP CSV as the current cash position.
- **Update portfolio snapshot**: Track cash_balance and investments_value separately in portfolio_value.csv.

## Capabilities

### New Capabilities
- `sipp-cash-flows`: Track non-trade SIPP transactions (contributions, dividends, tax relief, interest, transfers, withdrawals) in a dedicated table with flow type classification.

### Modified Capabilities
- `trader-agent`: Modify import logic to separate trades (valid Symbol) from cash flows (Symbol='n/a'), and use Running Balance as cash source of truth.

## Impact

- **Code**: agents/trader/trader_agent.py (import logic), database schema (add cash_flows table)
- **Data**: trades.db (add cash_flows table), portfolio_value.csv (add cash_balance and investments_value columns)
- **APIs**: TraderAgent.get_portfolio() will now include cash balance; portfolio_value.csv structure changes
- **UI**: Portfolio tab will show correct position count (7 instead of 96) and accurate cash balance

