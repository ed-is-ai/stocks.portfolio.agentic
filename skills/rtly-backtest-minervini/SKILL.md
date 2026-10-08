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
    default: 0
    description: >-
      Inclusive minimum VCP score. The score ranks competing candidates; the
      default floor of 0 only excludes a scan with no VCP score at all.
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
      Sell the held position with the weakest current 12-to-1 momentum when a
      qualifying unheld candidate clears the configured momentum lead.
    required: true
  - name: upgrade_score_margin
    type: integer
    default: 15
    description: >-
      Minimum momentum lead in percentage points required for the strongest
      unheld candidate to replace the weakest held position.
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

Enter only a Stage 2, trend-template-passing security whose monthly scan
state shows an intact base (`Pre-breakout`, `Breakout` or
`Early-post-breakout`) and whose trend score, VCP score floor, daily volume,
pivot, and pivot-extension gates all qualify. A validated multi-contraction
VCP is not required: flat bases and other non-VCP setups qualify too. Rank the
complete qualifying batch by VCP score descending, then 21-session price
momentum descending within equal VCP scores, then security ID ascending. The
breakout itself is detected on the daily session, not from the scan's
`Breakout` state, which only describes the snapshot session. Momentum uses
`close[-22] / close[-253] - 1` from 253 bounded Run-currency closes. Encode the
order as descending positive ordinal `Signal.priority` values and explain raw
VCP and momentum evidence, endpoint dates, currency, rank and any missing-data
reason. Momentum is ranking-only: absent capability, short history or
unavailable endpoints keep an otherwise qualifying BUY and place missing
momentum after valid momentum at the same VCP score. The VCP score remains the
separate raw value used by the position-upgrade rule. Exit the
full position on the configured loss threshold, a close below the current
50-session SMA, a non-Stage-2 scan, or `Invalid`/`Damaged` VCP state.

When `enable_position_upgrade` is true and all configured position slots are
occupied after mechanical exits, also sell the held position with the lowest
current 12-to-1 momentum when the strongest unheld qualifying candidate's
12-to-1 momentum leads by at least `upgrade_score_margin` percentage points.
Use the same Run-currency reading as the entry-ranking momentum:
`close[-22] / close[-253] - 1`. A position or candidate without valid current
momentum evidence cannot be ranked for an upgrade. The candidate must still
pass the normal Minervini entry qualification. This mirrors Minervini's own
"upgrading" discipline; it never overrides the mechanical exits above and
never buys anything itself -- the freed cash is picked up by the ordinary
entry path on a later qualifying session.

Suggest a stop (`stop_level`) for a held position at the close where
tomorrow's own exit would fire: the higher of the maximum-loss stop from
average cost and the close that breaks the next session's 50-session SMA (the
mean of today's latest 49 closes). Declare no level, with a reason, when
current closes are too short or `maximum_loss_pct` is missing or unusable
(never a default).

The engine owns BUY allocation and whole-share sizing.

Do not pyramid, partially exit, simulate an intraday stop, access live state,
or fetch data outside the supplied bounded views.
