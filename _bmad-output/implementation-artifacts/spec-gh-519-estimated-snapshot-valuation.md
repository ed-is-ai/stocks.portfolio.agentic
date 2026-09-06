---
title: 'GH-519: value unpriceable holdings at carrying cost and mark the day estimated'
type: 'bugfix'
created: '2026-09-06'
baseline_revision: 'afa77cda30e83cb238d695d7ff976b6eba4b21d3'
final_revision: 'd8d3fda4ecb3c8333f496722614ea46f4fcc2800'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Snapshot valuation is all-or-nothing — `SnapshotRepairService._reconstruct` and
`SnapshotBackfillService._value` both return `None` the moment one holding has no dated price —
so a single instrument no provider covers (gilt `TR28`) blanked ten months of the portfolio
chart and took every other holding's good price down with it.

**Approach:** Value a holding with no provider evidence at its **carrying cost** (per-ticker
average cost from the existing trade replay) instead of dropping the day, persist a
`value_is_estimated` flag on the snapshot row, and surface that flag through the chart so an
estimated point is labelled rather than presented as observed fact.

## Boundaries & Constraints

**Always:**
- Estimation applies to **any** holding with no dated evidence, not a curated instrument list —
  no instrument-type classifier exists and the issue asks that the *next* uncovered instrument
  degrade gracefully too.
- Carrying cost, never par: it is already computed from trade replay and needs no new data.
- A day is flagged estimated iff at least one holding in it was valued at carrying cost.
- Real dated evidence always wins: a priced holding is never replaced by its carrying cost.
- Trade prices are GBP major units (same convention `cost_basis_as_of` already uses) — no FX
  conversion is applied to a carrying-cost leg.
- Existing all-or-nothing behaviour stays reachable and remains the behaviour under the repair
  CLI's `--no-historical-evidence` opt-out.
- A valuation still rounding to `0.00` remains "unavailable" (`None`), preserving re-run no-op.
- The schema change is additive and idempotent; existing rows default to not-estimated.

**Block If:**
- The `portfolio_snapshots` rebuild migration cannot be made idempotent alongside the new column.

**Never:**
- No live/current prices, no nearby-date prices, no guessed FX rate — the evidence-only contract
  of `HistoricalGbpPriceSource` is untouched.
- No manual gilt price feed or new provider (tracked separately).
- No change to `total_cost` / `cash_balance` semantics, and no back-population of the new flag
  onto rows already carrying a real valuation.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| All holdings priced | Every ticker has a dated GBP close on `as_of` | Value from evidence; `value_is_estimated = 0` | No error expected |
