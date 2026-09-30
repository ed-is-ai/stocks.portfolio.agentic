---
kind: backtest-strategy
name: rtly-backtest-moving-average
display_name: Moving Average Backtest
description: >
  Backtests a deterministic, long-only fast/slow simple moving-average
  crossover across the Run's selected securities. Use to compare a
  transparent trend strategy with other replayable StrategyProtocolV1
  methods.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: fast_window
    type: integer
    default: 50
    description: Sessions in the fast simple moving average.
    required: false
    minimum: 1
    maximum: 1000
  - name: slow_window
    type: integer
    default: 200
    description: Sessions in the slow simple moving average.
    required: false
    minimum: 2
    maximum: 2000
---

# Moving Average Backtest

Use `scripts/strategy.py` through the Backtest Engine. The host binds the
Run's canonical selected-security tuple to `selected_securities`; iterate it
in that order and apply the crossover rule independently per security. Emit a
BUY only when the fast SMA crosses strictly above the slow SMA, and a SELL
only when it crosses strictly below a held position. Require one session
beyond the slow window to prove a crossover, ignore equality, reject
`fast_window >= slow_window`, and fail closed on missing, malformed, short,
or stale history.

Suggest a stop (`stop_level`) for a held position at the close below which
tomorrow's fast SMA would cross below the slow SMA:
`(fast * Ss - slow * Sf) / (slow - fast)`, where `Sf` and `Ss` sum today's
latest `fast - 1` and `slow - 1` closes. Declare no level, with a reason, when
the fast SMA is already below the slow one, the price is not positive, or
current closes are too short.

The engine owns BUY allocation and whole-share sizing. A SELL closes the exact
held quantity, including split-created fractional shares. Make decisions from bounded close-of-session
evidence; the engine fills accepted signals at the next-session open.

```bash
uv run pytest skills/rtly-backtest-moving-average/scripts/tests -q
```
