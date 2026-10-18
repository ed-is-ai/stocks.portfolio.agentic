---
kind: backtest-strategy
name: rtly-backtest-institutional-flow
display_name: Institutional Flow Backtest
description: >
  Backtests a deterministic long-only institutional-flow rule that buys when
  WhaleWisdom 13F net fund buying (funds_net) is strong and sells on net
  selling or a maximum-loss risk stop. Fails closed on missing enrichment and
  refuses reconstructed records. Use when comparing smart-money flow signals
  in Strategy Manager.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: min_funds_net
    type: integer
    default: 3
    description: Minimum inclusive funds_net (funds buying minus selling) to enter.
    required: true
    minimum: 1
    maximum: 1000
  - name: exit_funds_net
    type: integer
    default: -1
    description: >-
      Held positions exit when funds_net is at or below this value (default -1
      means net selling). Must be below min_funds_net.
    required: true
    minimum: -1000
    maximum: 999
  - name: max_scan_age_days
    type: integer
    default: 45
    description: Calendar days a monthly scan record stays usable.
    required: true
    minimum: 1
    maximum: 400
  - name: maximum_loss_pct
    type: number
    default: 10.0
    description: Inclusive loss threshold measured from average cost.
    required: true
    minimum: 0.0
    maximum: 100.0
---

# Institutional Flow Backtest

Use `scripts/strategy.py` through Strategy Manager's in-process
`StrategyProtocolV1` runtime. `selected_securities` is re-sorted and
deduplicated by the Strategy, so iteration order never depends on the caller.
The engine owns BUY allocation and whole-share sizing. Entries respect the
host regime filter (`entry_signals_permitted`).

Parameters fail closed (no signals at all) when any is missing or unusable,
or when `exit_funds_net >= min_funds_net`, which would let one reading both
enter and exit.

Evidence rules for the flow reading (all fail closed, i.e. no flow signal):

- The security's `scan_result` must exist, match the security, and not be dated
  after `view.as_of_session`.
- `provenance_quality` must be `observed_bau`. `best_effort_reconstructed`
  records are refused: reconstruction nulls all enrichment, so it is not
  evidence.
- `enrichment.funds_net` must be non-null. Null is "unknown", never zero.
- Stale-between-filings: 13F data changes quarterly but scans are monthly. A
  record older than `max_scan_age_days` at the as-of session is stale. Stale
  records produce no entry and no flow exit, because missing fresh evidence is
  not a sell signal.

Enter when `funds_net >= min_funds_net` and a close exists on
`view.as_of_session`. `Signal.priority` is `funds_net`, so the position cap
fills the strongest flow first.

Exit a held position when either fires (one SELL carrying both reasons):

- Net selling: fresh `funds_net <= exit_funds_net` (default `-1`, i.e.
  `funds_net < 0`).
- Risk stop: the close on `view.as_of_session` is at or below
  `average_cost * (1 - maximum_loss_pct / 100)`. It reads prices only, so it
  applies even when the scan record is stale, reconstructed or null. Without a
  current close the stop does not fire. `stop_level` reports this same level.

Backtestability: the strategy is currently inert. Reconstructed history has
null enrichment, and `observed_bau` records are also built with an empty
`EnrichmentV1()` today (`app/services/backtest/observed_bau_record_builder.py`),
so `funds_net` is always null and no flow signal can fire until #7 commits real
point-in-time enrichment. Until observed months accumulate (roughly 12+),
Strategy Manager should show "insufficient observed data" instead of a return
figure.
