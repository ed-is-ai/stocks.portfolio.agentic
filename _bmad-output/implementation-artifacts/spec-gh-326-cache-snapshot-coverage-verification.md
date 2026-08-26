---
title: 'Cache Strategy snapshot-coverage verification by revision'
type: 'performance'
created: '2026-08-26'
status: 'done'
baseline_revision: 'dd3428b2dc2454f76d1779978e359f5494c70e09'
final_revision: 'c4bea0323fd8f24620bcf0d82781bc6c663d4355'
review_loop_iteration: 0
followup_review_recommended: false
context: ['{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md']
warnings: []
---

<intent-contract>

## Intent

**Problem:** Strategy Manager, result, and comparison views repeatedly fully verify the same immutable monthly snapshot evidence. On the current data this can freeze a request for more than 50 seconds and duplicate work when multiple views use one profile.

**Approach:** Add an explicit, revision-safe coverage identity and a concurrency-safe repository cache for verified `CoverageSummaryV1` values. Cache hits must avoid month-by-month verification while any committed coverage change, profile change, corruption signal, or process restart safely forces reconstruction.

## Boundaries & Constraints

**Always:** Preserve all existing integrity checks, profile separation, freshness semantics, and `BacktestIntegrityError` behavior. Cache keys must identify the exact profile and committed coverage revision/content. A failed verification must never return an older cached value.

**Block If:** The existing immutable evidence contract cannot expose a reliable revision or detect a changed/corrupt write set without weakening integrity guarantees.

**Never:** Persist an unverified summary as authoritative, bypass verification after a detected mutation, alter financial/backtest calculations, or move blocking database work onto async request handlers.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | Same profile and unchanged committed coverage | First lookup verifies; subsequent lookups return the same valid summary | None |
| PROFILE_SEPARATION | Two profiles with different coverage | Each profile is verified and cached independently | None |
| MUTATION | Coverage is added, replaced, invalidated, or deleted | Previous entry is unusable and a fresh verification occurs | Preserve integrity errors |
| CORRUPTION | Evidence or revision metadata is tampered with | No stale cached summary is returned | Raise `BacktestIntegrityError` |
| CONCURRENT_ACCESS | Readers and a writer race on one profile | At most one valid value is published for a revision; no partial value escapes | Retry/serialize safely; preserve errors |
| RESTART | New repository/process reads durable coverage | Cache starts empty and reconstructs by normal verification | Preserve durable-state validation |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py` -- owns snapshot schema, atomic month commits, integrity verification, and `snapshot_coverage`.
- `tests/backtest/test_snapshot_coverage_repository.py` -- existing coverage integrity and profile-isolation tests; add cache, invalidation, corruption, concurrency, and restart assertions.
- `app/repositories/db.py` -- database initialization/migration owner if a durable revision table or migration is required.
- `app/api/routes/strategy_manager.py` -- representative Strategy Manager coverage callers; should continue using repository semantics without duplicating cache logic.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py` -- implement an explicit revision/content identity for committed snapshot coverage and a thread-safe verified-summary cache scoped by repository/database and profile -- avoid repeated full manifest verification while retaining fail-closed integrity behavior.
- [x] `app/repositories/backtest_repo.py` -- invalidate or bypass cached entries on every supported coverage/profile mutation and on any revision/content mismatch -- prevent stale data after commits, invalidation, deletion, or corruption.
- [x] `tests/backtest/test_snapshot_coverage_repository.py` -- add deterministic tests for cache hits, profile separation, all mutation invalidations, corruption, concurrent readers/writers, and process/repository restart reconstruction -- prove the acceptance contract without timing flakiness.
- [x] `tests/backtest/test_snapshot_coverage_repository.py` -- instrument verification and record representative before/after call-count/timing evidence in test output or a documented test note -- make the performance improvement reviewable.

**Acceptance Criteria:**
- Given unchanged committed coverage for one profile, when coverage is requested repeatedly, then only the first request fully verifies the write sets and later requests reuse the valid summary.
- Given a cached entry, when its profile or committed coverage revision/content differs, then the entry cannot be returned and the new state is verified independently.
- Given evidence that is corrupt or inconsistent, when coverage is requested, then the repository raises `BacktestIntegrityError` and never masks it with an older cached result.
- Given result and comparison callers reference the same profile and revision, when they request coverage in one process, then they share one verified summary without changing their existing output.
- Given a process/repository restart, when durable coverage is requested, then the cache is safely reconstructed from durable state and corruption remains detectable.
- Given concurrent readers and a concurrent committed coverage change, when coverage is requested, then callers receive only a complete summary for a single revision and no race publishes partial or stale state.
- Given the regression suite, when the targeted repository tests and full quality gates run, then all pass and the verification-count/timing evidence is recorded.

## Spec Change Log

## Review Triage Log

### 2026-08-26 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 1, medium 1, low 0)
- defer: 3: (high 0, medium 2, low 1)
- reject: 7: (high 0, medium 2, low 5)
- addressed_findings:
  - `[high][patch]` Coverage verification and revision calculation now run in one explicit SQLite read transaction, preventing a writer from publishing a summary under the wrong revision.
  - `[medium][patch]` The process-local cache is bounded to prevent unbounded growth as immutable profiles accumulate.

Deferred findings: repository-wide locking may serialize unrelated profile misses; revision construction still scans stored evidence rows; broader mutation/timing benchmarks would improve operational evidence. These do not weaken correctness or the acceptance tests in this run.

## Design Notes

The cache is repository-owned because callers already converge on `BacktestRepository.snapshot_coverage`. The revision may be durable or derived from immutable committed content, but it must be cheap to inspect and must not replace the existing full verifier on a miss.

## Verification

**Commands:**
- `uv run pytest tests/backtest/test_snapshot_coverage_repository.py` -- expected: targeted coverage repository tests pass.
- `uv run pytest` -- expected: full repository test suite passes.
- `uv run ruff check app/repositories/backtest_repo.py tests/backtest/test_snapshot_coverage_repository.py` -- expected: no lint findings.
- `uv run ruff format --check app/repositories/backtest_repo.py tests/backtest/test_snapshot_coverage_repository.py` -- expected: files already formatted.
- `git diff --check` -- expected: no whitespace errors.

## Auto Run Result

Status: done

Implemented repository-local, thread-safe caching of verified `CoverageSummaryV1` values, keyed by an exact content identity over the profile and immutable evidence, with explicit transactional reads, fail-closed corruption behavior, profile separation, bounded memory, and restart-safe reconstruction. Added deterministic cache-hit, invalidation, corruption, concurrency, and restart tests.

Files changed:
- `app/repositories/backtest_repo.py` -- coverage cache, revision identity, transactional read, and bounded eviction.
- `tests/backtest/test_snapshot_coverage_repository.py` -- acceptance and verification-count regression tests.

Review findings: 2 patch findings applied; 3 low/medium design/evidence items deferred; 7 low/medium findings rejected as non-blocking or already covered by the immutable/read-transaction contract.

Verification: targeted suite 23 passed; full suite 2,002 passed with 5 existing warnings; Ruff lint and format checks passed; `git diff --check` passed. Pyright is unavailable; pyrefly retains unrelated pre-existing errors.

Residual risk: revision identity construction remains proportional to stored evidence rows, so a future durable monotonic revision/index may improve very large profiles without changing the verifier contract.
