---
kind: backtest-strategy
name: rtly-backtest-minervini
display_name: Minervini Backtest
description: >
  Backtest a deterministic long-only Minervini VCP breakout across the Run's
  selected securities against bounded historical scan and daily OHLCV
  evidence with fixed-share sizing.
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
    description: Whole shares requested for each entry.
    required: true
    minimum: 1
    maximum: 100000
  - name: minimum_vcp_score
    type: integer
    default: 70
    description: Inclusive minimum VCP score.
    required: true
    minimum: 0
    maximum: 100
  - name: minimum_trend_score
    type: number
    default: 85.0
    description: Inclusive minimum trend-template score.
    required: true
    minimum: 0.0
    maximum: 100.0
  - name: minimum_relative_volume
    type: number
    default: 1.5
    description: Inclusive current-volume multiple of the prior 50-session mean.
    required: true
    minimum: 0.0
    maximum: 20.0
  - name: maximum_pivot_extension_pct
    type: number
    default: 3.0
    description: Inclusive maximum close extension above the VCP pivot.
    required: true
    minimum: 0.0
    maximum: 100.0
  - name: maximum_loss_pct
    type: number
    default: 8.0
    description: Inclusive loss threshold measured from average cost.
    required: true
    minimum: 0.0
    maximum: 100.0
---

# Minervini Backtest

Use `scripts/strategy.py` through `StrategyProtocolV1`. The host binds the
Run's canonical selected-security tuple to `selected_securities`; iterate it
in that order and evaluate each selected security independently after the
session close, accepting the engine's next-session-open fill convention.
Require current bounded daily history and visible monthly scan evidence; emit
nothing for a security whose evidence is missing, stale, or too short.

Enter only a Stage 2, valid, trend-template-passing VCP in `Breakout` state
whose score, volume, pivot, and pivot-extension gates all qualify. Exit the
full position on the configured loss threshold, a close below the current
50-session SMA, a non-Stage-2 scan, or `Invalid`/`Damaged` VCP state.

Do not pyramid, partially exit, simulate an intraday stop, access live state,
or fetch data outside the supplied bounded views.
