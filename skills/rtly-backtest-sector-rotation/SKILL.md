---
kind: backtest-strategy
name: rtly-backtest-sector-rotation
display_name: Sector Rotation Backtest
description: >
  Backtests a deterministic, long-only sector rotation: rank sectors by the
  equal-weight momentum of their member securities, hold members of the top
  sectors, and rotate on a monthly or quarterly rebalance. Use to test
  whether leading sectors persist across the Run's selected securities.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
parameters:
  - name: top_n_sectors
    type: integer
    default: 3
    description: Number of leading sectors whose members may be held.
    required: false
    minimum: 1
    maximum: 11
  - name: momentum_lookback_sessions
    type: integer
    default: 126
    description: Sessions in the close-to-close momentum return.
    required: false
    minimum: 5
    maximum: 1000
  - name: rebalance_cadence
    type: enum
    default: monthly
    enum_values: [monthly, quarterly]
    description: Rebalance on the first session of a new month or quarter.
    required: false
  - name: min_sector_members
    type: integer
    default: 3
    description: Scored members a sector needs before it can be ranked.
    required: false
    minimum: 1
    maximum: 100
  - name: maximum_loss_pct
    type: number
    default: 15
    description: Percent loss from average cost that forces an exit.
    required: false
    minimum: 0.1
    maximum: 99
  - name: sector_reference_version
    type: enum
    default: sector_reference.v1
    enum_values: [sector_reference.v1]
    description: Pinned sector reference the host resolves for the universe.
    required: true
---

# Sector Rotation Backtest

Use `scripts/strategy.py` through the Backtest Engine. The host binds the
Run's canonical selected-security tuple to `selected_securities` and the
pinned `{security_id: sector}` mapping to `sector_assignments`, with its
`sector_reference_digest`; both are sealed in the run manifest. A security
absent from `sector_assignments` is excluded, never grouped as "unknown", and
missing or malformed assignments produce no signals.

Score each security by `close[-1] / close[-1 - momentum_lookback_sessions] - 1`
from fresh price history. A sector's score is the equal-weight mean of its
scored members and it is rankable only with `min_sector_members` scored
members. Rank sectors by score descending, then sector name; the top
`top_n_sectors` lead.

Rebalance sessions are the first session of a new calendar month (or quarter),
detected from the last two history sessions. Only then: BUY members of leading
sectors, ranked within the batch by their own return via `Signal.priority`; and
SELL a held security whose sector left the leading set or became unrankable.
Between rebalances no rotation signal fires. The `maximum_loss_pct` stop from
average cost applies every session. Honour the regime filter via
`entry_signals_permitted`. The engine owns BUY allocation and sizing.

## Reference data and bias

Sectors come from `config/sector_reference.v1.json`, a single yfinance
Yahoo Finance sector-name snapshot (GICS-like, not GICS) applied to the whole history.

- Survivorship bias: the snapshot covers names that exist today; delisted
  securities have no mapping and are excluded, so past leaders that later
  disappeared never appear.
- Reclassification bias: a company's sector is its current sector, even in
  periods when it was classified differently, so sector membership in the past
  is approximated, not point-in-time.

```bash
uv run pytest skills/rtly-backtest-sector-rotation/scripts/tests -q
```
