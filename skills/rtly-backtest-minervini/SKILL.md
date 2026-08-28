---
kind: backtest-strategy
name: rtly-backtest-minervini
display_name: Minervini Backtest
description: >
  Backtest a deterministic long-only Minervini VCP breakout across the Run's
  selected securities against bounded historical scan and daily OHLCV
  evidence with engine-owned equal-capital allocation.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
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
  - name: enable_position_upgrade
    type: boolean
    default: false
    description: >-
      Sell the weakest held position (by current VCP score) when a stronger
      unheld candidate clears the configured score margin.
    required: true
  - name: upgrade_score_margin
    type: integer
    default: 15
    description: >-
      Minimum VCP score edge the strongest unheld candidate must hold over
      the weakest held position before an upgrade exit fires.
    required: true
    minimum: 0
    maximum: 100
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

When `enable_position_upgrade` is true, also sell the weakest held position
(lowest current VCP score) when the strongest unheld qualifying candidate's
VCP score exceeds the weakest holding's by at least `upgrade_score_margin`.
This mirrors Minervini's own "upgrading" discipline; it never overrides the
mechanical exits above and never buys anything itself -- the freed cash is
picked up by the ordinary entry path on a later qualifying session.

The engine owns BUY allocation and whole-share sizing.

Do not pyramid, partially exit, simulate an intraday stop, access live state,
or fetch data outside the supplied bounded views.
