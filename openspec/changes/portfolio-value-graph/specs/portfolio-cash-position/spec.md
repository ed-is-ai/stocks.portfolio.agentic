## MODIFIED Requirements

### Requirement: Portfolio history includes cash balance per snapshot
The `_load_portfolio_history()` function SHALL return a `cash_values` list alongside `labels`, `values`, and `costs`, enabling charts to plot cash as a separate series.

#### Scenario: History dict contains cash_values key
- **WHEN** `_load_portfolio_history()` is called
- **THEN** the returned dict contains a `cash_values` key with a list of the same length as `labels`

#### Scenario: Existing behaviour unchanged
- **WHEN** `_load_portfolio_history()` is called
- **THEN** `labels`, `values`, and `costs` behave identically to before this change
