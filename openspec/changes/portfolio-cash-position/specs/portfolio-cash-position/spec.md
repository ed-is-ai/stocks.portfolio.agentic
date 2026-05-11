## ADDED Requirements

### Requirement: Cash balance is persisted at SIPP import time
The system SHALL store the SIPP Running Balance in the `account_state` SQLite table whenever `import_sipp()` is called. The value SHALL be accessible via `get_cash_balance()` returning `float | None`.

#### Scenario: Cash balance saved on import
- **WHEN** `import_sipp()` processes a CSV with a valid Running Balance
- **THEN** `account_state` table contains `key='cash_balance'` with that amount
- **AND** `get_cash_balance()` returns the same float value

#### Scenario: Cash balance unavailable before first import
- **WHEN** `get_cash_balance()` is called and no import has occurred
- **THEN** it returns `None`

### Requirement: Portfolio tab displays a CASH row
The portfolio positions table SHALL include a CASH row showing the stored cash balance, positioned after all stock positions.

#### Scenario: CASH row is rendered when balance is known
- **WHEN** the portfolio partial is loaded and `get_cash_balance()` returns a value
- **THEN** a row with ticker "CASH" appears in the positions table
- **AND** Cost Basis and Market Value both show the cash balance in £
- **AND** P&L shows £0.00 (0%)
- **AND** no Adjust or Sell buttons are shown for the CASH row

#### Scenario: No CASH row when balance is unknown
- **WHEN** `get_cash_balance()` returns `None`
- **THEN** no CASH row appears in the portfolio table

### Requirement: Cash is included in GBP summary totals
The Total Cost and Market Value summary cards SHALL include the cash balance in their GBP totals.

#### Scenario: Summary totals include cash
- **WHEN** cash balance is £5,000 and stock positions total £20,000 cost / £22,000 value
- **THEN** Total Cost card shows £25,000
- **AND** Market Value card shows £27,000
- **AND** Unrealised P&L card shows £2,000 (stock P&L only — cash contributes £0)
