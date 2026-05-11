## 1. DB Schema and TraderAgent

- [x] 1.1 Add `account_state` table to `_SCHEMA` in `trader_agent.py`: `key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL`
- [x] 1.2 Add migration in `_init_db` to create the table if it doesn't exist (handled by `executescript` with `CREATE TABLE IF NOT EXISTS`)
- [x] 1.3 Add `set_cash_balance(amount: float) -> None` method — upserts `key='cash_balance'` with UTC timestamp
- [x] 1.4 Add `get_cash_balance() -> float | None` method — returns float or None if no row exists
- [x] 1.5 In `import_sipp`, call `self.set_cash_balance(cash_balance)` after reading the Running Balance

## 2. Web Layer

- [x] 2.1 In `partial_portfolio`, call `trader.get_cash_balance()` and pass `cash_balance` to `_render_portfolio`
- [x] 2.2 Add `cash_balance: float | None = None` parameter to `_render_portfolio`
- [x] 2.3 In `_render_portfolio`, add cash to `total_cost_gbp` and `total_value_gbp` if `cash_balance` is not None
- [x] 2.4 Pass `cash_balance` through to the template context

## 3. Template

- [x] 3.1 After the positions loop in `_portfolio.html`, render a CASH row when `cash_balance` is set: ticker "CASH", cost basis and market value = `cash_balance`, P&L = £0.00 (0%), no Adjust/Sell buttons
- [x] 3.2 Ensure the CASH row uses `£` symbol (always GBP)

## 4. Commit

- [ ] 4.1 Commit: `feat(portfolio): show cash balance as a position row with GBP totals`
