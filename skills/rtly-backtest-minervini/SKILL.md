---
kind: backtest-strategy
name: rtly-backtest-minervini
display_name: Minervini Backtest
description: >
  Backtest a deterministic long-only Minervini VCP breakout against bounded
  historical scan and daily OHLCV evidence with fixed-share sizing.
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

Use `scripts/strategy.py` through `StrategyProtocolV1`. Evaluate one configured
security after the session close and accept the engine's next-session-open
fill convention. Require current bounded daily history and visible monthly
scan evidence; emit nothing when evidence is missing, stale, or too short.

Enter only a Stage 2, valid, trend-template-passing VCP in `Breakout` state
whose score, volume, pivot, and pivot-extension gates all qualify. Exit the
full position on the configured loss threshold, a close below the current
50-session SMA, a non-Stage-2 scan, or `Invalid`/`Damaged` VCP state.

Do not pyramid, partially exit, simulate an intraday stop, access live state,
or fetch data outside the supplied bounded views.
