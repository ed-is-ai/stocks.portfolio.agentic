## Context

The SIPP CSV import already reads the `Running Balance` column and returns it from `import_sipp()`, but it is only written to `portfolio_value.csv` (which is overwritten each import). The SQLite DB has no cash balance record. As a result the web UI has no access to the cash figure and cannot include it in the portfolio.

Investigation shows the `cash_flows` table is not a reliable source for derivation: 146 of 148 entries are classified as "OTHER" (they are old-format trade settlement records from a pre-2022 CSV format). The only reliable figure is the Running Balance read directly from the CSV: £40,183.72 at the most recent import.

## Goals / Non-Goals

**Goals:**
- Persist the SIPP Running Balance in SQLite so it survives beyond the import call
- Expose `get_cash_balance()` and `set_cash_balance()` on `TraderAgent`
- Show a CASH row in the portfolio positions table
- Include cash in the GBP summary totals (Total Cost, Market Value)

**Non-Goals:**
- Deriving cash dynamically from trades and cash_flows (unreliable with current data)
- Storing per-trade GBP amounts (Phase 2)
- Updating cash balance in real-time between imports
- Tracking USD cash separately

## Decisions

### 1. New `account_state` table (key-value store in SQLite)
A single-row KV table avoids a dedicated schema for a single value and leaves room for future state (e.g. last import date, account number).

```sql
CREATE TABLE IF NOT EXISTS account_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`set_cash_balance(amount)` upserts `key='cash_balance'`. `get_cash_balance()` returns `float | None`.

Alternative considered: storing in `portfolio_value.csv` — rejected because the CSV is also written by the orchestrator (without cash) and is not the right home for DB-accessed state.

### 2. CASH row is a synthetic Position, not a real trade
`get_cash_balance()` returns a float. The web layer constructs a synthetic `Position`-like dict (or a special object) rather than storing a fake trade in the `trades` table. No P&L, no price feed, no Adjust/Sell buttons.

### 3. Cash always in GBP — no conversion needed
The Running Balance is always in GBP. No `price_currency` complexity.

### 4. Cash included in GBP summary totals
`_render_portfolio` already computes `total_cost_gbp` and `total_value_gbp`. Cash is added to both (market value of cash = cost of cash = balance). P&L contribution is £0.

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| Cash balance goes stale between quarterly imports | Acceptable — balance shown with last-import timestamp; user can see it is point-in-time |
| No mechanism to update cash between imports | Add manual override in a follow-on change if needed |
| Old portfolio_value.csv cash_balance column becomes redundant | Leave it — removing it would break the existing chart history |
