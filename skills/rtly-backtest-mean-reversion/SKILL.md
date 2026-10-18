---
kind: backtest-strategy
name: rtly-backtest-mean-reversion
display_name: Mean Reversion Backtest
description: >
  Backtests a deterministic long-only mean-reversion strategy across the
  Run's selected securities: buy a close that is statistically oversold
  against its recent mean, sell on reversion to the mean or after a time stop.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: lookback_sessions
    type: integer
    default: 20
    description: Sessions in the rolling mean and standard deviation.
    required: false
    minimum: 5
    maximum: 252
  - name: entry_zscore
    type: number
    default: 2.0
    description: Enter when the close is at least this many standard deviations below the mean.
    required: false
    minimum: 0.5
    maximum: 10.0
  - name: exit_zscore
    type: number
    default: 0.0
    description: Exit when the close has recovered to this z-score or higher.
    required: false
    minimum: -5.0
    maximum: 5.0
  - name: max_holding_sessions
    type: integer
    default: 10
    description: Time stop. Exit once a position has been held this many sessions.
    required: false
    minimum: 1
    maximum: 252
---

# Mean Reversion Backtest

Use `scripts/strategy.py` through the Backtest Engine. The host binds the
Run's canonical selected-security tuple to `selected_securities`; iterate it in
that order and evaluate each security independently after the session close.

The z-score is `(close - mean) / std` over the latest `lookback_sessions`
closes (population standard deviation, current close included). Emit a BUY
when z is at or below `-entry_zscore`. Emit a SELL for a held position when z
is at or above `exit_zscore`, or when it has been held `max_holding_sessions`
sessions (the time stop; the entry session counts as the first). The time stop
reads `PositionSummaryV1.entry_session`; it is skipped when the host does not
supply it. Fail closed on missing, malformed, short, stale or zero-variance
history.

Rank BUYs by depth of oversold (most negative z first, then security ID) via
descending positive ordinal `Signal.priority`, so the position cap keeps the
deepest dislocations.

The engine owns BUY allocation and whole-share sizing. A SELL closes the exact
held quantity. Do not pyramid, partially exit, or fetch data outside the
supplied bounded views.

```bash
uv run pytest skills/rtly-backtest-mean-reversion/scripts/tests -q
```
