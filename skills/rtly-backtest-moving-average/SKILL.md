---
kind: backtest-strategy
name: rtly-backtest-moving-average
display_name: Moving Average Backtest
description: >
  Backtests a deterministic, long-only fast/slow simple moving-average
  crossover for one security. Use to compare a transparent trend strategy
  with other replayable StrategyProtocolV1 methods.
api_version: 1
parameters:
  - name: security_id
    type: string
    default: sec-aapl
    description: Stable security identifier to trade.
    required: true
  - name: fixed_shares
    type: integer
    default: 10
    description: Number of shares to buy on an entry signal.
    required: false
    minimum: 1
    maximum: 100000
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

Use `scripts/strategy.py` through the Backtest Engine. Emit a BUY only when
the fast SMA crosses strictly above the slow SMA, and a SELL only when it
crosses strictly below. Require one session beyond the slow window to prove a
crossover, ignore equality, reject `fast_window >= slow_window`, and fail
closed on missing, malformed, short, or stale history.

Use `fixed_shares` for BUY sizing. Close only an integral held quantity for a
SELL. Make decisions from bounded close-of-session evidence; the engine fills
accepted signals at the next-session open.

```bash
uv run pytest skills/rtly-backtest-moving-average/scripts/tests -q
```
