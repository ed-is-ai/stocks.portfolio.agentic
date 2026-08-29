---
kind: backtest-strategy
name: rtly-backtest-buy-and-hold
display_name: Buy and Hold Backtest
description: >
  Backtests a deterministic passive buy-and-hold benchmark across the Run's
  selected securities. Use to establish a baseline when comparing active
  replayable StrategyProtocolV1 methods.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: entry_on_or_after
    type: string
    default: '2000-01-01'
    description: Earliest ISO calendar date on which to request entry.
    required: false
  - name: top_x
    type: integer
    default: 10
    minimum: 1
    description: Number of strongest eligible securities to buy once at the first Run session.
    required: false
---

# Buy and Hold Backtest

Use `scripts/strategy.py` through the Backtest Engine. The host binds the
Run's canonical selected-security tuple to `selected_securities`. On the first
normalized Run session only, rank every member by its trailing return (252
sessions): the last close strictly before selection divided by the close 252
security sessions earlier, minus one. A security with fewer than 253 valid
positive split-adjusted closing prices strictly before selection is
disqualified as insufficient history; missing or invalid price inputs are also
excluded with a plain-language reason in the completed Result.
Select the top `top_x` (default 10) by return descending and canonical security
ID ascending; record a stable excluded decision for every other member. Never
rerank, rebalance, emit an ordinary entry candidate, or emit SELL.

Fail closed into an auditable exclusion for malformed cutoff dates and missing,
short, stale, non-finite, or non-positive bounded history. The engine owns BUY
allocation and whole-share sizing. Implement SELL sizing defensively for
protocol completeness by returning only an integral held quantity.

```bash
uv run pytest skills/rtly-backtest-buy-and-hold/scripts/tests -q
```
