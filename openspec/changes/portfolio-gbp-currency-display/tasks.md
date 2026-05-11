## 1. Exchange Rate Fetch

- [x] 1.1 Add `_fetch_gbpusd_rate()` helper in `web/app.py` that fetches `GBPUSD=X` via `yf.Ticker("GBPUSD=X").fast_info.last_price`
- [x] 1.2 On failure, fall back to last cached rate from `price_cache` row with ticker `__GBPUSD__`; log warning
- [x] 1.3 Store the fetched rate in `price_cache` as ticker `__GBPUSD__` via `save_price_cache({"__GBPUSD__": rate})`

## 2. Currency-Aware Price Fetch and Storage

- [x] 2.1 `_fetch_price_gbp()` returns `(gbp_price, original_price, currency)` — GBp÷100→GBP, USD stored as-is
- [x] 2.2 `_fetch_all_prices()` returns `(gbp_prices, display_info)` where `display_info = {ticker: (orig_price, currency)}`
- [x] 2.3 `price_cache` table extended with `currency` and `original_price` columns (migration in `_init_db`)
- [x] 2.4 `save_price_cache(prices, currencies)` persists currency and original price alongside GBP price
- [x] 2.5 `load_price_cache()` returns `(prices, fetched_at, display_info)` — three-tuple

## 3. Position Model and Builder

- [x] 3.1 Add `price_currency: str = "GBP"` to `Position` model in `models.py`
- [x] 3.2 `_build_position()` accepts `display_info`; for USD stocks uses original USD price as `current_price` so cost and value are in the same currency
- [x] 3.3 `get_portfolio()` and `refresh_portfolio_prices()` accept and thread `display_info` through to `_build_position`

## 4. Summary Totals in GBP

- [x] 4.1 `_to_gbp()` helper in `web/app.py` converts USD amounts using live rate
- [x] 4.2 `_render_portfolio()` pre-computes `total_cost_gbp`, `total_value_gbp`, `total_pnl_gbp`, `total_cost_gbp_valued` for the summary cards
- [x] 4.3 These are passed through template context alongside `positions_with_value`

## 5. Template: Currency Symbols

- [x] 5.1 Summary cards (Total Cost, Market Value, Unrealised P&L) always show `£` using pre-computed GBP totals
- [x] 5.2 Per-row monetary columns (Avg Cost, Price, Cost Basis, Market Value, P&L) show `$` for USD stocks, `£` for GBP stocks via `{% set sym = '$' if p.price_currency == 'USD' else '£' %}`
- [x] 5.3 Toolbar shows exchange rate: `Rate: £1 = ${{ "%.4f"|format(gbpusd_rate) }}` when rate is available

## 6. Validation

- [ ] 6.1 Verify USD stock per-row values (avg cost, price, cost basis, market value, P&L) show `$` and are self-consistent in USD
- [ ] 6.2 Verify GBP stock per-row values show `£` and are unchanged
- [ ] 6.3 Verify summary totals show `£` and correctly convert USD positions via GBPUSD rate
- [ ] 6.4 Verify toolbar shows rate and timestamp after Refresh Prices
- [ ] 6.5 Verify error case: stale rate used when network unavailable

## 7. Commit

- [ ] 7.1 Commit: `feat(portfolio): display USD stocks in $ and GBP stocks in £ with £ summary totals`
