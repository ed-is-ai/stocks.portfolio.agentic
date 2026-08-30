---
title: 'Plain-language and declutter pass on backtest result and comparison pages'
type: 'feature'
created: '2026-08-29'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issue: 412
baseline_revision: '29abbc4b'
final_revision: 'b2d0ce41'
---

<intent-contract>

## Intent

**Problem:** Result and comparison screens expose integrity digests in primary content and display internal provenance tokens, unlike the plain-language Strategy Manager landing. Neither offers an in-page return to Strategy Manager.

**Approach:** Move only the two Result digests behind a collapsed audit disclosure; put the provenance-label mapping in one shared template macro used by landing, Result, and comparison; add the established back navigation to Result and comparison.

## Boundaries & Constraints

**Always:** Preserve presented/computed values, provenance warning text, Result/Comparison integrity states, existing HTMX behavior, and raw audit values inside the disclosure. The shared macro must retain a readable fallback for unknown provenance values.

**Block If:** A shared template macro cannot be imported by all three surfaces without changing template/render infrastructure.

**Never:** Do not change presenters, routes, calculations, API/data shapes, or comparison eligibility.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Result | completed result with both digests | Run identity excludes digests; closed Audit details holds both values. | n/a |
| Provenance | reconstructed / observed / unknown quality | Shared labels are “Reconstructed (approximate)”, “Observed (from live scans)”, or readable fallback. | n/a |
| Result/comparison state | normal or integrity-error screen | Visible Back to Strategy Manager control retains HTMX navigation. | Integrity error remains primary message. |

</intent-contract>

## Code Map

- `app/api/templates/_macros.html` — shared macro location.
- `app/api/templates/_strategy_manager.html`, `_backtest_result.html`, `_comparison.html` — three provenance consumers and navigation surfaces.
- `tests/test_strategy_manager_routes.py` — Result, comparison, landing markup coverage.

## Tasks & Acceptance

**Execution:**
- [x] `tests/test_strategy_manager_routes.py` — add failing Result/comparison/landing assertions for shared labels, disclosure, and navigation.
- [x] `app/api/templates/_macros.html` and the three consumers — introduce and use one provenance-label macro with fallback.
- [x] `app/api/templates/_backtest_result.html` and `_comparison.html` — add audit disclosure/back controls while preserving all existing content states.
- [x] Run focused quality checks and record results.

**Acceptance Criteria:**
- Given a Result page, when it renders, then manifest and execution-contract digests are behind a collapsed Audit details disclosure rather than Run identity.
- Given any Result/comparison/landing provenance display, when it renders, then it uses the one shared plain-language label and never shows “Observed Bau”.
- Given the Result or comparison page, when it renders, then a visible Back to Strategy Manager HTMX control is available without changing computed values or presenter behavior.

## Verification

- `uv run pytest tests/test_strategy_manager_routes.py -q`
- `uv run ruff format --check tests/test_strategy_manager_routes.py && uv run ruff check tests/test_strategy_manager_routes.py`
- `git diff --check`

## Review Triage Log

### 2026-08-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (low 2)
- defer: 0
- reject: 0
- addressed_findings:
  - `[low]` `[patch]` Trim macro output so its punctuation has no visible whitespace.
  - `[low]` `[patch]` Exclude Jinja from the Python-only Ruff format command.

## Auto Run Result

Status: done

**Implemented:** Result audit digests now live in collapsed Audit details; one shared macro gives the landing, Result, and comparison consistent plain-language provenance labels; Result and comparison include Strategy Manager back navigation.
