## Context

LSE-listed securities trade in pence but Yahoo Finance exposes this as the `GBp` currency code in `fast_info.currency`. The cost basis for all positions is stored in GBP (pounds). Without conversion, a price returned in GBp is passed directly as a GBP value, making market value ~100× too large for affected UK-listed positions.

Confirmed via `yf.Ticker(sym).fast_info.currency`: several LSE-listed holdings in the portfolio return `GBp`. US-listed holdings return `USD` and are unaffected.

## Goals / Non-Goals

**Goals:**
- Divide fetched price by 100 whenever yfinance reports `GBp` currency
- Apply consistently to both direct tickers and aliased tickers (e.g. fund aliases in `ticker_aliases.json`)

**Non-Goals:**
- Converting stored trade prices or cost basis (they are already in GBP)
- Handling other pence-denominated exchanges (ZAp, ILa etc.) — only GBp for now

## Decisions

### Currency check via `fast_info`
`yf.Ticker(sym).fast_info.currency` is a lightweight call that doesn't fetch full quote data. Called once per ticker per refresh alongside the price download — negligible overhead.

### Division at fetch time, not display time
Normalising at fetch means `price_cache`, `Position.current_price`, and all downstream calculations (market value, P&L) are always in GBP. No changes needed elsewhere in the stack.

### Graceful fallback
If `fast_info` raises an exception (network, unknown ticker), log a warning and use the raw price — same graceful behaviour as today.

## Risks / Trade-offs

| Risk | Mitigation |
|------|-----------|
| `fast_info` call adds latency (~0.1s per ticker) | Acceptable for manual refresh; portfolio size keeps total added latency under 1s |
| Aliased tickers (e.g. fund mapped via `ticker_aliases.json`) — also GBp | Same code path handles them automatically via the yfinance symbol |
| Future new positions on other exchanges | Only GBp is special-cased; others pass through unchanged |
