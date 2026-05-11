## Context

The SIPP CSV `Price` column holds the per-share price in the stock's **native currency** (USD for US stocks, pence for LSE stocks). This means:
- USD stock: `avg_cost` and `total_cost` are in **USD**
- GBP stock: `avg_cost` and `total_cost` are in **GBP** (after ÷100 pence conversion)

The original design assumed cost basis was always GBP. This was incorrect. The correct model is:

```
USD stock flow:
  price_cache stores: gbp_price (for ordering) + original_usd_price + currency='USD'
  Position.current_price = original_usd_price   (USD — matches cost basis)
  Position.current_value = usd_price × shares   (USD)
  Position.unrealised_pnl = value - cost        (USD — self-consistent)
  Summary totals = Σ(amount / gbpusd_rate)      (GBP)

GBP stock flow:
  price_cache stores: gbp_price (after GBp÷100) + original_pence_price + currency='GBP'
  Position.current_price = gbp_price            (GBP)
  Position.current_value = gbp_price × shares   (GBP)
  Summary totals = Σ(amount)                    (GBP)
```

## Goals / Non-Goals

**Goals:**
- Per-row: USD stocks show `$`, GBP stocks show `£` — all values self-consistent in native currency
- Summary cards (Total Cost, Market Value, P&L): always `£`, USD positions converted using live rate
- Exchange rate shown in toolbar
- Single rate fetch per refresh; rate cached in `price_cache` as `__GBPUSD__`

**Non-Goals:**
- Storing historical exchange rates or trade-time FX rates
- Supporting other foreign currencies (EUR, JPY etc.) — USD only for now

## Decisions

### 1. Fetch rate once per refresh as `GBPUSD=X`
`yf.Ticker("GBPUSD=X").fast_info.last_price`. Cached in `price_cache` as `__GBPUSD__` for reuse across page loads.

### 2. `_fetch_price_gbp` returns `(gbp_price, original_price, currency)`
`gbp_price` is used for the GBP-fallback path and the price floor check (`< 0.01`). `original_price` and `currency` are stored in `price_cache` and flow into `Position.price_currency`.

### 3. `_build_position` selects price based on currency
For USD stocks: `cp = display_info[ticker].original_price` (USD). For GBP stocks: `cp = current_prices[ticker]` (GBP). All downstream fields (current_value, unrealised_pnl) are then in the same currency.

### 4. GBP totals computed in `_render_portfolio`
`_to_gbp(amount, currency, fx)` converts USD amounts by dividing by the rate. Summary context vars `total_cost_gbp`, `total_value_gbp`, `total_pnl_gbp` are passed to the template.

### 5. Template uses `{% set sym = '$' if p.price_currency == 'USD' else '£' %}`
Single variable drives all currency symbols per row.

## Risks / Trade-offs

| Risk | Mitigation |
|------|-----------|
| Rate fetch fails | Fall back to cached `__GBPUSD__` rate; default 1.35 if neither available |
| Rate is stale between refresh clicks | Acceptable — user triggers refresh manually; rate shown in toolbar |
| Summary total currency mismatch if rate unavailable | Uses 1.35 fallback so totals remain approximately correct |
| Historical chart values (portfolio_value.csv) may not reflect USD correction | Pre-existing; chart shows historical GBP-approximated totals |
