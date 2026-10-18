---
kind: backtest-strategy
name: rtly-backtest-mean-reversion
display_name: Mean Reversion Backtest
description: >
  Backtests a deterministic long-only mean-reversion strategy across the
  Run's selected securities: buy a close that is statistically oversold
  against its recent mean while the long-term trend is up, sell on reversion
  to the mean, a maximum-loss stop or a time stop.
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
  - name: trend_lookback_sessions
    type: integer
    default: 200
    description: Trend gate. Buy only when the close is above this rising simple moving average.
    required: false
    minimum: 10
    maximum: 504
  - name: maximum_loss_pct
    type: number
    default: 8.0
    description: Inclusive loss threshold measured from average cost.
    required: true
    minimum: 0.0
    maximum: 99.0
---

# Mean Reversion Backtest

Use `scripts/strategy.py` through the Backtest Engine. The host binds the
Run's selected-security tuple to `selected_securities`; the strategy sorts and
de-duplicates it, then evaluates each security independently after the
session close.

The z-score is `(close - mean) / std` over the latest `lookback_sessions`
closes (population standard deviation, current close included). Emit a BUY
when z is at or below `-entry_zscore` and the long-term trend is up: the close
is above the `trend_lookback_sessions` simple moving average and that average
rose since the prior session (today's close above the close
`trend_lookback_sessions` sessions ago). Oversold in a downtrend is no BUY.

For a held position emit one SELL, checked in this order:

1. maximum-loss stop: close `<= average_cost x (1 - maximum_loss_pct / 100)`;
2. reversion: z at or above `exit_zscore`;
3. time stop: held `max_holding_sessions` sessions (the entry session counts
   as the first), read from `PositionSummaryV1.entry_session` and skipped when
   the host does not supply it. It fires even when no z-score can be computed
   (e.g. flat prices).

`stop_level` reports the maximum-loss stop (`basis: average_cost`,
`trigger: close_lte`). Evidence covers
`max(lookback_sessions, trend_lookback_sessions + 1, max_holding_sessions + 1)`
closes. Fail closed on missing, malformed, short, stale or zero-variance
history, a missing or unusable `maximum_loss_pct` (never a default), an
`entry_zscore` above `sqrt(lookback_sessions - 1)` (unreachable), or an
`exit_zscore` at or below `-entry_zscore`.

Rank BUYs by depth of oversold (most negative z first, then security ID) via
descending positive ordinal `Signal.priority`, so the position cap keeps the
deepest dislocations.

The engine owns BUY allocation and whole-share sizing. A SELL closes the exact
held quantity. Do not pyramid, partially exit, or fetch data outside the
supplied bounded views.

```bash
uv run pytest skills/rtly-backtest-mean-reversion/scripts/tests -q
```
