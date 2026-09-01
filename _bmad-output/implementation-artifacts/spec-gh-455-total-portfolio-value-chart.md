---
title: 'Plot total portfolio value from cash and stocks'
type: 'feature'
created: '2026-08-31'
status: 'done'
baseline_revision: '9fd10d6d73c59a2be8a51851c2931ef4cc14a2df'
final_revision: '8ea29de9'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-453-context.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** The Portfolio history chart currently emphasizes invested market
value and does not expose the account's combined value, so movements in cash
are absent from its main trend. Invalid or missing snapshot components must not
be disguised as zero or allowed to produce invalid chart JSON.

**Approach:** Add one service-owned, same-snapshot `Portfolio Value` projection
equal to market value plus cash, carry it through both full and lean chart
contexts, and render it as the dominant accessible Chart.js series while
preserving the existing supporting series, markers, ranges, downsampling and
fragment lifecycle.

## Boundaries & Constraints

**Always:** Use the same portfolio-scoped, chronologically ordered,
server-filtered and once-downsampled snapshot rows for every series. Treat zero
and negative cash as valid; represent a total as unavailable when either input
is missing, nonnumeric, `NaN`, or infinite. Keep arithmetic in
`PortfolioService`, keep arrays index-aligned, retain market value as the marker
anchor, and retain the stable chart shell, range selector and teardown.

**Block If:** Stored `total_value` is discovered to include cash for any active
writer or supported migration state, or a valid component cannot be interpreted
without changing currency/valuation semantics.

**Never:** Change snapshot schema or writers, mutate stored history, substitute
zero for an unavailable component, recompute historical FX, move arithmetic to
Jinja/JavaScript, or broaden the work into localStorage, portfolio selection,
trade-marker or chart-range redesign.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Valid history | Two or more rows with finite market value and cash | Each total is the exact same-row sum; zero and negative cash participate normally | No error expected |
| Partial history | Market value or cash missing in a retained row | Total at that index is `None`/JSON `null`; supporting valid values remain visible | Show factual unavailable-series copy; never use zero |
| Invalid number | Component is nonnumeric, `NaN`, or infinite | Total is unavailable and emitted JSON remains standards-safe | Fragment renders without exception |
| Range/account change | Any supported preset or selected account | All series use the same filtered, sorted and downsampled rows, at most 250 points | Preserve empty-range selector shell and fallback behavior |
| Repeated swap | Chart fragment is replaced repeatedly | Old chart is destroyed before the new chart is created | No leaked Chart.js instance |

</intent-contract>

## Code Map

- `app/services/portfolio_service.py` -- owns range cutoff, snapshot loading,
  aligned chart projection, marker anchoring, and both serialized chart
  contexts.
- `app/repositories/portfolio_snapshots_repo.py` -- authoritative tuple shape:
  timestamp, holdings market value, cost basis and separately stored cash;
  persistence is read-only for this story.
- `app/api/routes/views.py` -- lean chart-fragment route delegating to the
  service; its no-full-dashboard-work contract must remain intact.
- `app/api/templates/_portfolio_chart.html` -- stable range/card shell,
  Chart.js datasets, tooltip, markers and global instance teardown.
- `tests/test_portfolio_service.py` -- projection, ordering, downsampling and
  context serialization coverage.
- `tests/test_portfolio_chart_route.py` -- account/range-scoped lean fragment
  and retained #421 behavior.
- `tests/test_portfolio_template.py` -- dataset hierarchy, accessibility,
  non-colour distinctions and teardown contracts.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/portfolio_service.py` -- normalize finite chart components,
  sort selected rows chronologically before the existing single downsampling
  pass, calculate aligned same-row totals without rounding stored facts, and
  expose totals plus an unavailable indicator in both full and fragment
  contexts.
- [x] `app/api/templates/_portfolio_chart.html` -- add `Portfolio Value` as the
  first/dominant solid dataset; make Market Value, Cost Basis and Cash visually
  subordinate with distinct line treatments; expose a canvas accessible name
  and factual partial-data message while preserving Buy/Sell markers and both
  teardown paths.
- [x] `tests/test_portfolio_service.py` -- cover decimal arithmetic, zero and
  negative cash, missing/malformed/nonfinite values, input immutability,
  chronological alignment, <=250-point downsampling, and parity between full
  and fragment contexts.
- [x] `tests/test_portfolio_chart_route.py` -- prove selected account/range
  totals render through the lean fragment and retain empty-range, marker and
  stable-shell behavior.
- [x] `tests/test_portfolio_template.py` -- verify labels/order, dominant and
  non-colour line distinctions, accessible/fallback text, and destroy-before-
  create/no-data teardown behavior.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` and
  `github-bmad-tracking.yaml` -- keep GH-455 implementation status visible and
  synchronized with the review state.

**Acceptance Criteria:**
- Given valid retained snapshots, when either the full Portfolio dashboard or
  lean range fragment renders, then `Portfolio Value` equals market value plus
  cash for each identical retained row and is visually dominant.
- Given unavailable components, when chart data is projected, then affected
  totals are explicit gaps with factual accessible notice and no persisted fact
  is changed or replaced by zero.
