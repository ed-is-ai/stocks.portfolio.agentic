---
kind: backtest-strategy
name: rtly-backtest-darvas-box
display_name: Darvas Box Backtest
description: >
  Backtests a deterministic long-only Darvas box breakout using bounded daily
  OHLCV history, prior-window box depth, and volume confirmation. Use when
  comparing box breakouts, tuning Darvas lookbacks, or replaying strict
  breakout and box-bottom exit rules in Strategy Manager.
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
    description: Whole shares to buy for each accepted entry signal.
    required: true
    minimum: 1
    maximum: 100000
  - name: box_lookback_sessions
    type: integer
    default: 20
    description: Prior sessions used to establish the box top and bottom.
    required: true
    minimum: 2
    maximum: 252
  - name: maximum_box_depth_pct
    type: number
    default: 15
    description: Maximum inclusive percentage depth of a qualifying box.
    required: true
    minimum: 0
    maximum: 100
  - name: volume_multiplier
    type: number
    default: 1.5
    description: Minimum inclusive current-volume multiple of prior-box mean volume.
    required: true
    minimum: 0
    maximum: 100
---

# Darvas Box Backtest

Use `scripts/strategy.py` through Strategy Manager's in-process
`StrategyProtocolV1` runtime. The host binds the Run's canonical
selected-security tuple to `selected_securities`; iterate it in that order and
trade each selected security independently with fixed-share sizing. Enter when
the current close strictly exceeds the prior-window box top,
the box depth is within the configured maximum, and current volume is at least
the configured multiple of the prior-window mean. Exit a held position when the
current close is strictly below the prior-window box bottom.

Keep all decisions bounded to `view.as_of_session`. Exclude the current bar
from every box and volume reference window; treat equality at a price boundary
as no signal and equality at depth or volume thresholds as qualifying. Return
no signal for empty, stale, short, or non-finite history.
