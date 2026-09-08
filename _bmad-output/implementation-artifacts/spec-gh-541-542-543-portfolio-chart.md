---
title: 'Portfolio value chart: live refresh, marker toggle, reconstructed cash'
type: 'bugfix'
created: '2026-09-08'
status: 'done'
baseline_revision: 'e43c27a8a5ff851fdd96165b39a7a441ed55caae'
final_revision: 'b847ae73'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: [multiple-goals, oversized]
---

<intent-contract>

## Intent

**Problem:** Three defects on the `/portfolio` value-history chart: (#541) after **Refresh prices** the chart keeps its pre-refresh state until a browser reload; (#542) trade-event markers always render, cluttering busy portfolios; (#543) reconstructed history takes cash from the last *stated* statement balance, so a BUY/SELL between statements moves market value but not cash and the Portfolio Value line steps up/down on trades — and rows before the first statement carry NULL cash, forcing the whole chart onto the Market-Value-only fallback.

**Approach:** (#541) Swap the refresh response through htmx (`htmx.swap`) instead of a raw `innerHTML` assignment so the chart's inline init script and `hx-*` attributes are processed, carry the user's saved chart range on the request, and re-render the chart card once the background backfill completes. (#542) Render the Buy/Sell datasets hidden by default and add a client-side toggle whose state lives in `localStorage`, following the existing `activeChartRange` pattern. (#543) Replace last-stated-balance carry-forward with anchor-plus-delta reconstruction: from the nearest dated `cash_balance_history` anchor, roll signed `cash_flows` and signed trade proceeds/costs forward or backward to each day.

## Boundaries & Constraints

**Always:**
- Reconstruct cash per currency in native units, then fold to GBP through the same exact-date `gbp_rate` evidence `_cash_as_of` uses today; return `None` (never a partial or fabricated total) when there is no anchor at all or a currency has no dated rate.
- Trade replay prices are already GBP major units (existing convention); a BUY is `-shares*price`, a SELL is `+shares*price`.
- `cash_flows.amount` is magnitude-only (`CHECK(amount > 0)`); direction must come from a `flow_type` sign map. Directionally ambiguous types contribute zero rather than a guess.
- Trade rows are never written to `cash_flows` (import routes them to `planned_trades` only), so summing both cannot double-count.
- Cash may only be rewritten on rows the backfill itself wrote (timestamp exactly `{day}T00:00:00+00:00`); a live snapshot's stated cash is authoritative and must not be overwritten.
- Marker toggle is pure client-side over data the chart already holds — no server round-trip, no new route.
- `localStorage` access is wrapped in try/catch (private mode) with a working default, matching `activeChartRange`.

**Block If:**
- The `flow_type` sign map cannot be settled for `CONTRIBUTION`, `DIVIDEND`, `INTEREST`, `TAX_RELIEF`, `WITHDRAWAL` from existing code/tests.

**Never:**
- Do not add a fees/commission column to `trades`, change the SIPP import pipeline, or alter `cash_balance_history` semantics (`balances_as_of` stays as-is for its existing callers).
- Do not fetch live prices or new market data anywhere in this work.
- Do not add a JS build step, a new chart library, or a new Python dependency.
- Do not change the Market-Value fallback logic or banner copy; it must simply stop triggering once cash is reconstructed.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Sell between statements | Anchor £1000 on d0; SELL 10 @ £5 on d1; backfilled row for d1 | Cash at d1 = £1050; `total_value` unchanged vs. d0 apart from price drift | No error expected |
| Buy between statements | Anchor £1000 on d0; BUY 10 @ £5 on d1 | Cash at d1 = £950; total continuous | No error expected |
| Day before first statement | Earliest anchor £1000 on d5; BUY 10 @ £5 on d3 | Roll backward: cash at d2 = £1050, at d4 = £950; rows carry real cash, not NULL | No error expected |
| Dated cash flow | Anchor £1000 on d0; `DIVIDEND` £20 on d1; `WITHDRAWAL` £50 on d2 | Cash d1 = £1020, d2 = £970 | No error expected |
| Ambiguous flow type | `OTHER` / `TRANSFER` / `OPENING` row in the gap | Contributes 0 to the delta; reconstruction still returns a figure | Documented ceiling, no error |
| No anchor at all | Portfolio with empty `cash_balance_history` | `_cash_as_of` returns `None`; NULL cash rows; existing Market-Value fallback and banner appear | No error expected |
| Missing FX rate | Non-GBP anchor with no dated `gbp_rate` for the day | Returns `None`, not a partial total | No error expected |
| Refresh prices | Click **Refresh prices** with range `3M` selected | Chart card re-renders with the new snapshot point, still on `3M`, markers honouring the saved toggle | Failed request leaves existing chart and resets the button |
| Marker toggle default | First ever visit, no `showTradeMarkers` key | Buy/Sell datasets hidden; toggle reads "Show trades" | localStorage unavailable → default hidden |
| Marker toggle persisted | Toggle on, then switch range (htmx fragment swap) | Markers still visible after the swap | localStorage unavailable → reverts to hidden |

</intent-contract>

## Code Map

- `app/api/templates/index.html` — page-scope JS: `refreshPortfolioPrices()` (`innerHTML` swap ~:994), SIPP import swap (~:1210), `activeChartRange`/`setChartRange` (~:878), `startBackfillWatch` (~:1011), `htmx:afterSwap` backfill handler (~:1033). htmx 2.0.4 loaded at :13.
- `app/api/templates/_portfolio_chart.html` — shared chart card for both render paths; range button group (:14-32), banner (:43-48), canvas + inline Chart.js IIFE (:55+), Buy dataset (:156-167), Sell dataset (:168-180), legend filter (:195), tooltips (:201-208).
- `app/api/templates/_portfolio.html` — refresh button (:316), chart include (:415).
- `app/api/routes/portfolio.py` — `refresh_portfolio_prices` (:210), `with_current_chart_data` call (:285), backfill background task (:284).
- `app/services/portfolio_service.py` — `with_current_chart_data` (:1177), `_load_portfolio_history` (:883), `_project_portfolio_chart_rows` (:971, `market_value_extends_further` at :1045), `chart_fragment_context` (:1423).
- `app/services/snapshot_backfill.py` — `_backfill_one` (:187, marker signature ~:209, day loop :234), `_cash_as_of` (:341).
- `app/services/snapshot_repair.py` — shared replay helpers (`holdings_as_of`, `position_cost_basis_as_of`); replay tuple is `(ticker, action, shares, price, date, stop_loss, entry_price)`.
- `app/repositories/cash_balance_history_repo.py` — `balances_as_of` (:62), `earliest_as_of` (:84); table `(portfolio_id, currency, as_of, amount)`.
- `app/repositories/cash_flows_repo.py` — `history` (:29); table has `date`, `flow_type`, `amount > 0`, `currency`, `portfolio_id`.
- `app/repositories/portfolio_snapshots_repo.py` — `has_missing_cash` (:126), `fill_missing_cash` (:138), `append_daily_value_if_absent` (:149).
- `tests/test_snapshot_backfill.py` — `test_cash_balance_is_carried_forward_from_the_last_statement` (:587) encodes the buggy semantics and must be rewritten.
- `tests/test_portfolio_service.py`, `tests/test_portfolio_chart_route.py`, `tests/test_portfolio_template.py` — chart projection, fragment route, template-text assertions.

## Tasks & Acceptance

**Execution:**

*#543 — reconstructed cash*
- [x] `app/repositories/cash_balance_history_repo.py` -- add `series(portfolio_id) -> list[tuple[str, str, Decimal]]` returning every `(as_of, currency, amount)` oldest-first -- the reconstruction needs anchor *dates*, which `balances_as_of` collapses away.
- [x] `app/repositories/cash_flows_repo.py` -- add `dated_flows(portfolio_id) -> list[tuple[str, str, float, str]]` returning every `(date, flow_type, amount, currency)` oldest-first -- loaded once per portfolio, not per day.
- [x] `app/services/snapshot_repair.py` -- add `net_trade_cash(replay_rows, start_exclusive, end_inclusive) -> float`: `Σ(SELL shares*price) − Σ(BUY shares*price)` over trades dated in `(start, end]` -- reuses the one replay tuple both services already share.
- [x] `app/services/cash_reconstruction.py` (new) -- `CashReconstruction` built from anchor series, dated flows and replay rows, exposing `balances_at(as_of) -> dict[str, Decimal] | None`: per currency pick the nearest anchor by absolute date distance, then add the signed delta over the interval (forward for `as_of >= anchor`, subtract for `as_of < anchor`). Trades apply to GBP only. Flow sign map: `CONTRIBUTION`/`DIVIDEND`/`INTEREST`/`TAX_RELIEF` `+1`, `WITHDRAWAL` `-1`, `TRANSFER`/`OTHER`/`OPENING` `0`.
- [x] `app/services/snapshot_backfill.py` -- build one `CashReconstruction` per portfolio in `_backfill_one` and have `_cash_as_of` fold *its* balances to GBP (same `gbp_rate` loop, same `None` rules). Bump the idempotency signature to `v2:{start}..{end}` so existing installs re-run once, and treat "needs cash repair" as true whenever the stored signature lacks the `v2:` prefix.
- [x] `app/repositories/portfolio_snapshots_repo.py` -- add `update_backfilled_cash(portfolio_id, day, cash_balance) -> bool` updating only the row whose `timestamp = '{day}T00:00:00+00:00'` -- so a live snapshot's stated cash is never clobbered.
- [x] `app/services/snapshot_backfill.py` -- in the already-present branch of the day loop, use `update_backfilled_cash` (falling back to `fill_missing_cash` for non-backfilled rows with NULL cash) so rows written under the old semantics are corrected.
- [x] `tests/test_snapshot_backfill.py` -- rewrite `test_cash_balance_is_carried_forward_from_the_last_statement` for anchor+delta, and add regression tests for every #543 row of the I/O matrix (sell/buy in a gap, roll-backward before the first statement, dated flows, ambiguous flow type, no anchor, missing FX rate).
- [x] `tests/test_portfolio_service.py` -- add a test that a reconstructed sell day keeps `total_values` continuous and leaves `market_value_extends_further` False.

*#541 — chart updates on refresh*
- [x] `app/api/templates/index.html` -- add a `swapTabContent(html)` helper using `htmx.swap('#tab-content', html, {swap: 'innerHTML'})` with a plain-`innerHTML` fallback when htmx is absent; use it from both `refreshPortfolioPrices()` and the SIPP import path -- both call sites have the same root defect.
- [x] `app/api/templates/index.html` -- send the saved range on the refresh request (`range=activeChartRange()`), and in the existing `htmx:afterSwap` backfill handler fire one `htmx.ajax('GET', '/partials/portfolio/chart', {target: '#portfolio-chart-card', swap: 'outerHTML'})` on the transition out of a running state, so backfilled points appear without a reload.
- [x] `app/api/routes/portfolio.py` -- accept `range` on `refresh_portfolio_prices` and thread it through `with_current_chart_data` / `portfolio_partial_context`, defaulting to `DEFAULT_CHART_RANGE`.
- [x] `app/services/portfolio_service.py` -- give `with_current_chart_data` a `range_key` parameter defaulting to `DEFAULT_CHART_RANGE`, passed to `_load_portfolio_history`.
- [x] `tests/test_portfolios_routes.py` -- assert the refresh response honours a supplied `range` and that its rendered chart data includes the just-written snapshot.

*#542 — marker toggle*
- [x] `app/api/templates/index.html` -- add `showTradeMarkers()` / `setTradeMarkers(on)` beside `activeChartRange`/`setChartRange`, defaulting to **hidden**, try/catch-wrapped.
- [x] `app/api/templates/_portfolio_chart.html` -- add a toggle button in the always-rendered card shell next to the range group (aria-pressed, label flipping between "Show trades" / "Hide trades"); set `hidden: !showTradeMarkers()` on the Buy and Sell datasets at construction; on click flip both datasets' `hidden`, call `window.__portfolioChart.update()`, persist via `setTradeMarkers`, and update the button state — no server round-trip.
- [x] `tests/test_portfolio_template.py` -- assert the toggle control renders in the card shell and that the Buy/Sell datasets take their `hidden` state from `showTradeMarkers()`.

**Acceptance Criteria:**
- Given a portfolio with reconstructed history containing a SELL, when the value chart is rendered, then the Portfolio Value line does not step down by the sale value — the value moves from the securities component into cash and the total stays continuous.
- Given an anchor balance and dated `cash_flows` exist, when snapshots are backfilled, then Portfolio Value (incl. cash) is plotted across the full range and the Market-Value-only fallback and its banner appear only when no anchor exists or a currency lacks a dated rate.
- Given an install whose snapshots were backfilled under the old carry-forward semantics, when the backfill next runs, then its rows' `cash_balance` values are recomputed and live snapshot rows are left untouched.
- Given the portfolio tab is open on a non-default range, when **Refresh prices** completes, then the chart card shows the new snapshot point on that same range without a browser reload.
- Given a background snapshot backfill was scheduled by a refresh, when it finishes, then the chart card re-renders once to pick up the backfilled points.
- Given a user has never toggled markers, when the chart loads on either the full page or the range fragment, then no trade markers are drawn; and given the user turns them on, when they switch range or revisit the tab, then markers remain visible.

## Spec Change Log

## Review Triage Log

### 2026-09-08 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 7: (high 2, medium 2, low 3)
- defer: 2: (high 0, medium 2, low 0)
- reject: 14
- addressed_findings:
  - `[high]` `[patch]` `htmx.swap` was passed `{swap: 'innerHTML'}`, but htmx 2.0.4's swap spec key is `swapStyle`; it worked only by falling through to `htmx.config.defaultSwapStyle`. Verified against the htmx 2.0.4 source and corrected to `{swapStyle: 'innerHTML'}`.
  - `[high]` `[patch]` `balances_at` projected every anchored currency across the whole series, so one recently-stated USD balance asserted a phantom holding on old rows *and*, because `_cash_as_of` returns None when any currency lacks a dated rate, blanked cash on every historical row — putting the chart straight back on the Market-Value fallback this change exists to retire. Currencies reconstructing to zero are now omitted, and `_cash_as_of` distinguishes `None` (no anchor) from `{}` (genuinely no cash).
  - `[medium]` `[patch]` `needs_cash_repair` used `has_missing_cash`, so a day whose cash is unresolvable (a currency with no dated FX rate) answered "still missing" on every trigger and reopened the whole day loop, evidence prefetch included, without converging. The idempotency marker now stamps the cash *inputs* (anchor count and latest anchor date) alongside the date range, so a statement import reopens the loop and nothing else does. This also removed the unreachable `stored_signature is None` branch.
  - `[medium]` `[patch]` No direct unit tests for `CashReconstruction` — all coverage ran through SQLite and the whole service. Added `tests/test_cash_reconstruction.py` (10 tests) pinning nearest-anchor selection, the equidistant roll-forward tie-break, the roll-backward branch, half-open interval at the anchor day, flow signs incl. ambiguous types, multi-currency isolation, and the zero-balance omission.
  - `[low]` `[patch]` The module docstring claimed nearest-anchor selection "bounds the drift to one inter-statement gap" — true only *between* anchors, not before the first or after the last. Scoped the claim honestly.
  - `[low]` `[patch]` `series()`'s docstring stated the inverse of its own rationale ("collapses the anchor dates away, which is exactly what the reconstruction needs"). Reworded.
  - `[low]` `[patch]` `test_portfolio_template.py` asserted a verbatim line of JavaScript, which would fail on any reformatting while proving nothing. Loosened to the two identifiers that carry the meaning.

## Design Notes

**Why nearest anchor rather than always rolling forward.** Rows before the first statement are NULL today, which is what forces the whole chart onto the Market-Value fallback. Rolling *backward* from the earliest anchor covers them with a real figure, and choosing the nearest anchor in either direction bounds any drift from unattributable flows to a single inter-statement gap instead of letting it accumulate across the whole series.

**Sign conventions.** `cash_flows.amount` is magnitude-only, and the provider's Debit/Credit distinction is discarded at import — only `flow_type` survives (`classify_flow_type`, `trader_agent.py:1324`). `TRANSFER`/`OTHER` are genuinely directionless and `OPENING` is a stated balance rather than a movement, so all three contribute zero; mark this in code with a `ponytail:` comment naming the ceiling (unattributed flows distort within one gap) and the upgrade path (persist the signed amount at import). Trade rows are absent from `cash_flows`, so adding trade deltas cannot double-count.

**Why `htmx.swap` for #541.** `element.innerHTML = html` never executes `<script>` tags and never processes `hx-*` attributes, which is why the chart's inline Chart.js IIFE (and its own range buttons) stay dead until a reload; the htmx-driven range fragment works precisely because htmx does both. htmx 2.0.4 is already loaded, so `htmx.swap` is a one-line change at each call site, not new machinery.

**Marker toggle placement.** The card shell is always rendered while the canvas is gated on `chart_usable_total_points >= 3`; putting the toggle inside the gate would delete it on an `outerHTML` swap into an empty range. The helpers live in `index.html` page scope so they survive the chart card being swapped out.

## Verification

**Commands:**
- `uv run pytest tests/test_snapshot_backfill.py tests/test_portfolio_service.py tests/test_portfolio_chart_route.py tests/test_portfolio_template.py` -- expected: all pass, including the new #543 regression cases
- `uv run pytest` -- expected: no new failures
- `uv run ruff format . && uv run ruff check .` -- expected: clean
- `pyrefly check` -- expected: no new errors

**Manual checks (if no CLI):**
- Open `/portfolio`, pick a non-default range, click **Refresh prices**: the chart redraws on that range with the new point, no reload, and markers respect the saved toggle.
- Toggle trade markers off/on, switch range, reload the page: the chosen state persists.
- On a portfolio with a sale in the reconstructed window, confirm the Portfolio Value line has no step at the sale date and the "Market Value shown instead" banner is absent.
</content>

## Auto Run Result

Status: done

**Implemented change.** Three defects on the `/portfolio` value-history chart, in one branch:

- **#543** — reconstructed history took each day's cash from the last *stated* statement balance, so a BUY/SELL between statements moved market value but not cash and the Portfolio Value line stepped on every trade; days before the first statement carried NULL cash, forcing the whole chart onto the Market-Value-only fallback. Replaced with anchor-plus-delta reconstruction: from the nearest dated `cash_balance_history` anchor (either direction), roll signed `cash_flows` and signed trade proceeds/costs to each day, per currency, folded to GBP through the same dated FX evidence as before.
- **#541** — the refresh response was swapped in with `element.innerHTML = html`, which never executes the fragment's `<script>` tags nor processes its `hx-*` attributes, leaving the chart card inert until a browser reload. Now swapped through `htmx.swap`, with the user's saved range carried on the request and one chart-card re-render when the background backfill finishes.
- **#542** — trade-event markers always rendered. They are now hidden by default with a client-side toggle persisted in `localStorage`.

**Files changed**

- `app/services/cash_reconstruction.py` (new) — `CashReconstruction.balances_at()`: nearest-anchor-plus-signed-delta per currency, with the `FLOW_SIGNS` direction map.
- `app/services/snapshot_backfill.py` — builds one reconstruction per portfolio; `_cash_as_of` folds its balances; idempotency marker stamped with the cash inputs so a statement import (and only that) reopens the loop; already-present rows recomputed.
- `app/services/snapshot_repair.py` — `net_trade_cash()`, the signed cash a trade moves over a half-open interval.
- `app/repositories/cash_balance_history_repo.py` — `series()`, anchors with their dates intact.
- `app/repositories/cash_flows_repo.py` — `dated_flows()`, the whole uncapped flow ledger.
- `app/repositories/portfolio_snapshots_repo.py` — `update_backfilled_cash()`, scoped to the backfill's own midnight-UTC timestamp so a live snapshot is never clobbered.
- `app/services/portfolio_service.py`, `app/api/routes/portfolio.py` — `range` threaded through the refresh path.
- `app/api/templates/index.html` — `swapTabContent()` via `htmx.swap` (used by refresh *and* SIPP import), marker-toggle helpers, chart re-render on backfill completion.
- `app/api/templates/_portfolio_chart.html` — toggle button in the always-rendered card shell; Buy/Sell datasets default hidden.
- Tests — `tests/test_cash_reconstruction.py` (new, 10), plus additions to `test_snapshot_backfill.py`, `test_portfolio_service.py`, `test_portfolios_routes.py`, `test_portfolio_template.py`, `test_portfolio_import.py`.

**Review findings.** 7 patched (2 high, 2 medium, 3 low), 2 deferred, 14 rejected. Details in the Review Triage Log; deferred items in `deferred-work.md`.

**Verification.** `uv run pytest` — 3011 passed, 0 failures. `uv run ruff format` / `ruff check` clean on every changed file (unrelated files that were already unformatted on `main` were reverted to keep the diff minimal). `uv run pyrefly check` — 0 errors on all nine changed/new Python modules.

**Residual risks.**

- Directionless `cash_flows` types (`OTHER`, `TRANSFER`, `OPENING`) contribute zero, and `classify_flow_type` falls back to `OTHER` for anything it does not recognise — fees, charges, stamp duty. Those movements are silently absent from the reconstruction, and the error grows with distance from the nearest anchor. Marked with a `ponytail:` comment naming the upgrade path (persist the signed amount at import).
- Trade currency is assumed GBP (deferred; pre-existing convention across every replay consumer).
- The JavaScript has no behavioural coverage — `swapTabContent`, `toggleTradeMarkers` and `refreshChartCard` are asserted only as template text. The htmx `swapStyle` bug the review caught is exactly the class of defect that gap allows through.
