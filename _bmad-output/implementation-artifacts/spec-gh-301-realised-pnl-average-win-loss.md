---
title: 'Add average win and loss percentages to Realised P&L'
type: 'feature'
created: '2026-08-24'
status: 'done'
baseline_revision: '48222d71'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/spec-gh-291-realised-pnl-collapse-summary.md'
warnings:
  - 'Canonical _bmad workflow files and the named bmad-dev-auto SKILL.md are absent from this worktree; the prior bmad-dev-auto issue artifact is used as the local workflow contract.'
---

<intent-contract>

## Intent

**Problem:** The Realised P&L summary reports how many resolved round-trips won or lost but not the typical percentage size of either outcome.

**Approach:** Add nullable average percentage fields to the request-scoped summary, calculate simple arithmetic means from the same FX-resolved GBP win/loss buckets as the existing counts, and render a new summary card between Win / Loss and Unmatched Sells.

## Boundaries & Constraints

**Always:** Classify bucket membership by resolved `realised_pnl_gbp`, exactly as the existing win/loss counts do. Include exact GBP break-even in the win bucket. Average each bucket's `realised_pnl_pct` without position-size weighting. Use `None` for an empty bucket so the UI renders an em dash. Keep FX-unavailable round-trips visible elsewhere but exclude them from both averages.

**Block If:** The averages cannot reuse the existing resolved round-trip classification without changing FIFO or FX behavior.

**Never:** Do not add persistence or a migration, reclassify wins/losses by percentage, include FX placeholder values, size-weight averages, or change existing totals/counts/order.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| No resolved round-trips | Empty or unmatched-only account | Both averages are `None`; card shows em dashes | No fabricated `0.0%` |
| Wins only | Positive and/or exact-zero GBP P&L | Simple mean of their P&L percentages; loss average is `None` | Zero remains a win |
| Losses only | Negative GBP P&L | Win average is `None`; simple mean is shown for losses | Negative style retained |
| Mixed values | Positive, zero, and negative resolved rows | Independent simple means from existing GBP buckets | No cross-bucket weighting |
| FX unavailable | Placeholder row plus resolved rows | Placeholder contributes to neither average | No false break-even win |

</intent-contract>

## Code Map

- `app/schemas/realised_pnl.py` -- request-scoped summary contract.
- `app/services/realised_pnl_service.py` -- authoritative resolved win/loss classification and aggregation.
- `app/api/templates/_realised_pnl.html` -- summary strip rendering.
- `tests/test_realised_pnl_service.py` -- calculation edge cases.
- `tests/test_realised_pnl_route.py` -- rendered card, ordering, styles, and missing-state coverage.

## Tasks & Acceptance

**Execution:**
- [x] Add backward-compatible nullable `average_win_pct` and `average_loss_pct` fields.
- [x] Compute simple bucket averages from FX-resolved round-trips using existing GBP win/loss classification.
- [x] Insert the average percentage stat card between Win / Loss and Unmatched Sells, with positive/negative styles and em dashes for absent buckets.
- [x] Cover no round-trips, wins only, losses only, mixed values, FX exclusion, and rendered output.

**Acceptance Criteria:**
- Given no resolved round-trips, both summary averages are `None` and the rendered card displays an em dash for each bucket.
- Given only resolved wins, including exact break-even, `average_win_pct` is their simple percentage mean and `average_loss_pct` is `None`.
- Given only resolved losses, `average_win_pct` is `None` and `average_loss_pct` is their simple percentage mean.
- Given mixed resolved outcomes, each average uses only its existing GBP P&L bucket and is not size weighted.
- Given an FX-unavailable round-trip, its placeholder percentage contributes to neither average.
- The new card is rendered after Win / Loss and before Unmatched Sells, with available win/loss values using `.pos`/`.neg` and absent values rendered as em dashes.

### Review Findings

- [x] [Review][Patch] Scope missing-value assertions to the average card so table em dashes cannot produce a false pass [tests/test_realised_pnl_route.py]
- [x] [Review][Patch] Document nullable bucket semantics beside the new schema fields [app/schemas/realised_pnl.py]

## Spec Change Log

- 2026-08-24: Created implementation contract from GitHub issue #301 and user-confirmed intent.
- 2026-08-24: Implemented and reviewed the schema, service aggregation, summary card, and regression coverage.

## Review Triage Log

### 2026-08-24 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2 (high 0, medium 0, low 2)
- defer: 0
- reject: 0
- addressed_findings:
  - `[low]` `[patch]` Isolated average-card markup in route assertions and verified missing values have neither result style.
  - `[low]` `[patch]` Added field-adjacent documentation for the nullable resolved-bucket means.

## Verification

**Commands:**
- `uv run pytest -q tests/test_realised_pnl_service.py tests/test_realised_pnl_route.py` -- passed: 78 tests, 1 warning.
- `uv run ruff check app tests` -- passed.
- `uv run pyrefly check app/schemas/realised_pnl.py app/services/realised_pnl_service.py` -- passed: 0 errors.
- `uv run pytest -q` -- passed: 1,948 tests, 5 warnings.
- `git diff --check` -- passed.

Deferred work: none.
