---
title: 'GH-539 remove whole-profile hashing from repeated Strategy Manager navigation'
type: bugfix
created: '2026-09-09'
status: done
final_revision: 215e0fe8daefc0a15325357b18956506386f306b
baseline_revision: c1c5223f8fb84f1109fb761fae229e566ba7bae6
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issues: [444, 539]
---

<intent-contract>

## Intent

**Problem:** Moving between Strategy Manager screens is slow because every coverage-cache hit rereads and hashes all profile members and scan results. Several views repeat this work in the same request.

**Approach:** Replace that invalidation scan with durable SQLite revision counters maintained in the source transactions. Check runtime/profile authority before any expensive verification, retain full verification on genuine cache misses, and measure real navigation before/after on an offline copy of the approved main database. This is the navigation increment of GH-539; durable verified summaries and cold successful Result rendering remain tracked under that open story.

## Boundaries & Constraints

**Always:** Preserve coverage, aliases, roster, provenance, runtime authority, corruption detection and existing job/replay behavior. Invalidate transactionally for every source previously hashed. Preserve old/new profile identity updates and rollback semantics. Reuse the repository's existing read transaction, lock and bounded process cache.

**Block If:** Improvement requires returning unverified coverage or dropping an existing integrity check.

**Never:** Cache by time-to-live; bypass invalid detector authority; change immutable evidence; run benchmark writes on live data; claim that warm-cache improvements satisfy GH-539's unimplemented cold-read/durable-summary criteria.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected behavior | Failure handling |
|---|---|---|---|
| Repeated navigation | Same validated profile and revision | Indexed constant-size revision lookup and reuse verified summary | Runtime authority still checked |
| Source mutation | Member/result/month/profile/roster/alias change | Committed counter change invalidates old summary | Full existing verifier rejects corruption |
| Rollback | Evidence transaction aborts | Counter also rolls back | Previous valid cache remains usable |
| Moved identity | UPDATE changes profile key | Both old/new keys invalidate | No stale old-key summary |
| Restart | New repository process | Rebuild verified process cache | No new trust in unverified stored summaries |
| Bad runtime authority | Pinned detector differs | Fail before scanning profile payloads | Preserve BacktestIntegrityError |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py` — schema, `_snapshot_coverage_revision`, `snapshot_coverage` and existing verified summary cache.
- `tests/backtest/test_snapshot_coverage_repository.py` — real SQLite fixtures, cache/corruption/concurrency checks.
- `app/api/routes/strategy_manager.py`, `app/services/backtest/strategy_readiness_service.py`, `app/services/backtest/backtest_launch_service.py` — shared coverage callers on landing/readiness/initialization/configuration/Result screens; no per-route caching workaround needed.
- `docs/evidence-database-operations.md` — baseline, trust limitations and performance report.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py` — additive/idempotent revision schema with independent AFTER INSERT/UPDATE/DELETE triggers for all nine previous invalidation sources; profile-scoped generations for profile/month/member/result tables and shared generation for active selection, rosters and aliases. Include database epoch/schema generation in identity so recreation or DDL cannot reuse a stale process cache.
- [x] `app/repositories/backtest_repo.py` — replace stored-byte scanning/hash with indexed counter lookup, validate runtime/profile authority first, and preserve miss/error/publication/read-snapshot behavior.
- [x] `tests/backtest/test_snapshot_coverage_repository.py` or focused adjacent test module — prove zero member/result reads on hits; second-connection invalidation, old/new keys, shared sources, rollback, unrelated job writes, restart and schema-change invalidation. Keep existing corruption and serialized-reader tests passing.
- [x] `scripts/benchmark_strategy_navigation.py` — repeatable offline navigation benchmark using real repository/services, disabled worker lifecycle, explicit database/code/snapshot identity and honest first/warm/error labels; no live schema writes or provider calls.
- [x] `_bmad-output/implementation-artifacts/gh-539-navigation-before.json`, `gh-539-navigation-after.json`, `docs/evidence-database-operations.md` — retain evidence and explain measured improvements and remaining cold-cache cost.
- [x] BMAD sprint/GitHub tracking and spec — mark GH-539 in progress with navigation increment implemented; keep compact storage GH-536 queued.

