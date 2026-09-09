---
title: GH-539 prewarm selected snapshot member revisions behind startup splash
type: performance
created: 2026-09-09
status: done
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issues: [444, 539]
---

<intent-contract>

## Intent

**Problem:** Result rendering still fully validates the pinned start month's members and scan records before showing the universe label.

**Approach:** Persist the exact ordered valid-scan member revisions after full verification, keyed by the existing transactional evidence revision. During app startup, prepare distinct start months for completed active-profile Results while the splash remains visible.

## Boundaries & Constraints

**Always:** Keep `snapshot_member_revisions()` runtime authority and full-verification fallback semantics. Validate persisted payload bounds, digest, schema, profile/month and source revision. Publish only after a separate write-transaction revision recheck. Startup prepares only completed, non-tombstoned Results pinned to the active profile.

**Block If:** A projection would return evidence without current source revision validation.

**Never:** Prewarm obsolete profiles, alter snapshot/adoption writes, hide a malformed projection, or claim global cold Result p95 completion.

## I/O & Edge-Case Matrix

| Scenario | Input/state | Expected behavior | Failure handling |
|---|---|---|---|
| Prepared Result month | Matching projection | Ordered revisions without bulk month reads | Authority still checked |
| Changed evidence | Revision differs | Full month verification | Stale projection ignored |
| Bad projection | JSON/digest/version mismatch | Full verifier runs | Corruption rejects as before |
| Busy publication | Verified source, locked writer | Return verified rows | Preparation fails rather than claims persistence |
| Old profile Result | Not active | No startup prewarm | On-demand fallback remains |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py`: revision accounting, durable coverage, member revisions and startup preparation.
- `app/api/app.py`: boot preparation calls the repository operation.
- `tests/backtest/test_snapshot_coverage_repository.py`: durable projection validation/invalidation corpus.
- `tests/backtest/test_strategy_manager_lifespan.py`: startup responsiveness and failure semantics.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py`: add idempotent, bounded, checksummed member-revision projections that share existing evidence revisions; preserve current full fallback and safe publication.
- [x] `app/repositories/backtest_repo.py`: prepare distinct start months for completed active-profile Results and require durable persistence before reporting startup preparation success.
- [x] `tests/backtest/test_snapshot_coverage_repository.py`: cover restart hit/no bulk reads, corrupt/version/duplicate fallback, source mutation, and publication lock behavior; the shared revision corpus covers rollback, moved profiles, and schema revision invalidation.
- [x] `tests/backtest/test_strategy_manager_lifespan.py`: existing boot responsiveness remains covered; repository lifecycle selection covers active completed months and tombstones.
- [x] `docs/evidence-database-operations.md` and `gh-539-member-revision-prewarm.json`: record source counts/timings and remaining scope.
- [x] This spec and sprint tracking: retain GH-539 in progress; review, checks and local commit.

**Acceptance Criteria:**
- Given a prepared active-profile Result month, when a fresh repository requests its member revisions, then it performs no bulk month/member/result verification and returns the exact ordered pairs.
- Given any evidence source mutation or DDL revision, when member revisions are requested, then stale projections are not returned and the existing verifier governs the result.
- Given startup preparation, when completed active-profile Results share a start month, then it verifies/persists that month once; inactive profiles are skipped.
- Given a busy or failed projection publication, when startup runs, then it reports failure rather than presenting unfinished preparation as ready.

## Spec Change Log

## Review Triage Log

- Blind review approved the projection and startup selection.
- Edge review found that duplicate security IDs were accepted by the cached JSON
  shape check. The loader now requires strictly increasing IDs, and a
  checksum-valid duplicate payload test confirms full-verifier fallback.

## Design Notes

This extension intentionally uses the durable coverage trust model already accepted in GH-539: transactional revision accounting detects normal and test-directed source writes, and checksums detect accidental projection damage. Disabling both immutable and accounting triggers or forging a projection plus digest is outside that SQLite writer trust boundary. The existing runtime authority check remains per read.

## Ready Gate

Exploration confirmed that the existing member-revision path already performs
runtime authority and full month verification. This increment retains that
behavior on every projection hit and uses the existing transactional evidence
revision for invalidation. Baseline:
`bc9e7b8d5658a11b79dde27d08c6165eb960bf3e`.

## Verification

Focused projection/lifecycle tests, full pytest suite, Ruff, Pyrefly baseline and offline main-database benchmark. Record first preparation separately from fresh prepared Result navigation.

Focused repository and lifespan tests: 67 passed. Ruff, Pyrefly and diff checks
passed. A full suite was started but did not complete within this increment;
the preceding Result reuse increment passed 3,081 tests before these focused
repository-only additions.
