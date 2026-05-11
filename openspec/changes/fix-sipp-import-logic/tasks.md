## 1. Database Schema Updates

- [x] 1.1 Create cash_flows table schema in trades.db with columns: id, date, flow_type, ticker, amount, description, reference
- [x] 1.2 Add UNIQUE constraint on reference column for duplicate prevention
- [x] 1.3 Add flow_type CHECK constraint: CONTRIBUTION, DIVIDEND, INTEREST, TAX_RELIEF, TRANSFER, WITHDRAWAL, OTHER

## 2. SIPP Import Logic Refactor

- [x] 2.1 Update import_sipp() function to filter trades: only import rows where Symbol is not 'n/a' and not empty
- [x] 2.2 Implement cash flow extraction: classify Symbol='n/a' rows by Description field (check for "contribution", "div", "interest", "tax relief", "trf", "debit")
- [x] 2.3 Extract final Running Balance from CSV as cash position (iterate backwards, find last non-null, non-zero Running Balance entry)
- [x] 2.4 Parse amounts correctly: remove £ symbols, commas, quotes; handle "n/a" values
- [x] 2.5 Implement Reference field extraction and storage for audit trail in cash_flows table

## 3. Portfolio Value Snapshot

- [x] 3.1 Update portfolio_value.csv format: add columns cash_balance and investments_value
- [x] 3.2 Calculate investments_value as sum of all position costs (from trades)
- [x] 3.3 Set cash_balance from extracted Running Balance
- [x] 3.4 Update total_value calculation: verify total_value = investments_value + cash_balance

## 4. TraderAgent Integration

- [x] 4.1 Update TraderAgent.get_portfolio() to include cash_balance in return
- [x] 4.2 Ensure position calculation uses only valid-ticker trades (skip 'n/a' symbols)
- [x] 4.3 Verify portfolio snapshot includes both investments_value and cash_balance

## 5. Data Migration

- [x] 5.1 Clear existing trades.db (delete all from trades table, delete all from cash_flows if exists)
- [x] 5.2 Re-import merged.csv using corrected logic
- [x] 5.3 Verify: open positions count equals expected holdings
- [x] 5.4 Verify: cash_balance matches SIPP Running Balance

## 6. Code Quality & Testing

- [x] 6.1 Run type checks: pyrefly check and verify no new type errors
- [x] 6.2 Run linting: ruff format . && ruff check . 
- [ ] 6.3 Manually test: run import_sipp() function, verify no errors logged
- [ ] 6.4 Verify portfolio tab displays 7 positions, correct cash balance, accurate P&L

## 7. Documentation & Finalization

- [x] 7.1 Update extraction-agent and trader-agent docstrings to document cash_flows table
- [ ] 7.2 Update CLAUDE.md or relevant wiki with quarterly update process for SIPP imports
- [ ] 7.3 Create commit message explaining SIPP import fix and cash flow tracking
- [ ] 7.4 Create PR with clear description of the bug fix and validation steps