**Acceptance Criteria:**
- Given a verified warm coverage cache, when any Strategy Manager caller reads it, then no snapshot-member or scan-result query and no full-profile hash occurs.
- Given any INSERT/UPDATE/DELETE of evidence formerly included in the hash, when committed from another connection, then the next affected coverage read revalidates; rolled-back changes do not invalidate.
- Given a runtime-invalid profile, when requested, then the same integrity error occurs before bulk evidence access.
- Given concurrent readers, when a cache miss occurs, then only one verifier publishes a complete summary and failed verification leaves no reusable stale summary.
- Given approved main data copied offline, when real screens are measured before/after, then response timings/statuses are recorded without presenting error responses as successful Result benchmarks.
- Given this change, when tests and quality checks run, then introduced regressions are resolved and unrelated existing failures are identified.

## Spec Change Log

## Review Triage Log

## Design Notes

Independent invalidation triggers survive tests or recovery tooling dropping immutability triggers. Counter updates happen in the same transaction as source changes. A shared roster/alias generation conservatively invalidates all profile caches for infrequent shared changes; ordinary member/result writes invalidate only their profile. Worker heartbeats/job rows are not coverage inputs. Schema version adds conservative DDL invalidation; arbitrary file tampering or deliberately disabling accounting is outside cache trust, as before.

The first valid cold read still runs full integrity verification. GH-539 stays open for persisted verified summaries and bounded cold Result access. User steering prioritizes this measured navigation bottleneck over GH-536, which has no dependency on GH-539.

## Verification

- Focused snapshot coverage/revision tests, then the full repository suite.
- Ruff on changed Python files; Pyrefly with the installed project interpreter; `git diff --check`.
- Real navigation benchmark on the isolated main Backtest copy with workers disabled; compare statuses and timings.


### 2026-09-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 10 (high 1, medium 4, low 5)
- defer: 0
- reject: 0
- addressed_findings:
  - [high] [patch] Prevent benchmark output from overwriting the source database, including symlinks/hardlinks; same-file invocation rejects before opening the database.
  - [low] [patch] Explicitly retain both ignored benchmark artifacts in the local commit.
  - [low] [patch] Explicitly retain the ignored specification in the local commit.
  - [medium] [patch] Record repository implementation SHA-256 in both measured artifacts and future benchmark output.
  - [medium] [patch] Record source size/schema metadata in future runs and document the same-backup identity and missing initial fingerprint honestly.
  - [medium] [patch] Future benchmarks record response digest and alert/error markers; initial runs did not capture these and retain that explicit limitation.
  - [medium] [patch] Preserve completed request measurements and exception type when a route raises.
  - [low] [patch] Assert unaffected-profile isolation for profile counters and conservative shared invalidation.
  - [low] [patch] Cover rolled-back moved-key UPDATE and DELETE as well as INSERT.
  - [low] [patch] Use SQLite authorizer table reads to reject all bulk evidence reads on cache hits, regardless of query spelling.

## Auto Run Result

Implemented the navigation increment of GH-539. Warm navigation medians fell from 4.6–9.9 seconds to 26–78 milliseconds on the same approved main SQLite backup. First landing remained 126 seconds; persisted summaries, bounded cold Result reads, and the complete GH-539 p95 criteria remain unfinished. GH-539 stays in progress and GH-536 remains queued.

Changed files:
- `../../app/repositories/backtest_repo.py`: additive transactional revision accounting and authority-first cache lookup.
- `../../tests/backtest/test_snapshot_coverage_repository.py`: warm-read, invalidation, rollback, isolation and authority tests.
- `../../scripts/benchmark_strategy_navigation.py`: disposable-backup navigation measurement, outcome/provenance metadata and safe output guard.
- `../../docs/evidence-database-operations.md`: measured results and cold-read limitations.
- Local sprint/context, this spec and before/after JSON: scope/progress and retained measurement evidence.

Verification:
- Full pytest suite: 3,045 passed; four browser setup errors from sandbox localhost binding. All four passed when rerun with local-server permission (3,049 total passing).
- Focused coverage suite: 34 passed; rerun after review test improvements.
- Ruff and git diff whitespace checks pass.
- Pyrefly: unchanged 33 errors in the pre-existing alert agent; no new errors in this implementation.
- Benchmark CLI smoke run: all five routes return HTTP 200; output/source collision exits with argparse error before opening source.
- Independent blind and edge-case reviews completed; no production repository changes were needed after review.

Residual limitations: initial before/after evidence has two warm samples per route, uncontrolled OS caches and no semantic response markers or full database fingerprint. HTTP 200 is not evidence of a successfully rendered completed Result. Runtime-invalid reference profiles remain rejected. No live database was modified for these benchmarks. Commit is local only.
