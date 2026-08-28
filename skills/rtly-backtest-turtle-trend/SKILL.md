---
kind: backtest-strategy
name: rtly-backtest-turtle-trend
display_name: Turtle Trend Backtest
description: >
  Backtests a deterministic long-only Turtle trend system using bounded daily
  high and low channels. Use when comparing trend-following breakouts, tuning
  independent entry and exit lookbacks, or replaying strict Donchian-style
  channel signals in Strategy Manager.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: entry_lookback_sessions
    type: integer
    default: 20
    description: Prior sessions used to establish the entry high channel.
    required: true
    minimum: 2
    maximum: 252
  - name: exit_lookback_sessions
    type: integer
    default: 10
    description: Prior sessions used to establish the exit low channel.
    required: true
    minimum: 2
    maximum: 252
---

# Turtle Trend Backtest

Use `scripts/strategy.py` through Strategy Manager's in-process
`StrategyProtocolV1` runtime. The host binds the Run's canonical
selected-security tuple to `selected_securities`; iterate it in that order and
trade each selected security independently. The engine owns BUY allocation and
whole-share sizing. Enter when
the current high strictly exceeds the highest high in the
prior entry channel. Exit a held position when the current low strictly falls
below the lowest low in the prior exit channel.

Keep all decisions bounded to `view.as_of_session`. Exclude the current bar
from both channels, apply each channel's own warm-up, and treat equality at a
channel boundary as no signal. Return no signal for empty, stale, short, or
non-finite history.
