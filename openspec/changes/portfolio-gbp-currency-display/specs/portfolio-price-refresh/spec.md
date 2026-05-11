## MODIFIED Requirements

### Requirement: All portfolio monetary values are expressed in GBP
The portfolio SHALL display all prices, market values, and P&L figures in GBP (£), converting USD-denominated live prices using the live GBP/USD exchange rate fetched at refresh time.

#### Scenario: USD-denominated position price is converted to GBP
- **WHEN** `_fetch_last_price` retrieves a price for a ticker whose `fast_info.currency` is `USD`
- **THEN** the price is divided by the live `GBPUSD=X` rate
- **AND** the resulting GBP price is stored in `price_cache`
- **AND** market value and P&L calculations use the GBP price

#### Scenario: GBP-denominated position price is unchanged
- **WHEN** `_fetch_last_price` retrieves a price for a ticker whose currency is `GBP` or `GBp` (already converted)
- **THEN** no additional FX conversion is applied

#### Scenario: Exchange rate fetch fails
- **WHEN** `GBPUSD=X` cannot be fetched (network error, timeout)
- **THEN** the last cached rate (stored as `__GBPUSD__` in `price_cache`) is used if available
- **AND** a warning is logged; refresh proceeds with stale rate rather than failing entirely

#### Scenario: Portfolio template uses £ symbol
- **WHEN** the portfolio partial is rendered
- **THEN** all monetary values display with `£` symbol, not `$`
- **AND** the toolbar shows the exchange rate used: e.g. `Rate: £1 = $1.36`