- Given supporting series and trade events, when the chart renders, then their
  labels, distinct non-colour treatments, marker meaning and tooltip behavior
  remain available.
- Given any supported range/account refresh, when rows are selected, then all
  datasets remain chronological, index-aligned, portfolio-scoped and bounded to
  250 points while the selector, local preference, empty state and htmx swap
  contract remain unchanged.

## Spec Change Log

## Review Triage Log

### 2026-08-31 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 10: (high 0, medium 1, low 9)
- defer: 1: (high 0, medium 0, low 1)
- reject: 3: (high 0, medium 0, low 3)
- addressed_findings:
  - `[low]` `[patch]` Verified the supported legacy CSV writer stores holdings market value separately from cash and added a regression proving cash is added exactly once.
  - `[low]` `[patch]` Replaced lexical timestamp sorting with UTC-instant normalization and excluded unplaceable timestamps before bounded downsampling.
  - `[low]` `[patch]` Made decimal addition use sufficient local precision for wide-magnitude cancellation without changing stored facts.
  - `[low]` `[patch]` Rejected overflow and underflow presentation values before total calculation so supporting gaps cannot produce a displayed total or phantom zero.
  - `[low]` `[patch]` Treated short retained rows as unavailable components rather than raising an index error.
  - `[low]` `[patch]` Distinguished wholly unavailable Portfolio Value history from a partially unavailable series in user-facing copy.
  - `[low]` `[patch]` Tightened route tests to compare every total with its same-index market and cash inputs and to assert the intended null total directly.
  - `[low]` `[patch]` Added mixed-offset, mixed-representation, wide-decimal, underflow, legacy CSV, and aligned-downsampling regression cases.
  - `[medium]` `[patch]` Renamed the compiled context to GitHub Epic 453 so it cannot collide with the repository's completed canonical Epic 1.
  - `[low]` `[patch]` Added an explicit all-totals-unavailable context contract to both full and lean renders.

## Design Notes

The stored snapshot `total_value` is holdings market value despite its legacy
name; current writers persist cash separately. Keep existing `values` semantics
for markers and add a separate projected series. Use finite decimal-domain
addition before converting the presentation value to a JSON-safe float, so
`0.1 + 0.2` does not become a misleading binary artefact and `None` becomes
JavaScript `null`.

## Verification

**Commands:**
- `uv run pytest -q tests/test_portfolio_service.py tests/test_portfolio_chart_route.py tests/test_portfolio_template.py` -- focused Portfolio service/route/template regressions pass.
- `uv run pytest -q` -- repository suite introduces no failures.
- `uv run ruff check app tests` -- lint passes.
- `uv run ruff format --check app tests` -- formatting passes.
- `git diff --check` -- no whitespace errors.

## Auto Run Result

### Summary

Added a service-owned `Portfolio Value` history projection from each retained
snapshot's market value and cash, rendered it as the dominant accessible chart
series, and retained the existing supporting series, ranges, account scoping,
markers, bounded downsampling, htmx fragment shell, and Chart.js teardown.

### Files changed

- `app/services/portfolio_service.py` -- UTC-normalized chronological rows,
  finite decimal projection, unavailable-state metadata, and full/lean context
  serialization.
- `app/api/templates/_portfolio_chart.html` -- dominant total series, distinct
  supporting line patterns, accessible canvas text, and partial/all-unavailable
  notices.
- `tests/test_portfolio_service.py` -- arithmetic, invalid values, ordering,
  legacy storage, alignment, downsampling and context parity coverage.
- `tests/test_portfolio_chart_route.py` -- exact same-index totals, retained
  marker behavior and scoped unavailable gaps.
- `tests/test_portfolio_template.py` -- dataset hierarchy, non-colour
  distinctions, accessibility copy and teardown contracts.
- `_bmad-output/implementation-artifacts/epic-453-context.md` -- focused context
  for GitHub Epic 453 without colliding with canonical Epic 1.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` and
  `github-bmad-tracking.yaml` -- GH-455 review visibility.
- `_bmad-output/implementation-artifacts/deferred-work.md` -- recorded the
  independent browser-level repeated-swap test gap.

### Review findings

- Patches applied: 10 (one medium, nine low), covering ordering, numeric edge
  handling, legacy evidence, unavailable copy, exact assertions and BMAD epic
  identity.
- Deferred: 1 low-severity browser-harness coverage improvement.
- Rejected: 3 low-severity findings already covered by retained regressions,
  commit staging, or outside the accepted accessibility contract.
- Follow-up review recommended: true because review changes crossed service,
  presentation, regression and planning-artifact boundaries.

### Verification

- Focused Portfolio suite: 88 passed.
- Full repository suite: 2,599 passed with 5 warnings.
- Ruff check across `app` and `tests`: passed.
- Ruff formatting for all changed Python files: passed.
- BMAD YAML parsing: passed.
- `git diff --check`: passed.

### Residual risks

- The browser-level execution of repeated Chart.js instances across real htmx
  swaps remains deferred; source ordering and existing fragment behavior are
  covered.
- Historical snapshot currency/valuation semantics are intentionally unchanged;
  the total projection combines the two stored same-row components only.
