---
kind: backtest-strategy
name: rtly-backtest-institutional-flow
display_name: Institutional Flow Backtest
description: >
  Backtests a deterministic long-only institutional-flow rule that buys when
  WhaleWisdom 13F net fund buying (funds_net) is strong and sells when it
  fades. Fails closed on missing enrichment and refuses reconstructed
  records. Use when comparing smart-money flow signals in Strategy Manager.
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
    default: 0
    description: Held positions exit when funds_net is at or below this value.
    required: true
    minimum: -1000
    maximum: 1000
  - name: max_scan_age_days
    type: integer
    default: 45
    description: Calendar days a monthly scan record stays usable.
    required: true
    minimum: 1
    maximum: 400
---

# Institutional Flow Backtest

Use `scripts/strategy.py` through Strategy Manager's in-process
`StrategyProtocolV1` runtime. Iterate the host-bound `selected_securities` in
order; the engine owns BUY allocation and whole-share sizing.

Evidence rules (all fail closed, i.e. no signal):

- The security's `scan_result` must exist, match the security, and not be dated
  after `view.as_of_session`.
- `provenance_quality` must be `observed_bau`. `best_effort_reconstructed`
  records are refused: reconstruction nulls all enrichment, so it is not
  evidence.
- `enrichment.funds_net` must be non-null. Null is "unknown", never zero.
- Stale-between-filings: 13F data changes quarterly but scans are monthly. A
  record older than `max_scan_age_days` at the as-of session is stale. Stale
  records produce no entry, and a held position is held (no exit), because
  missing fresh evidence is not a sell signal.

Enter when `funds_net >= min_funds_net`; exit a held position when fresh
`funds_net <= exit_funds_net`. `Signal.priority` is `funds_net`, so the
position cap fills the strongest flow first.

Backtestability: reconstructed history has null enrichment, so until observed
months accumulate (roughly 12+) Strategy Manager should show "insufficient
observed data" instead of a return figure.
