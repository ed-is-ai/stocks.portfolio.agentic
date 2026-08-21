---
kind: backtest-strategy
name: rtly-backtest-weinstein
display_name: Weinstein Backtest
description: >
  Backtest a deterministic long-only Weinstein Stage 2 breakout across the
  Run's selected securities against bounded monthly scan and daily OHLCV
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
  - name: breakout_lookback_sessions
    type: integer
    default: 50
    description: Prior sessions used to establish the strict breakout high.
    required: true
    minimum: 2
    maximum: 252
  - name: minimum_relative_volume
    type: number
    default: 1.5
    description: Inclusive current-volume multiple of the prior 50-session mean.
    required: true
    minimum: 0.0
    maximum: 20.0
  - name: maximum_loss_pct
    type: number
    default: 10.0
    description: Inclusive loss threshold measured from average cost.
    required: true
    minimum: 0.0
    maximum: 100.0
---

# Weinstein Backtest

Use `scripts/strategy.py` through `StrategyProtocolV1`. The host binds the
Run's canonical selected-security tuple to `selected_securities`; iterate it
in that order and evaluate each selected security independently after the
session close, accepting the engine's next-session-open fill convention.
Require current bounded daily history and visible monthly scan evidence; emit
nothing for a security whose evidence is missing, stale, or too short.

Enter only when the visible scan and the dependency-neutral daily classifier
both report Stage 2, the close strictly exceeds the prior configured high, and
current volume meets the prior 50-session mean multiple. Exit the full
position at the configured loss threshold, on a non-Stage-2 scan, or when the
close falls below its current 150-session SMA.

Do not pyramid, partially exit, simulate an intraday stop, access live state,
or fetch data outside the supplied bounded views.
