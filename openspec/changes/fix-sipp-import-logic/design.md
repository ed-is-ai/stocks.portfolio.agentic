## Context

Currently, SIPP CSV import logic treats all rows with Quantity as stock trades, regardless of Symbol field validity. Rows with Symbol='n/a' (dividends, corporate actions) create phantom tickers, resulting in 96 open positions instead of the 7 actual holdings. Portfolio cash position is not tracked separately, making reconciliation impossible. The Running Balance column in the CSV represents the authoritative account total but is not currently used.

## Goals / Non-Goals

**Goals:**
- Reduce phantom open positions from 96 to 7 by filtering trades on valid Symbol field
- Separate non-trade cash flows (dividends, contributions, tax relief, interest) into dedicated table
- Use SIPP Running Balance as authoritative cash position source
- Enable audit trail via Reference field for all cash transactions
- Maintain compatibility with existing position calculation and P&L logic

**Non-Goals:**
- Implement automated SIPP API integration (manual CSV import remains the method)
- Add fee tracking or detailed transaction categorization beyond flow_type
- Rebuild historical cash flow statements
- Implement portfolio rebalancing or contribution forecasting

## Decisions

### 1. Separate trades and cash_flows into distinct database tables
**Decision**: Create a new `cash_flows` table in trades.db (existing schema) rather than a separate database.

**Rationale**: 
- Keeps all portfolio data in one place (consistency with trades)
- Simplifies the TraderAgent interface (no additional DB connections)
- Cash flows and trades share a common date/amount structure but different semantics

**Alternatives considered**:
- Single "transactions" table with type discriminator: More complex querying, mixes concerns
- Separate "cash.db": Added operational complexity, separate backup/restore concerns

### 2. Trade filtering: Symbol field as primary discriminator
**Decision**: Import as trade only if Symbol is valid (not 'n/a', not empty).

**Rationale**:
- Symbol='n/a' in SIPP export consistently indicates non-trades (dividends, contributions)
- Matches user's SIPP account statement structure (7 holdings all have valid symbols)
- Simple, explicit rule with no ambiguity

**Alternatives considered**:
- Parse Description field (harder to maintain, prone to breaks if descriptions change)
- Check for matching Quantity/Price/Amount arithmetic: Too fragile

### 3. Cash balance sourcing: Use Running Balance as source of truth
**Decision**: Extract the final non-null Running Balance from CSV as current cash balance. Do NOT recalculate from summed cash flows.

**Rationale**:
- Running Balance is the authoritative account total from the broker
- Eliminates risk of cash flow categorization errors accumulating
- Matches user's expectation (cash matches account statement)
- Simple and bulletproof

**Alternatives considered**:
- Sum all cash flows with starting balance: Requires perfect categorization, risk of accumulation error
- Request balance from API: Out of scope (manual import only)

### 4. Database schema for cash_flows table
```sql
CREATE TABLE cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    flow_type TEXT NOT NULL CHECK(flow_type IN ('CONTRIBUTION', 'DIVIDEND', 'INTEREST', 'TAX_RELIEF', 'TRANSFER', 'WITHDRAWAL', 'OTHER')),
    ticker TEXT,  -- nullable; only for DIVIDEND
    amount REAL NOT NULL,  -- always positive; direction determined by flow_type
    description TEXT,
    reference TEXT UNIQUE  -- from CSV Reference column, enables audit trail
);
```

**Rationale**:
- flow_type enum prevents categorization errors
- ticker links dividends back to source security
- reference provides audit trail to original CSV row
- amount as positive with flow_type direction (not signed) matches accounting convention

### 5. Portfolio value snapshot structure
**Decision**: Extend portfolio_value.csv with cash_balance and investments_value columns.

**New format**:
```
timestamp, total_value, total_cost, cash_balance, investments_value
2026-05-11T..., 577739.46, 531038.89, 46700.57, 531038.89
```

**Rationale**:
- Cash and investments subtotals enable reconciliation: total = investments + cash
- total_cost already exists; total_value remains the same
- Additive change (no breaking changes to existing columns)

## Risks / Trade-offs

| Risk | Mitigation |
|------|-----------|
| **Cash balance mismatch** - If Running Balance is incorrect/stale in CSV | Validate against broker statement manually; add warning to import log if balance is negative or too old |
| **Reference field uniqueness** - Duplicate references in CSV | Use UNIQUE constraint; log conflicts during import and skip duplicates |
| **Dividend ticker missing** - Some dividends may not have ticker in original CSV | Acceptable; record as None; UI can handle missing ticker case |
| **Legacy portfolio_value.csv** - Existing file has old format | Migration: Add columns with backfill NULL for cash_balance; fix during next portfolio update |
| **Performance** - Cash flow queries if table grows large | Acceptable; expect <1000 entries; index on date if needed later |

## Migration Plan

1. **Create cash_flows table schema** (backwards compatible, trades table unchanged)
2. **Update import_sipp() function**:
   - Check Symbol field before creating trade record
   - Route Symbol='n/a' rows to cash_flows
   - Extract final Running Balance, store as cash position
3. **Clear existing trades** (existing phantom positions)
4. **Reimport merged.csv** with corrected logic
5. **Update portfolio_value.csv** with new columns
6. **Update TraderAgent.get_portfolio()** to include cash_balance return value
7. **Test**: Verify 7 open positions, correct cash total matches account statement

**Rollback**: If needed, delete cash_flows table and revert import function; existing trades remain unaffected.

## Open Questions

1. Should cash_flows have amount as signed (negative for withdrawal) or always positive with flow_type direction? → **Decision: Always positive, direction from flow_type** (matches accounting)
2. Do we need to recalculate cash flows if more recent SIPP CSV is uploaded? → **Answer: Full reimport; clear both trades and cash_flows, reimport from merged.csv**
3. Should dividend ticker be required or optional? → **Answer: Optional; dividends without ticker are valid**

