---
title: 'Show real progress on the backtest run activity screen'
type: 'feature'
created: '2026-08-29'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issue: 411
baseline_revision: '29abbc4b'
final_revision: '7ef66009'
---

<intent-contract>

## Intent

**Problem:** A running backtest activity only shows its current calendar month, so it gives no indication of total progress. The run period already supplies the inclusive total, and the activity restart plus configuration submit controls still use stock Bootstrap primary-button styling.

**Approach:** Derive an inclusive month position from the existing Strategy Run and authoritative job current month, then render a textual, accessible progress bar in the existing polling fragment. Adopt the Strategy Manager primary-button component for the two affected actions without changing any lifecycle behavior.

## Boundaries & Constraints

**Always:** Use `TradingCalendar.months_inclusive()` for the month range. Preserve the outer activity element, 3-second/version-gated polling, live-status region, and cancel/restart/delete behaviors. Render a progressbar with a visible “Month X of N” label and accurate ARIA min/max/current values. Retain cancellation wording.

**Block If:** Deriving the view would require a new persisted field, endpoint, client-side polling/counter, or a lifecycle-schema/service change.

**Never:** Do not alter `StrategyJobService`, worker semantics, status-version handling, run range validation, or submit names/disabled logic. Do not render the bar for queued, terminal, or a running job without an authoritative current month.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Mid-run | `2024-01`–`2024-03`, current `2024-02` | “Month 2 of 3”; ARIA value 2/max 3; fill 66.67%. | No error expected. |
| Inclusive boundaries | one month, or Dec–Feb range | First/last positions are 1/N and N/N; one month is 1/1. | No error expected. |
| Inapplicable lifecycle state | queued, terminal, or running with no month | No bar; the existing status shell remains. | No error expected. |
| Poll update | newer status version/current month | Existing outerHTML swap re-renders the newly derived position; no endpoint change. | 204 on unchanged version remains. |
| Invalid unexpected month | current month outside run range | Omit progress rather than render false progress. | Preserve activity render. |

</intent-contract>

## Code Map

- `app/api/routes/strategy_manager.py` — activity context used by both the initial GET and the status poll; `TradingCalendar` is already imported and provides the canonical inclusive month sequence.
- `app/api/templates/_backtest_activity.html` — running-month display, polling shell, and Restart backtest control.
- `app/api/templates/_strategy_configuration.html` — Run Backtest submit styling.
- `app/api/static/css/theme.css` — existing Strategy Manager tokens and `.month-progress` treatment; add a scoped, non-animated bar.
- `tests/test_strategy_manager_routes.py` — route-template tests and fakes for running/terminal activities.

## Tasks & Acceptance

**Execution:**
- [x] `tests/test_strategy_manager_routes.py` — add failing cases for inclusive position/percentage, poll-rendered progress, absent non-running progress, the preserved single live region, and both component button classes.
- [x] `app/api/routes/strategy_manager.py` — add a pure backtest progress helper and expose its view model only for a running backtest with a valid current month.
- [x] `app/api/templates/_backtest_activity.html` and `app/api/static/css/theme.css` — replace the bare running-month text with the accessible label/bar and scoped styles; retain polling/cancellation markup; use `sm-btn sm-btn-primary` on restart.
- [x] `app/api/templates/_strategy_configuration.html` — replace the stock primary submit class while preserving its existing margin and disabled behavior.
- [x] Run focused, full, lint, static-analysis, and whitespace checks; record results.

**Acceptance Criteria:**
- Given a running backtest over an N-month period, when the activity fragment renders, then it shows “Month X of N” and a proportional bar that advances after an accepted polling update.
- Given the backtest activity or configuration submit action, then its primary controls use the `sm-btn sm-btn-primary` project styling.
- Given these presentation changes, when lifecycle actions or a same-version poll are used, then their existing behavior remains unchanged.

## Design Notes

`StrategyJobV1.current_month` is published before the worker processes that calendar month, so X means the active month’s one-based position, not completed-month count. The repository validates progress inside the run period; the helper nevertheless returns no view model for an unexpected value so an integrity anomaly does not turn into a template failure or deceptive percentage. The context is recomputed for each existing fragment render, avoiding JavaScript and keeping the current single live region intact.

## Verification

**Commands:**
- `uv run pytest tests/test_strategy_manager_routes.py -q` — focused route and markup suite passes.
- `uv run pytest -q` — full regression suite passes.
- `uv run ruff format --check . && uv run ruff check .` — formatting and lint pass.
- `uv run pyrefly check` — no new static-analysis errors.
- `git diff --check` — no whitespace errors.

## Review Triage Log

### 2026-08-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5: (medium 1, low 4)
- defer: 0
- reject: 0
- addressed_findings:
  - `[medium]` `[patch]` Replacing Bootstrap's disabled button class removed the disabled affordance; added disabled/aria-disabled component styling.
  - `[low]` `[patch]` Added same-version polling, queued/terminal/no-current-month, malformed range, and cancellation-copy regression coverage.

## Auto Run Result

Status: done

**Implemented:** Running backtest activities now calculate their active calendar month as an inclusive `Month X of N`, render a proportional accessible progress bar, and update through the existing version-gated HTMX poll. Restart backtest and Run Backtest use the Strategy Manager primary button component; its disabled state remains visibly disabled.

**Files changed:**
- `app/api/routes/strategy_manager.py` — safe, pure inclusive progress view model exposed only for running backtests.
- `app/api/templates/_backtest_activity.html` — accessible progress markup and component-styled restart button.
- `app/api/templates/_strategy_configuration.html` — component-styled submit button.
- `app/api/static/css/theme.css` — explicit non-animated progress styling and disabled button affordance.
- `tests/test_strategy_manager_routes.py` — progress, lifecycle, polling, accessibility, cancellation, and styling coverage.

**Verification:** Focused route suite: `147 passed`. Targeted Ruff format/check and `git diff --check` passed. Static analysis reports two existing unrelated errors in `strategy_manager.py` (lines 235 and 1080); repository-wide Ruff reports nine unrelated lint errors and 20 pre-existing formatting differences. The complete pytest suite was attempted twice but did not return a final result after progress reached 31%; no test failure was reported before the runner stopped returning output.

**Residual risk:** Full-suite completion requires a follow-up in an environment where the suite runner can finish; focused coverage of this change passes.
