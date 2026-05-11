## ADDED Requirements

### Requirement: SIPP cash flows SHALL be tracked separately from trades
The system SHALL maintain a dedicated `cash_flows` table to track non-trade SIPP transactions (contributions, dividends, tax relief, interest, transfers, withdrawals) with flow type classification and reference to original CSV entries for audit trail.

#### Scenario: Extract dividend from SIPP CSV
- **WHEN** a CSV row has `Symbol='n/a'`, `Quantity` present, and `Debit` or `Credit` amount
- **THEN** the system classifies it as a DIVIDEND cash flow and stores it with flow_type='DIVIDEND'
- **AND** the dividend amount is recorded in the cash_flows table with reference to the original transaction

#### Scenario: Extract contribution from SIPP CSV
- **WHEN** a CSV row has description containing "CONTRIBUTION" and `Credit` amount with no `Quantity`
- **THEN** the system classifies it as CONTRIBUTION and stores amount with flow_type='CONTRIBUTION'
- **AND** the transaction is NOT imported as a trade

#### Scenario: Classify interest and tax relief
- **WHEN** a CSV row has description containing "interest" or "tax relief" and `Credit` amount
- **THEN** the system classifies it correctly (INTEREST or TAX_RELIEF) and stores in cash_flows table
- **AND** these entries add to the available cash balance

#### Scenario: Track cash flow metadata
- **WHEN** cash flows are imported
- **THEN** each entry captures: date, flow_type, amount, optional ticker (for dividends), description, and reference (from CSV Reference column)
- **AND** reference field enables audit trail back to original CSV transaction

### Requirement: Portfolio cash position SHALL use Running Balance as source of truth
The system SHALL extract the final Running Balance from SIPP CSV and use it as the current cash position, rather than recalculating from cash flows.

#### Scenario: Extract final running balance
- **WHEN** SIPP CSV is imported
- **THEN** the system identifies the last non-null "Running Balance" entry in the CSV
- **AND** stores this value as the current cash balance in portfolio_value.csv

#### Scenario: Cash balance reconciliation
- **WHEN** portfolio value is calculated
- **THEN** total portfolio = (sum of position costs) + (final running balance)
- **AND** this matches the account statement total value

### Requirement: Trade filtering SHALL exclude non-trade rows
The system SHALL only import rows as trades where the Symbol field contains a valid ticker (not 'n/a' or empty), regardless of whether Quantity is present.

#### Scenario: Skip rows with Symbol='n/a'
- **WHEN** a CSV row has `Symbol='n/a'` but contains `Quantity` data
- **THEN** the system does NOT create a trade entry
- **AND** instead routes it to cash_flows based on the Description and Amount fields

#### Scenario: Import only valid ticker trades
- **WHEN** a CSV row has a valid `Symbol` (not 'n/a') AND `Quantity > 0` AND Debit/Credit
- **THEN** the system imports it as a trade (BUY if Debit, SELL if Credit)
- **AND** open positions are calculated only from these valid ticker trades

