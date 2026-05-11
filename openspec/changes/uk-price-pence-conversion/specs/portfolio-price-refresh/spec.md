## MODIFIED Requirements

### Requirement: Fetched prices are normalised to GBP regardless of yfinance currency
`portfolio-price-refresh` SHALL convert prices denominated in GBp (pence) to GBP by dividing by 100 before storing them in the price cache or passing them to position calculations.

#### Scenario: LSE ticker priced in pence
- **WHEN** `_fetch_last_price` retrieves a price for a ticker whose `fast_info.currency` is `GBp`
- **THEN** the returned price is divided by 100
- **AND** the GBP value is stored in `price_cache` and used in market value / P&L calculations

#### Scenario: USD or other non-pence ticker
- **WHEN** `_fetch_last_price` retrieves a price for a ticker whose currency is not `GBp`
- **THEN** the price is used as-is with no conversion

#### Scenario: Currency lookup fails
- **WHEN** `fast_info` raises an exception for a given symbol
- **THEN** a warning is logged and the raw price is used unchanged
