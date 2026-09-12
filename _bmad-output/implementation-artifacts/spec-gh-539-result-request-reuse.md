---
title: GH-539 eliminate redundant Result verification within Strategy Manager requests
type: performance
created: 2026-09-09
status: done
final_revision: 32fc368ba0b9fd0b8ed7e74a5c7de502ea175548
baseline_revision: 5824ecc292cd2499fd26e63eeaf90379e981ce18
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issues: [444, 539]
---

<intent-contract>

## Intent

**Problem:** Compare and picker requests reconstruct and hash the same immutable Result multiple times. The current main database has small Result payloads, but this is unnecessary work and grows with Result size.

**Approach:** Reuse a Result already fully reconstructed in the same request, without durable Result shortcuts. Keep the existing full reconstruction and digest verification on each independent public repository read.

## Boundaries & Constraints

**Always:** Preserve public `backtest_result()` tamper detection, job eligibility checks, comparison predicate, ordering, errors and rendered data. Reuse only Results freshly verified in the caller's request.

**Block If:** The improvement requires trusting a persisted or cross-request Result summary without re-verifying base evidence.

**Never:** Weaken tests that deliberately corrupt immutable Result evidence, bypass Result digest checks, alter comparison eligibility, or represent small-payload timing as meeting the story's cold p95 target.

## I/O & Edge-Case Matrix

| Scenario | Input/state | Expected behavior | Failure handling |
|---|---|---|---|
| Compare picker | Verified anchor | Candidate lookup reuses it | Tampered candidates still reject |
| Comparison page | Two complete Results | Each is reconstructed once | Ineligible/tampered evidence retains explicit response |
| Independent request | Same run later | Full repository verification repeats | No cross-request trust |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py`: comparison eligibility/candidate reads and full Result verifier.
- `app/api/routes/strategy_manager.py`: picker and comparison request contexts.
- `tests/backtest/test_backtest_repo_comparison.py`: comparison correctness and corruption corpus.
- `tests/test_strategy_manager_routes.py`: route rendering/error behavior.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py`: add narrow optional reuse inputs or a repository operation that accepts freshly verified Results, retaining every existing eligibility and Result verification boundary.
- [x] `app/api/routes/strategy_manager.py`: pass freshly verified Results through picker and comparison contexts so an anchor is read once and each comparison side once.
- [x] `tests/backtest/test_backtest_repo_comparison.py` and `tests/test_strategy_manager_routes.py`: count full verifier reads on picker/comparison paths; retain direct tamper rejection and independent-read behavior.
- [x] `_bmad-output/implementation-artifacts/gh-539-result-request-reuse.json` and `docs/evidence-database-operations.md`: record main-database payload counts and explain that coverage/member work, not Result payload size, dominated the measured latency.
- [x] This spec and sprint tracking: retain GH-539 in progress for bounded cold Result reads; local commit with review and checks.

**Acceptance Criteria:**
- Given a picker request with a valid anchor, when eligible candidates are listed, then the anchor Result is fully reconstructed once.
- Given a comparison request with two valid Results, when eligibility and rendering occur, then each side is fully reconstructed once while the existing predicate and output remain unchanged.
- Given deliberately tampered Result evidence, when any affected read occurs, then the existing integrity error still occurs before presentation.
- Given independent requests, when a Result is loaded again, then no persisted or cross-request Result trust is introduced.

## Spec Change Log

## Review Triage Log

### 2026-09-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 0
- reject: 0
- addressed_findings:
  - none
- Both required independent review agents were launched but reached their service usage limit before returning findings. Manual adversarial review verified that only Results created by the current route call are passed as reuse inputs; ordinary `backtest_result()` calls still reconstruct and validate persisted evidence, and tampered candidate evidence still raises.

## Auto Run Result

Implemented same-request Result reuse. The picker verifies its anchor once;
the comparison page verifies each Result once and reuses those objects for
eligibility and presentation. No process or durable Result cache was added.

The offline main database has three completed Results: 446 equity points and
194 trade events total, with the largest event payload about 69 KB. This work
removes duplicate reconstruction but does not claim cold Result p95 acceptance;
profile coverage/member verification was the measured bottleneck and is prepared
at startup by the previous increment.

Verification: focused comparison/route suite 209 passed; full suite 3,081
passed (27 warnings); Ruff and diff whitespace checks passed. Pyrefly retains
the pre-existing 33 alert-agent errors. Review agents were unavailable due to
their usage limit; manual review found no additional issue. GH-539 remained open
for bounded cold Result/member reads at the time of this implementation.

### Closure addendum — 2026-09-12

The remaining bounded completed-Result gate is now evidenced by
`gh-539-result-rendering.json`: the current-authority profile has a ready
241-month interval, and a completed Result rendered with HTTP 200 for every
sample. Warm p95 was 0.120 seconds and cold p95 was 3.325 seconds. The cold
measurement excludes application startup and records OS page-cache state as
uncontrolled; both values are below the GH-539 thresholds. The story is complete.

## Design Notes

Main offline data contains three completed Results: the reference has 400 curve points and 194 events (about 69 KB event JSON); the other two have 23 curve points and no events. The profile has 210,634 snapshot members, so coverage/member verification was the material bottleneck. Durable Result projections would weaken current corruption semantics unless fully revalidated; this increment deliberately does not add them.

## Verification

Focused comparison and route tests, full pytest suite, Ruff, Pyrefly baseline check and `git diff --check`. Record offline payload counts and request verifier-call counts; report remaining cold scope honestly.
