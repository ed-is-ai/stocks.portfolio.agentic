## ADDED Requirements

### Requirement: Ticker alias config maps internal symbols to Yahoo Finance symbols
The system SHALL read `data/ticker_aliases.json` at refresh time and substitute any matching internal tickers with their configured Yahoo Finance equivalents before fetching prices.

#### Scenario: Alias exists for an internal ticker
- **WHEN** `data/ticker_aliases.json` contains a mapping for an internal ticker
- **AND** the portfolio holds a position with that internal ticker
- **THEN** the refresh fetches price using the mapped Yahoo Finance symbol
- **AND** the returned price is stored against the original internal ticker position

#### Scenario: Alias file is absent
- **WHEN** `data/ticker_aliases.json` does not exist
- **THEN** refresh proceeds without aliases (existing behaviour unchanged)
- **AND** no error is raised

#### Scenario: Ticker has no alias and no yfinance match
- **WHEN** a ticker has no alias entry
- **AND** yfinance returns NaN for both the raw ticker and the `.L` suffix variant
- **THEN** the position retains `—` for price (existing graceful behaviour)
