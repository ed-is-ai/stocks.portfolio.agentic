---
kind: backtest-strategy
name: rtly-backtest-52-week-high
display_name: 52-Week High Backtest
description: >
  Backtests a deterministic long-only 52-week-high proximity rule (George and
  Hwang, 2004): buy when the close is near the highest high of the lookback
  window, rank by that proximity, and sell when it falls away or a
  maximum-loss stop fires. Uses price history only. Use when testing whether
  nearness to the 52-week high predicts returns in Strategy Manager.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: high_lookback_sessions
    type: integer
    default: 252
    description: Sessions in the high window, ending on (and including) today.
    required: true
    minimum: 20
    maximum: 1000
  - name: entry_proximity
    type: number
    default: 0.95
    description: Minimum inclusive close / window-high ratio to enter.
    required: true
    minimum: 0.01
    maximum: 1.0
  - name: exit_proximity
    type: number
    default: 0.85
    description: >-
      Held positions exit when close / window-high falls below this ratio.
      Must be below entry_proximity.
    required: true
    minimum: 0.0
    maximum: 0.99
  - name: rebalance_cadence
    type: enum
    default: daily
    enum_values: [daily, monthly]
    description: >-
      Evaluate entries and the proximity exit every session, or only on the
      first session of a new month.
    required: true
  - name: maximum_loss_pct
    type: number
    default: 10.0
    description: Inclusive loss threshold measured from average cost.
    required: true
    minimum: 0.0
    maximum: 99.9
---

# 52-Week High Backtest

Use `scripts/strategy.py` through Strategy Manager's in-process
`StrategyProtocolV1` runtime. `selected_securities` is re-sorted and
deduplicated by the Strategy, so iteration order never depends on the caller.
The engine owns BUY allocation and whole-share sizing. Entries respect the
host regime filter (`entry_signals_permitted`).

Proximity is `close / max(high)` over the latest `high_lookback_sessions`
price-history rows ending on `view.as_of_session`. Today's high is in the
window, so a new high gives proximity `1.0`. No proximity reading (so no entry
and no proximity exit) without exactly that many rows, a bar on the as-of
session, and positive finite highs and close.

Parameters fail closed (no signals at all, including the risk stop) when any
is missing or unusable, or when `exit_proximity >= entry_proximity`, which
would let one reading both enter and exit; `stop_level` then reports
`invalid_setting`.

Enter when proximity is at or above `entry_proximity`. `Signal.priority` is
the proximity, so the position cap fills the names nearest their high first.
Proximity is capped at `1.0` (a close above every window high is bad OHLC
data). Equal proximities, common at new highs, keep security-id order.

Exit a held position when either fires (one SELL carrying both reasons):

- Proximity exit: proximity below `exit_proximity`.
- Risk stop: the close on `view.as_of_session` is at or below
  `average_cost * (1 - maximum_loss_pct / 100)`. It needs only the current
  close, so it fires even with short history. `stop_level` reports this level.

`rebalance_cadence: monthly` gates entries and the proximity exit to the first
session of a new calendar month (the previous session is the latest one any
fresh universe security traded, so one security's gap cannot fake a
rebalance). A run starting mid-month makes its first entries next month, and
a held name below `exit_proximity` mid-month is kept until the next rebalance.
The risk stop is evaluated every session and needs a positive close.

```bash
uv run pytest skills/rtly-backtest-52-week-high/scripts/tests -q
```
