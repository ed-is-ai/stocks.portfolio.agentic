# Epic 444 Context: Database storage, retention, and Backtest Result performance

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->
<!-- Epic source: _bmad-output/planning-artifacts/feature-gh-444-database-storage-performance.md. Relevant architecture: _bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md. -->

## Goal

Reduce historical evidence storage and completed Backtest Result latency while preserving immutable evidence identity, deterministic replay, and existing financial and universe semantics. Supply safe migration, retention, recovery, and SQLite operational controls so compact storage becomes the supported default only after measured equivalence and performance validation.

## Stories

- Story 444.1: Establish baselines, observability, and contention policy
- Story 444.2: Add historical evidence schema v2
- Story 444.3: Migrate and cut over safely
- Story 444.4: Reclaim unreachable evidence safely
- Story 444.5: Optimize Backtest storage and Result reads
- Story 444.6: Roll out and operate schema v2

## Requirements & Constraints

Migration must prove canonical digest and replay equivalence for every revision in a production-shaped copy without network or provider access. The whole historical cache must shrink by at least 70 percent against the agreed v1 benchmark; otherwise cutover stops for evidence-backed review. A small manifest compression sample does not establish whole-database acceptance. Preserve acquisition/provenance records, pinned revisions, sealed universes, exact coverage, aliases, and existing Backtest financial results.

Build beside the source database with disk-capacity preflight, durable checkpoints, restartable progress, per-revision verification, atomic activation, and tested rollback. Do not manually compress or delete a live SQLite file. VACUUM is not the storage solution: the original measured database had no meaningful free pages. Unreferenced evidence is not automatically disposable. Garbage collection requires an authoritative reference graph, grace period, reasoned dry-run plan, transactional execution, recovery, and an auditable operator action; pinned revisions and reachable chunks must never be deleted.

Completed Result rendering must avoid request-time whole-profile scans and hashes and meet p95 below two seconds warm and five seconds cold for the 241-month reference profile. Summary caching must preserve fail-closed integrity checks, including deterministic invalidation for every source mutation. Contention must have bounded waits/retries and retain original SQLite failure class/code and actionable operation context when exhausted. Operational evidence must cover database size/counts, latency, compression, migration progress, verification, retention, cutover, rollback, backup/restore, and recovery.

The August baseline describes 4,198 historical revisions, 903 securities, 31,349,272 observations, 2,628 referenced revisions, and 1,570 currently unreferenced revisions. Historical components measured 7.69 GiB manifests, 7.04 GiB observations, and 2.78 GiB observation index. The reference profile has 241 months, 874 securities, and 210,634 expected member-months. Reported coverage latency was 98.55 seconds cold and 4.01 seconds warm; member revisions took 3.46 seconds and base Result retrieval 0.189 seconds. These are historical observations to reproduce, not current acceptance results.

## Technical Decisions

Maintain layered routes-to-services-to-repositories architecture and additive, idempotent schema evolution. SQLite persistence remains repository-owned; Backtest stays separate from live trading and portfolio state. Historical evidence v2 introduces integer revision relationships and versioned application-compressed content-addressed chunks, preserving canonical uncompressed digests as evidence identity. Annual chunks are an initial hypothesis that must be benchmarked before the format is frozen. Overlapping revisions should share identical chunk content. Reconstruction must bound decompression and memory use; retaining duplicate full physical series needs benchmark justification.

Canonical evidence remains one security and one request interval with inclusive start and exclusive end. Preserve provider-native OHLCV and adjusted close, effective-dated actions, exchange-local sessions, quote units, aliases, provider/version, request contract/version, and response provenance. Acquisition time is audited separately from content identity. Storage deduplication must not implicitly merge revision identity or alter provider-native, as-traded, and split-continuous price/volume semantics. Preserve finite-number hexadecimal encoding, JSON nulls, and canonical ordering exactly; no heuristic repair or future-action exposure is allowed.

Replace whole-profile hashing with durable constant-size revisions or content digests updated transactionally with their source mutations. Persist or incrementally maintain verified coverage/provenance summaries outside user requests. Choose and test journal mode, timeout/retry limits, read snapshots, transaction duration, and checkpoint policy for both databases; WAL alone is insufficient. Prefer deterministic offline fixtures for correctness and explicitly separate them from production-shaped performance acceptance.

## UX & Interaction Patterns

Opening completed Results must preserve existing provenance, coverage, alias, and universe information and integrity failure behavior while removing synchronous bulk verification. Operator workflows must make dry-run candidates, progress, verification status, capacity failures, recovery, and explicit cutover/rollback actions reviewable.

## Cross-Story Dependencies

Story 444.1 precedes all accepted optimization claims. Story 444.2 precedes migration and retention; Story 444.3 precedes live v2 garbage collection. Story 444.5 may proceed alongside schema migration after shared observability exists. Story 444.6 closes the epic only after benchmark, replay, rollback, retention, and operational safety evidence is recorded. Track the epic and each implementation story in GitHub with progress matching BMAD artifacts. Child issues #535–#540 correspond to Stories 444.1–444.6; child-issue creation is complete.
