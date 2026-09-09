---
status: confirmed
created: 2026-08-30
method: bmad-mini-plan
github_issue: 444
github_url: https://github.com/ed-is-ai/Agents.stocks/issues/444
prerequisite_issue: null
architecture: _bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md
---

# Feature GH-444 — Database storage, retention, and Backtest Result performance

## Feature contract

### Problem

The historical evidence database is approximately 18 GB because immutable
price history is retained as both full canonical JSON manifests and normalized
observation rows, with repeated revision keys and indexes adding overhead. The
Backtest database is approximately 2.5 GB, and opening a completed Result can
synchronously scan and hash a whole profile and verify every snapshot month.
The evidence model correctly protects deterministic replay, but it has no safe
reference-aware reclamation path, while SQLite contention failures currently
lose the diagnostic detail needed for reliable operations.

### Outcome

Historical evidence uses a compact, deduplicated representation; existing
databases migrate safely and resumably; unreachable evidence can be reclaimed
without touching anything pinned; SQLite contention is bounded and
diagnosable; and completed Results open through bounded reads without changing
Backtest, provenance, universe, or replay semantics.

### Success conditions

1. Production-shaped migration proves canonical digest and replay equivalence
   for every revision without provider/network access.
2. The migrated historical cache is at least 70% smaller than v1 on the agreed
   benchmark, or cutover stops for an explicit evidence-backed review.
3. Migration is side-by-side, resumable, capacity-checked, atomically cut over,
   and recoverable through a tested rollback.
4. GC dry-run and execution prove that no referenced revision or reachable
   chunk can be deleted.
5. Completed Result rendering performs no request-time whole-profile scan or
   hash and meets p95 below two seconds warm and five seconds cold on the
   current 241-month reference profile.
6. Contention policy is bounded and tested, and exhausted retries retain the
   original actionable SQLite exception/code and operation context.
7. Operator documentation and metrics cover storage, migration, verification,
   cutover, rollback, retention, and recovery.

## Confirmed scope and constraints

- Preserve immutable evidence, deterministic reconstruction, pinned revision
  retention, sealed universe semantics, and existing financial results.
- Keep persistence behind repositories and schema evolution additive and
  idempotent. Routes continue to call services rather than databases.
- Do not manually compress or delete a live SQLite file and do not treat
  `VACUUM` as the storage solution; the measured database has no meaningful
  freelist to reclaim.
- Do not fetch replacement market data during migration.
- Unreferenced is not synonymous with disposable. The authoritative evidence
  reference graph, a grace period, dry-run output, and an auditable operator
  action govern deletion.
- Treat the measured 90.6% compression saving for a 25-manifest sample as
  design evidence, not a guarantee for the complete database.

## Measured baseline

| Area | Baseline |
|---|---|
| Historical evidence | 4,198 revisions, 903 securities, 31,349,272 observations |
| Historical footprint | Manifests 7.69 GiB; observations 7.04 GiB; observation index 2.78 GiB |
| Retention | 2,628 referenced and 1,570 currently unreferenced revisions |
| Compression sample | 50.2 MiB to 4.7 MiB with zlib level 6 (10.6x) |
| Backtest footprint | Reconstruction cache 830.9 MiB; monthly results 829.0 MiB; members 684.2 MiB |
| Reference profile | 241 months, 874 securities, 210,634 expected member-months |
| Result-path timing | Coverage 98.55 s cold / 4.01 s warm; member revisions 3.46 s; base result 0.189 s |

## Target design

### Historical evidence schema v2

- Introduce an integer surrogate revision key for internal relationships.
- Store versioned application-compressed, content-addressed history chunks,
  initially evaluating annual chunks for bounded reads.
- Map revisions to chunks so overlapping immutable revisions share identical
  content. Retain canonical uncompressed digests as evidence identity.
- Reconstruct through bounded streaming/decompression and avoid retaining two
  complete physical copies of every series unless benchmarks justify one.

### Migration and retention

- Build v2 beside v1 with disk preflight, durable checkpoints, restartability,
  per-revision verification, observable progress, atomic cutover, and rollback.
