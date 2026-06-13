## Context

`portfolio_value.csv` already contains `timestamp`, `total_value`, `total_cost`, `cash_balance`, and `investments_value` columns — written each time prices are refreshed. The portfolio tab already renders a Chart.js chart using `_load_portfolio_history()`, which currently reads only `total_value` and `total_cost`. Cash is therefore available in the CSV but not surfaced in the chart.

Current chart has two lines: Market Value and Cost Basis. We want to add a third line for Cash.

## Goals / Non-Goals

**Goals:**
- Add a Cash line to the existing portfolio chart
- Cash line plots `cash_balance` from each CSV snapshot row
- Total Value line already includes cash (it's `investments_value + cash_balance`) — no change needed to how it's calculated
- Backwards-compatible: rows without `cash_balance` (older snapshots) render as 0 or null for that point

**Non-Goals:**
- Changing the snapshot write logic (already correct)
- Adding a new chart — extend the existing one
- Storing cash history separately — the CSV is the source of truth

## Decisions

**Reuse existing Chart.js chart** — the chart is already on the page with labels, datasets, and tooltip callbacks. Adding a third dataset is the minimal change.

**Read `cash_balance` in `_load_portfolio_history()`** — extend the return dict to include a `cash_values` list alongside the existing `values` and `costs`. Rows missing the column default to `None` (rendered as a gap in Chart.js).

**Line style for Cash** — dashed green line to visually distinguish it from Market Value (solid blue) and Cost Basis (dashed grey). Cash is stable so a muted style fits.

## Risks / Trade-offs

**Single data point** — currently only one snapshot row exists. The chart will show a single dot until more refreshes are recorded. This is expected behaviour; no mitigation needed.

**CSV schema drift** — `cash_balance` is already present in the file, so no migration of existing rows is required.

## Migration Plan

1. Update `_load_portfolio_history()` to read `cash_balance` column (default `None` for missing)
2. Pass `chart_cash` list through `_render_portfolio` context
3. Add Cash dataset to the Chart.js config in `_portfolio.html`

No rollback risk — the CSV is append-only and the change is purely additive.
