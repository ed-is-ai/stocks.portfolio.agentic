## 1. Price Conversion Fix

- [ ] 1.1 In `_fetch_last_price()` in `web/app.py`, after computing the raw price, call `yf.Ticker(yf_sym).fast_info.currency` and divide by 100 if currency is `GBp`
- [ ] 1.2 Wrap the currency check in try/except; log warning and use raw price on failure

## 2. Validation

- [ ] 2.1 Verify UK-listed trust/ETF prices are ~100× lower than the raw GBp value (correct GBP conversion)
- [ ] 2.2 Verify US-listed stock prices are unchanged (USD, no division)
- [ ] 2.3 Verify aliased fund price is correct (GBp fund mapped via ticker_aliases.json)

## 3. Commit

- [ ] 3.1 Commit: `fix(portfolio): divide GBp prices by 100 to normalise to GBP`
