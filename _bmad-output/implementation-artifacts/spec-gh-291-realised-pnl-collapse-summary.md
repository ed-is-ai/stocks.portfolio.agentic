---
title: 'Add collapsed ticker detail and win/loss summary to Realised P&L'
type: 'feature'
created: '2026-08-24'
status: 'in-progress'
baseline_revision: '79228e5dc45ac70afbaef2a0b299468bc9c4cbbc'
final_revision: 'ac402b7781ec4eafb79ecf2ec73c7b47ff4318af'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/specs/spec-realised-pnl-tab-summary-round-trip-table-and-states/SPEC.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** The Realised P&L tab presents every round-trip as one long flat table and does not tell the portfolio owner how many realised trades won versus lost.

**Approach:** Keep each ticker subtotal visible but place its individual round-trip rows in a native disclosure that is collapsed by default. Extend the request-scoped summary contract with service-computed win/loss counts, counting exact break-even as a win and excluding FX-unavailable placeholder rows.

## Boundaries & Constraints

**Always:** Preserve the service-provided ticker/row order, existing ticker subtotals, and `summary.total_realised_pnl_gbp` as the sole Account-total source. Compute win/loss counts in `RealisedPnlService`, not Jinja. A win is `realised_pnl_gbp >= 0` only when `fx_unavailable` is false; a loss is a resolved P&L below zero; FX-unavailable rows count as neither. All per-ticker disclosures must start collapsed and retain every existing identifying/FX-unavailable row detail when opened.

**Block If:** Valid table/disclosure markup cannot retain the existing keyboard-accessible native disclosure behavior without changing the Realised P&L table contract.

**Never:** Do not persist new data, change FIFO/FX calculation, alter the existing account/ticker ordering, treat an FX placeholder `0.0` as break-even, or add filtering/export/tax functionality.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Mixed resolved P&L | Positive, negative, and exact-zero round-trips | Summary counts positive and zero as wins; negatives as losses | No error expected |
| FX unavailable | A round-trip has `fx_unavailable=true` and placeholder zero P&L | It is visible in collapsed ticker detail when opened but counts as neither win nor loss and remains excluded from subtotals/totals | No false break-even classification |
| Multiple tickers | Ordered grouped round-trips | Each visible subtotal is followed by its own closed disclosure; opening it exposes unchanged detail rows | No re-sorting or regrouping |
| No round-trips | Empty summary | Existing empty-state/table-total behavior remains unchanged; win/loss counts are zero | No error expected |

</intent-contract>

## Code Map

- `app/schemas/realised_pnl.py` -- request-scoped `RealisedPnlSummary` contract.
- `app/services/realised_pnl_service.py` -- authoritative FX-aware P&L aggregation and group ordering.
- `app/api/templates/_realised_pnl.html` -- summary strip, ticker subtotal rows, and detail display.
- `tests/test_realised_pnl_service.py` -- FIFO/FX aggregation regression coverage.
- `tests/test_realised_pnl_route.py` -- rendered Realised P&L fragment coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/schemas/realised_pnl.py` -- add backward-compatible winner and loser count fields to the request-scoped summary.
- [x] `app/services/realised_pnl_service.py` -- compute FX-aware winner/loser counts alongside the existing account total.
- [x] `app/api/templates/_realised_pnl.html` -- show win/loss balance in the stat strip and render each ticker’s detail rows in a closed, valid native disclosure beneath its subtotal.
- [x] `tests/test_realised_pnl_service.py` -- cover positive, negative, exact-zero, and FX-unavailable count behavior.
- [x] `tests/test_realised_pnl_route.py` -- verify collapsed disclosures, rendered balance, unchanged subtotal/total figures, and retained FX-unavailable detail text.

**Acceptance Criteria:**
- Given resolved positive, negative, and zero-P&L round-trips, when a summary is computed, then positive and zero rows count as wins and negative rows count as losses.
- Given an FX-unavailable round-trip, when a summary is computed, then it counts as neither win nor loss and does not change existing subtotals or account total.
- Given any ticker with round-trips, when the Realised P&L fragment renders, then its subtotal remains visible and its detail disclosure has no `open` attribute by default.
- Given a ticker detail disclosure is opened, when its content is inspected, then all existing columns and FX-unavailable flags for that group remain visible.
- Given a multi-ticker summary, when it renders, then ticker-group and row order equal the service-returned order and the Account total still renders from `summary.total_realised_pnl_gbp`.
- Given no round-trips, when the fragment renders, then the existing empty state and zero-valued win/loss balance render without an error.

### Review Findings

- [ ] [Review][Patch] Include the ticker in each disclosure summary for a distinguishable accessible control [app/api/templates/_realised_pnl.html:101]
- [ ] [Review][Patch] Make each visible ticker subtotal a row header [app/api/templates/_realised_pnl.html:80]
- [ ] [Review][Patch] Correct the summary-strip comment now that it includes non-GBP counts [app/api/templates/_realised_pnl.html:24]
- [ ] [Review][Patch] Make the closed-disclosure assertion reject every boolean `open` attribute form [tests/test_realised_pnl_route.py:132]
- [ ] [Review][Patch] Assert the FX-only subtotal explicitly reports unavailable FX [tests/test_realised_pnl_route.py:136]
- [ ] [Review][Patch] Assert each nested table retains its ticker-specific accessible label [tests/test_realised_pnl_route.py:135]
- [ ] [Review][Patch] Assert visible positive and negative ticker subtotal amounts [tests/test_realised_pnl_route.py:137]
- [x] [Review][Defer] Normalise rounded negative-zero P&L before rendering [app/services/realised_pnl_service.py:152] — deferred, pre-existing

## Spec Change Log

## Review Triage Log

### 2026-08-24 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 10 (high 0, medium 2, low 8)
- defer: 0
- reject: 0
- addressed_findings:
  - `[medium]` `[patch]` Nested table headers no longer compete with the outer sticky table header, and each detail table now has an accessible ticker-specific label.
  - `[low]` `[patch]` Win/loss labels use correct singular grammar and zero counts are not styled as positive or negative results.
  - `[low]` `[patch]` Disclosure summaries use neutral wording and a visual boundary that associates them with their preceding subtotal.
  - `[medium]` `[patch]` An FX-only ticker subtotal now explicitly reports unavailable FX instead of an apparent break-even result.
  - `[low]` `[patch]` Route coverage now asserts row content, intra-ticker order, plural wording, and absence of `open` on every disclosure.

## Design Notes

Use the existing native `<details>/<summary>` pattern rather than a Bootstrap collapse inside table rows. Keep outer-table structure valid by placing each disclosure inside a full-width detail cell; the nested detail table repeats the existing columns/values and is omitted from the initial rendered visual state by the closed disclosure.

## Verification

**Commands:**
- `uv run pytest -q tests/test_realised_pnl_service.py tests/test_realised_pnl_route.py` -- expected: all focused P&L tests pass.
- `uv run ruff check app tests` -- expected: no lint violations.
- `uv run pyrefly check app/schemas/realised_pnl.py app/services/realised_pnl_service.py` -- expected: no errors.
- `git diff --check` -- expected: no whitespace errors.
