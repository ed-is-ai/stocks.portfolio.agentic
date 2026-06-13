## ADDED Requirements

### Requirement: Portfolio chart shows cash as a separate line
The portfolio value chart SHALL render a Cash line in addition to the existing Market Value and Cost Basis lines, plotting the stored cash balance at each historical snapshot point.

#### Scenario: Cash line appears when history contains cash data
- **WHEN** `portfolio_value.csv` contains rows with a non-null `cash_balance` column
- **THEN** the chart renders a third dataset labelled "Cash" with those values

#### Scenario: Chart renders with no cash history
- **WHEN** all snapshot rows have no `cash_balance` value (older rows)
- **THEN** the Cash dataset renders as null/gap points and does not error

#### Scenario: Cash line is visually distinct
- **WHEN** the chart is displayed
- **THEN** the Cash line uses a dashed style and colour distinct from Market Value and Cost Basis

### Requirement: `_load_portfolio_history` returns cash values
The `_load_portfolio_history()` function SHALL include a `cash_values` list in its return dict, one entry per snapshot row, defaulting to `None` for rows missing the column.

#### Scenario: Cash values loaded from CSV
- **WHEN** `portfolio_value.csv` contains a `cash_balance` column
- **THEN** `_load_portfolio_history()["cash_values"]` contains the corresponding float values

#### Scenario: Missing column handled gracefully
- **WHEN** a CSV row has no `cash_balance` column
- **THEN** the corresponding entry in `cash_values` is `None`