- Compute GC reachability from authoritative evidence references. Produce a
  reasoned dry-run plan, apply a grace period, and make deletion transactional,
  recoverable, and auditable.

### Backtest Result access

- Replace request-time row rendering/hashing with durable O(1) revisions or
  content digests maintained transactionally with every source mutation.
- Persist or incrementally maintain the coverage/provenance summary required by
  the Result screen, with deterministic invalidation tests.
- Preserve exact coverage, alias, universe, and provenance integrity checks.

### Concurrency and observability

- Define journal mode, busy timeout/retry limits, transaction duration, read
  snapshots, and checkpoint behaviour for both databases and test readers with
  writers under realistic load.
- Record original SQLite failure class/code and safe operation context. Expose
  database size/counts, migration/compression progress, GC candidates, and
  relevant latency measurements.

## Ordered implementation stories

### Story 1 — Establish baselines, observability, and contention policy

Add reproducible size/latency benchmarks, actionable SQLite diagnostics,
database configuration policy, and reader/writer concurrency tests. This story
must land before optimization claims are accepted.

### Story 2 — Add historical evidence schema v2

Implement compressed content-addressed chunks, integer revision relationships,
bounded reconstruction, compatibility reads, and digest/replay tests behind the
historical repository boundary. Benchmark chunk size before freezing it.

### Story 3 — Migrate and cut over safely

Implement preflight, checkpoints, interruption/restart, complete verification,
atomic activation, and rollback without network access. Record a
production-shaped migration report.

### Story 4 — Reclaim unreachable evidence safely

Implement authoritative reachability, dry-run reporting, grace-period policy,
audited execution, transactional failure handling, and recovery tests. Prove
zero deletion of pinned or transitively reachable evidence.

### Story 5 — Optimize Backtest storage and Result reads

Add transactionally maintained revisions/summaries, remove whole-profile work
from the request path, test every invalidation source, and meet the agreed cold
and warm Result benchmarks.

### Story 6 — Roll out and operate schema v2

Rehearse backup/restore and rollback, publish capacity and recovery guidance,
stage the production migration, verify post-cutover semantics and performance,
and make v2 the supported default for new installations.

## Dependencies and sequencing

Story 1 precedes all performance acceptance. Story 2 precedes Stories 3 and 4;
Story 3 precedes live v2 GC. Story 5 may proceed alongside schema migration once
the shared observability conventions are established. Story 6 closes the epic
only after all benchmark and safety evidence is recorded.

## Deferred child-issue creation

This planning request intentionally creates one GitHub epic. Child issues will
be created from the six ordered stories during sprint planning, allowing their
acceptance criteria and file-level impact to incorporate the baseline work
rather than prematurely freezing implementation details.

## Decisions recorded for review

1. Annual content chunks are the starting hypothesis, subject to benchmarks.
2. A 70% whole-cache reduction is the cutover target; the smaller manifest
   sample does not justify promising a 90% whole-database reduction.
3. Durable revisions/summaries must be transactionally updated, not repaired
   lazily from a whole-profile scan on a user request.
4. WAL alone is insufficient; timeout, retry, transaction, checkpoint, and
   observability behaviour form one tested contention policy.
5. The epic creates no child issues yet and authorizes no database migration or
   deletion by itself.

## Verification strategy

- Golden canonical digest and reconstruction comparisons across all revisions
  in a production-shaped database copy.
- Representative Backtest replay/result equality before and after migration.
- Forced interruption at migration checkpoints and successful resume/rollback.
- Reachability property tests and seeded pinned/unpinned GC scenarios.
- Concurrent reader/writer integration tests with timeout and recovery cases.
- Repeatable cold/warm Result benchmarks with query/row-count evidence proving
  the request path is bounded.


## Delivery tracking update — 2026-09-08

Child stories were created during development planning: #535 baseline/diagnostics (review), #536 compact schema (backlog), #537 migration/rollback (backlog), #538 retention/GC (backlog), #539 bounded Result reads (backlog), #540 rollout (backlog). The original child-creation deferral above records the August planning decision; it is now fulfilled. The epic remains in progress.