| One holding unpriceable | `TR28` has no close, others do | Value = priced legs + `TR28` shares x avg cost; `value_is_estimated = 1` | No error expected |
| No holding priceable | No ticker has a close | Value = total carrying cost; `value_is_estimated = 1` | No error expected |
| Estimation disabled | Same, with `estimate_unpriceable=False` | `None` — day stays an honest gap (pre-#519 behaviour) | No error expected |
| Carrying cost is zero | Unpriceable ticker whose replayed position nets to a `0.00` total | `None` (unavailable), not a stored `0.00` | No error expected |
| Repair of an existing NULL row | NULL row, holdings present, no evidence | Row updated to the estimate with the flag set; a second pass reports it `unchanged` | No error expected |
| Legacy database | `portfolio_snapshots` without the new column | Migration adds it with default `0`; rows read as not-estimated | Migration is idempotent; re-run is a no-op |
| Chart with estimated points | History mixing estimated and observed rows | Estimated points visually distinguished, tooltip says estimated, one explanatory line above the chart | Missing flag treated as not-estimated |

</intent-contract>

## Code Map

- `app/repositories/db.py` -- owns the `trades.db` schema; `_SCHEMA` declares
  `portfolio_snapshots`, `init_trades_db` applies additive migrations,
  `_migrate_portfolio_snapshots_nullable` rebuilds that table with an explicit column list.
- `app/repositories/portfolio_snapshots_repo.py` -- `history()` (chart read),
  `append_daily_value_if_absent()` (backfill write), `update_valuation()` (repair write).
- `app/services/snapshot_repair.py` -- `cost_basis_as_of`, `holdings_as_of`,
  `HistoricalGbpPriceSource`, `NoHistoricalPriceSource`, `SnapshotRepairService._reconstruct`,
  `SnapshotRepairReport`.
- `app/services/snapshot_backfill.py` -- `SnapshotBackfillService._value`,
  `SnapshotBackfillReport`, `build_backfill_service` wiring.
- `app/cli/repair_portfolio_snapshots.py` -- `--no-historical-evidence` opt-out.
- `app/services/portfolio_service.py` -- `_project_portfolio_chart_rows` builds the aligned
  chart series; `portfolio_partial_context` and `chart_fragment_context` both publish the
  `chart_*` keys (they must stay in step).
- `app/api/templates/_portfolio_chart.html` -- the Chart.js card, existing notice block.
- `tests/test_snapshot_repair.py`, `tests/test_snapshot_backfill.py` -- existing
  all-or-nothing coverage via `NoHistoricalPriceSource`.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/db.py` -- add `value_is_estimated INTEGER NOT NULL DEFAULT 0` to
      `portfolio_snapshots` in `_SCHEMA`, mirror it in the nullable-rebuild table definition,
      and add a guarded `ALTER TABLE ... ADD COLUMN` in `init_trades_db` after
      `_migrate_portfolio_snapshots_nullable` -- so new and legacy databases converge.
- [x] `app/services/snapshot_repair.py` -- add `position_cost_basis_as_of()` returning
      `{ticker: carrying cost}` and re-express `cost_basis_as_of()` as its sum (one replay
      convention, not two); add a shared `value_holdings()` returning
      `(value | None, is_estimated)`; give `SnapshotRepairService` an
      `estimate_unpriceable: bool = True` constructor flag, delegate `_reconstruct` to
      `value_holdings`, persist the flag, and add an `estimated` counter to the report.
- [x] `app/services/snapshot_backfill.py` -- delete the duplicated `_value` body in favour of
      the shared `value_holdings`, take the same `estimate_unpriceable` flag, pass the flag
      through to `append_daily_value_if_absent`, and add a `days_valued_with_estimates`
      counter to the report.
- [x] `app/repositories/portfolio_snapshots_repo.py` -- select `value_is_estimated` as
      `history()`'s fifth column, and accept/write it in `append_daily_value_if_absent()` and
      `update_valuation()` (defaulting to not-estimated so existing callers are unchanged).
- [x] `app/cli/repair_portfolio_snapshots.py` -- pass `estimate_unpriceable=False` when
      `--no-historical-evidence` is given, and say so in the option help.
- [x] `app/services/portfolio_service.py` -- project an `estimated` boolean series aligned with
      `labels` plus a `has_estimated_values` summary in `_project_portfolio_chart_rows`, and
      publish them as `chart_estimated` / `chart_has_estimated_values` from **both**
      `portfolio_partial_context` and `chart_fragment_context`.
- [x] `app/api/templates/_portfolio_chart.html` -- render one explanatory line when the window
      contains estimates, mark estimated points on the value series with a distinct point
      style/colour, and append " (estimated)" to those points' tooltips.
- [x] `tests/test_snapshot_repair.py`, `tests/test_snapshot_backfill.py` -- cover every row of
      the I/O matrix: partial evidence, no evidence, estimation disabled, zero carrying cost,
      repair idempotency, and the persisted flag.
- [x] `tests/` -- add a migration test (legacy table shape gains the column, re-run is a no-op)
      and a chart-projection test asserting the aligned `estimated` series and summary flag.

**Acceptance Criteria:**
- Given a portfolio holding a priceable stock and an unpriceable gilt on a day, when the
  snapshot backfill runs, then a row is written whose value includes both legs and whose
  `value_is_estimated` is `1` — where previously no row was written at all.
- Given snapshot rows already stored as `NULL` for days with holdings, when the repair pass
  runs with estimation enabled, then those rows are repaired to carrying-cost-inclusive values
  flagged as estimated, and a second identical pass reports every row `unchanged`.
- Given a database created before this change, when `init_trades_db` runs twice, then
  `portfolio_snapshots` has exactly one `value_is_estimated` column and no row's other data
  changed.
- Given a chart window containing at least one estimated snapshot, when the portfolio page or
  the `/partials/portfolio/chart` fragment renders, then the estimate notice appears and the
  estimated points are visually distinguished from observed ones.
- Given `--no-historical-evidence`, when the repair CLI runs, then every candidate row is still
  nulled and none is estimated.

## Spec Change Log

## Review Triage Log

### 2026-09-06 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 2, low 1)
- defer: 4: (high 0, medium 2, low 2)
- reject: 2: (high 0, medium 0, low 2)
- addressed_findings:
  - `[medium]` `[patch]` Estimated-values banner rendered even when the canvas itself is gated off (`chart_usable_total_points < 3`), referencing a diamond marker the user could never see -- gated the banner on the same threshold as the canvas.
  - `[medium]` `[patch]` Tooltip appended "(estimated)" to every dataset's label, including Cost Basis and Cash, neither of which is ever computed via the carrying-cost fallback -- restricted the suffix to the Portfolio Value / Market Value datasets.
  - `[low]` `[patch]` No test covered a day where every held ticker (not just one of several) is unpriceable -- added `test_every_held_ticker_unpriceable_values_the_day_at_total_cost` to `tests/test_snapshot_backfill.py` (the repair-side equivalent already existed).

  Deferred (see `deferred-work.md`): BAU notifications don't surface the new `estimated`/`days_valued_with_estimates` counters; `--no-historical-evidence` conflates "skip the price cache" with "skip the carrying-cost fallback"; `value_holdings` can silently misvalue a ticker with an uncorrected over-sell in its history because `holdings_as_of` and `position_cost_basis_as_of` can disagree on effective share count after such an anomaly; the broad `except sqlite3.OperationalError` around the new migration step is unchanged, matching the identical pre-existing idiom used by every other additive migration in this file.

  Rejected: a defensive length-parity check between `chart_estimated` and `chart_total_values` in the template JS (both arrays are built by the same loop over the same rows, so they are guaranteed aligned by construction); a regression test asserting `value_is_estimated` clears to 0 when a row transitions from estimated back to unavailable (already correct by construction -- `update_valuation`'s `marked_unavailable` call site omits the parameter, defaulting to `False`/0 -- and the transition requires contrived state to reach, since a once-valued row is no longer a repair candidate).

## Design Notes

Per-ticker carrying cost already exists inside `cost_basis_as_of`'s average-cost replay; it is
only aggregated too early. Splitting out `position_cost_basis_as_of` keeps one replay convention
(a sell reduces at the running average, a fully closed position resets) shared by the total cost
basis and the new fallback, so a backfilled row's `total_cost` and its estimated legs cannot
drift apart.

`_reconstruct` and `_value` are today byte-identical duplicates of the same all-or-nothing rule
in two services; the fix goes into one `value_holdings` they both call rather than being written
twice.

Shape of the shared valuation:

```python
def value_holdings(source, holdings, as_of, carrying):  # carrying: {} disables estimation
    total, estimated = 0.0, False
    for ticker, shares in holdings.items():
        price = source.gbp_price(ticker, as_of)
        if price is None:
            if ticker not in carrying:
                return None, False        # honest gap, unchanged pre-#519 behaviour
            total, estimated = total + carrying[ticker], True
        else:
            total += shares * price
    value = round(total, 2)
    return (None, False) if value == 0.0 else (value, estimated)
```

## Verification

**Commands:**
- `uv run pytest tests/test_snapshot_repair.py tests/test_snapshot_backfill.py -q` -- expected: all pass
- `uv run pytest -q` -- expected: no regressions
- `uv run ruff format . && uv run ruff check .` -- expected: clean
- `pyrefly check` -- expected: no new errors
