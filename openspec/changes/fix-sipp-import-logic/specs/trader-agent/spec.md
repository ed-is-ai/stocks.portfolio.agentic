## ADDED Requirements

### Requirement: Trader Agent SHALL import SIPP transactions with correct trade filtering
Trader Agent reads SIPP CSV export and imports stock transactions as trades while separately capturing cash flows. Trade filtering requires valid Symbol field (not 'n/a'); rows with Symbol='n/a' are routed to cash_flows table as dividends, contributions, or other cash movements.

#### Scenario: Import SIPP stock trade with valid ticker
- **WHEN** CSV row has valid `Symbol` (e.g., "GOOGL"), `Quantity > 0`, and Debit or Credit amount
- **THEN** Trader Agent:
  - Creates a trade record: ticker=GOOGL, action=BUY (if Debit) or SELL (if Credit)
  - Stores: date, shares, price per share
  - Writes to trades database

#### Scenario: Skip non-trade CSV rows with Symbol='n/a'
- **WHEN** CSV row has `Symbol='n/a'` with Quantity (e.g., "87 UNILEVER Del") and Credit amount
- **THEN** Trader Agent:
  - Does NOT create a trade record
  - Classifies as dividend/other cash flow based on Description
  - Writes to cash_flows table, not trades table
  - Preserves reference for audit trail

#### Scenario: Import cash flow entries (contributions, tax relief, interest)
- **WHEN** CSV rows have no Quantity or Quantity='n/a' but have Credit/Debit amounts
- **THEN** Trader Agent:
  - Classifies by Description: "CONTRIBUTION", "tax relief" → TAX_RELIEF, "interest" → INTEREST, "Div" → DIVIDEND
  - Extracts amount from Credit or Debit field
  - Stores in cash_flows table with flow_type and optional ticker (for dividends)
  - These entries do NOT affect open positions, only cash balance

### Requirement: Trader Agent portfolio calculation SHALL use Running Balance for cash
Trader Agent calculates total portfolio value using open positions (from valid trades) plus cash balance extracted from SIPP Running Balance field, not by summing cash flows.

#### Scenario: Calculate portfolio with correct cash balance
- **WHEN** portfolio is calculated after importing SIPP CSV
- **THEN** Trader Agent:
  - Replays valid ticker trades to derive open positions and cost basis
  - Extracts final Running Balance from CSV as cash balance
  - Total portfolio = sum(position costs) + Running Balance
  - Writes to portfolio_value.csv: timestamp, total_value, total_cost, cash_balance, investments_value

#### Scenario: Portfolio reflects 7 open positions not 96
- **WHEN** SIPP CSV is imported with correct trade filtering
- **THEN** open positions count equals actual holdings (7 in user account)
- **AND** positions match expected tickers: GOOGL, INTC, RIO, SGLN, SMT, WCOG, HSBC FTSE

