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
  - name: fixed_shares
    type: integer
    default: 10
    description: Number of shares requested for the initial purchase.
    required: false
    minimum: 1
    maximum: 100000
  - name: entry_on_or_after
    type: string
    default: '2000-01-01'
    description: Earliest ISO calendar date on which to request entry.
    required: false
---

# Buy and Hold Backtest

Use `scripts/strategy.py` through the Backtest Engine. The host binds the
Run's canonical selected-security tuple to `selected_securities`; iterate it
in that order and apply the same rule per security. On every fresh target
session on or after `entry_on_or_after`, emit the same deterministic BUY
candidate for each selected security with current history.
StrategyProtocolV1 does not expose run start or portfolio state to
`entry_signals`, so the engine rejects later candidates after the first fill
as position conflicts. Never emit SELL.

Fail closed on malformed cutoff dates and missing or stale target history. Use
`fixed_shares` for BUY sizing. Implement SELL sizing defensively for protocol
completeness by returning only an integral held quantity.

```bash
uv run pytest skills/rtly-backtest-buy-and-hold/scripts/tests -q
```
