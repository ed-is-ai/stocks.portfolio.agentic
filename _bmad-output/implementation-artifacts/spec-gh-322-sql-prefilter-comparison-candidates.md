---
title: 'GitHub #322: SQL-prefilter comparison candidates'
type: 'feature'
created: '2026-08-27'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** The Compare picker currently loads and integrity-verifies every completed backtest Result before discarding candidates with obviously incompatible persisted comparison dimensions. That work grows with all historical results rather than the plausible choices.

**Approach:** Use the anchor Result's six persisted comparison dimensions to select plausible completed Backtest IDs in SQL, then preserve the existing authoritative job checks, Result verification, and canonical in-memory eligibility predicate for every displayed candidate.

## Boundaries & Constraints

**Always:** Keep `is_comparable` unchanged as the stale-submit authority. The SQL filter must use only `strategy_runs` fields already compared by `_compare_eligible_results`: start/end month, profile hash, ordered-month digest, base currency, and execution-contract digest. Every SQL-selected candidate must still pass `_comparison_job_reason`, `backtest_result`, and `_compare_eligible_results` before it is displayed. Preserve `enqueue_seq DESC` ordering and anchor exclusion.

**Block If:** The canonical predicate depends on a comparison dimension that is not persisted with the candidate's `strategy_runs` row.

**Never:** Do not add a migration, change eligibility semantics, trust the SQL projection as evidence verification, weaken stale-submit validation, or turn missing/tampered selected Results into silently eligible candidates.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Mixed history | Anchor plus compatible peers and peers mismatched on each persisted dimension | SQL selects only dimension-matching plausible IDs; output is the same compatible peers in descending enqueue order | No error |
| Selected tampering | A dimension-compatible completed peer has invalid stored Result evidence | Candidate is loaded and `BacktestIntegrityError` propagates | Integrity error is not hidden |
| Missing/incomplete/deleted/self | Candidate row is absent, incomplete, tombstoned, or the anchor itself | It is absent from the picker or handled by existing anchor/job checks | No false candidate |
| Stale submit | Candidate changes after picker rendering | `is_comparable` remains the unchanged authoritative revalidation path | Existing explicit rejection remains |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py` -- owns the candidate query, Result digest verification, and canonical comparison predicate.
- `tests/backtest/test_backtest_repo_comparison.py` -- real SQLite comparison fixtures and candidate-order/integrity regressions.
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` -- Epic 319 implementation visibility.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py` -- replace the broad completed-job candidate query with a parameterized join to `strategy_runs`, constrained by the anchor's six persisted comparable dimensions, while retaining the existing defensive checks and Result loading for selected rows.
- [x] `tests/backtest/test_backtest_repo_comparison.py` -- add a mixed-fixture regression that proves results mismatched on each SQL dimension are not loaded, compatible peers retain canonical output/order, and selected evidence tampering still raises.
- [x] `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` -- record #322 and the already delivered #323 work in the Epic 319 completion ledger, removing stale duplicate tracking.

**Acceptance Criteria:**
- Given completed Backtest history containing clear persisted-dimension mismatches, when `comparison_candidates` runs, then it only loads plausible IDs and returns the same canonical compatible set in `enqueue_seq DESC` order.
- Given a SQL-selected candidate, when it is displayed, then it is still job-checked, loaded through `backtest_result`, integrity-verified, and evaluated by `_compare_eligible_results`.
- Given malformed, deleted, incomplete, missing, self, or stale data, when candidates are listed or later submitted, then no invalid candidate is displayed and `is_comparable` retains its existing authoritative behavior.

## Spec Change Log

## Review Triage Log

- 2026-08-27: 0 intent_gap, 0 bad_spec, 0 patch, 0 defer, 2 reject.
  - `[high][reject]` The review proposed loading completed candidates whose
    `strategy_runs` row is missing or whose stored comparison fields differ.
    Rejected: #322 explicitly changes candidate enumeration to a necessary
    SQL prefilter and requires Result integrity verification for *displayed*
    candidates; missing/malformed/incompatible rows are required to be
    excluded or handled. `is_comparable` remains unchanged as the
    authoritative stale-submit and direct-validation path.
  - `[medium][reject]` The specification artifact is ignored by Git by
    project convention. It will be explicitly force-added with the commit,
    alongside the tracking ledger.

## Design Notes

The SQL predicate is deliberately a necessary-condition prefilter, not a second source of truth. The in-memory predicate remains after loading because persisted rows can be absent or tampered with and future comparison rules may be stricter than the indexable fields.

## Verification

**Commands:**
- `uv run pytest tests/backtest/test_backtest_repo_comparison.py -q` -- comparison semantics, integrity, and load-count regressions pass.
- `uv run pytest tests/backtest/ -q` -- Backtest repository and consumer regressions pass.
- `uv run ruff format --check app/repositories/backtest_repo.py tests/backtest/test_backtest_repo_comparison.py` -- no formatting changes needed.
- `uv run ruff check app/repositories/backtest_repo.py tests/backtest/test_backtest_repo_comparison.py` -- no lint errors.

## Auto Run Result

**Summary:** Candidate enumeration now SQL-prefilters completed Backtest jobs
against the anchor's six persisted comparable dimensions, then keeps the
existing job-level checks, Result digest verification, and in-memory canonical
predicate for every displayed candidate.

**Verification:** `uv run pytest tests/backtest/test_backtest_repo_comparison.py
-q` (25 passed); `uv run pytest tests/backtest/ -q` (886 passed, 2 warnings);
Ruff format/check and `git diff --check` passed.
