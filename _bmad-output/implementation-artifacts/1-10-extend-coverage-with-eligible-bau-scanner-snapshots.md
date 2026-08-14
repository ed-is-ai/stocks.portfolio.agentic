---
baseline_commit: 78fc219
github_issue: 193
---

# Story 1.10: Capture Scanner-Owned BAU Evidence and Extend Coverage

Status: done

## Story

As a portfolio owner,
I want eligible completed month-end BAU scanner runs to preserve the exact raw market evidence they used and extend compatible historical coverage automatically,
so that historical initialization is only needed to fill missing past coverage.

## Acceptance Criteria

1. Before an eligible month-end run starts scanning, the scanner resolves one active compatible profile, immutable roster, effective identities/aliases, canonical MIC sessions and allowed source cutoffs. If any authority is unavailable, capture mode is disabled visibly; normal scanning continues unchanged.
2. Capture mode is enabled only for the first eligible BAU run after all roster MIC month-end sessions close. It explicitly targets the immediately closed `YYYY-MM`; a missed capture is not silently backfilled by a later live scan and remains available to historical initialization.
3. In capture mode, the scanner records the raw yfinance bars/actions and acquisition instant actually fetched for every expected roster member before converting data into `StockRecord` or analysis output. It also records resolved identity, alias revision, MIC, canonical session, provider request contract, cutoff, detector input manifests and payload digests.
4. A separate `ObservedBauRecordBuilder` consumes only that run-owned raw-evidence type. `HistoricalScanReconstructor` is fixed to `best_effort_reconstructed`; neither it nor a caller-supplied provenance flag can produce `observed_bau`.
5. Missing, ambiguous, post-cutoff, malformed, mixed-source, or partial member evidence rejects the entire observed month. No historical cache lookup, post-run refetch, dashboard `StockRecord`, scan history, or analysis artifact may fill an observed member.
6. The scanner atomically publishes one versioned `BauRunEnvelopeV1` as the authoritative per-run artifact. The envelope contains the run identity/outcome, analysis payload digest, optional complete BAU capture, raw evidence references/digests, and completion state. Derived dashboard analysis output is not promotion authority.
7. Only a successfully completed envelope with a matching run ID, capture digest, active compatible profile, complete roster and canonical session/cutoff/evidence/detector/policy facts passes repository-owned `is_promotable_bau(profile, envelope)`.
8. Promotion reloads the published envelope, renders `MonthlySnapshotCommitV1` from its capture only, and uses the existing immutable `commit_snapshot_month` path. Identical replay is a no-op; different profile/month content is an integrity error. It never creates or touches Strategy Manager jobs.
9. Any successful later BAU run replays eligible, completed-but-uncommitted envelopes. A crash, restart, or promotion warning must therefore not lose a valid observed month. Current/future months and partial/failed envelopes never promote.
10. Evidence references are reconciled idempotently from the winning immutable snapshot after commit. A failed/conflicting promotion leaves no new pin; a later replay repairs a failed post-commit reconciliation.
11. Capture/envelope/promotion failures are visible in BAU logging and notifications but do not change the terminal outcome or ordinary artifacts of an otherwise successful scanner run.
12. Mixed observed/reconstructed coverage remains distinct through `CoverageSummaryV1.provenance`; no raw evidence browser, source-gap list, or future coverage UI is added.

## Tasks / Subtasks

- [x] Define scanner capture-mode authority and raw evidence types (AC: 1-5)
  - [x] Add typed `BauRawEvidenceV1`, `BauCaptureMemberV1`, `BauSnapshotCaptureV1`, and `BauRunEnvelopeV1` contracts, with canonical digests and no conversion from presentation models.
  - [x] Resolve active profile/roster/identity/session authority before the qualifying scan, gate capture mode exactly once per closed month, and retain ordinary scans unchanged.
  - [x] Thread a capture sink through the scanner yfinance fetch boundary so it persists the exact provider response/actions, acquisition instant and resolved authority used by that run.

- [x] Build observed records without historical replay (AC: 3-5)
  - [x] Restrict `HistoricalScanReconstructor` to reconstructed provenance at the type/API level.
  - [x] Implement `ObservedBauRecordBuilder` over `BauRawEvidenceV1`, with no `ReconstructionRequestV1`, cache lookup, later provider fetch, or caller-controlled provenance.
  - [x] Reject the complete month for any member that is missing, ambiguous, post-cutoff, malformed, or not exact roster/session coverage.

- [x] Publish and replay one authoritative run envelope (AC: 6, 9, 11)
  - [x] Define one atomic per-run envelope publication protocol that binds analysis digest, run outcome, optional capture digest and completion state; never trust a loose capture file or marker.
  - [x] Promote only a reloaded completed envelope; replay eligible uncommitted envelopes on every later successful BAU run.
  - [x] Ensure artifact/envelope/promotion failures are warning-only for normal scanner outcome and cannot overwrite an immutable run envelope.

- [x] Enforce eligibility, immutable commit and retention reconciliation (AC: 7-10)
  - [x] Make `is_promotable_bau(profile, envelope)` validate durable ownership, completion, complete roster/session/cutoff/evidence/detector/policy facts and return one concise reason.
  - [x] Reuse `commit_snapshot_month` for immutable commit; enforce active profile inside its transaction and replay identical content as a no-op.
  - [x] Reconcile price-evidence pins from committed snapshot winners, without pre-commit/orphan pins.

- [x] Prove the boundary end to end (AC: 1-12)
  - [x] Unit-test raw capture, observed builder, rejection matrix, typed provenance separation, and predicate validation.
  - [x] Add pipeline tests for mixed US/UK completion, publication crashes, marker/file fabrication, partial/failed outcomes, missed capture, replay, profile changes, immutable collision, pin reconciliation and Strategy Manager isolation.
  - [x] Run focused Backtest/pipeline tests, full suite, Ruff, format and Pyrefly.

## Dev Notes

### Architecture and Safety Guardrails

- AD-13 defines one snapshot identity: `YYYY-MM`, with each security mapped through `XNAS -> XNYS`, `XNYS -> XNYS`, `XLON -> XLON` and its canonical last completed session. Do not use a single US close for all members.
- AD-14 owns transactional monthly readiness. `BacktestRepository.commit_snapshot_month()` is the sole immutable compare-and-insert path and already validates profile authority, closed-month authority, evidence, profile membership, manifest, and stored-row equivalence.
- AD-16 makes `is_promotable_bau(profile, run)` repository-owned and keeps BAU independent of the Strategy Manager FIFO.
- AD-18 requires exactly `observed_bau` or `best_effort_reconstructed`. An observed BAU month cannot contain a reconstructed member.
- `StockRecord`, `analysis_results.json`, scan history, and dashboard values are presentation artifacts. They may trigger neither capture nor promotion input conversion. Capture uses the active profile’s canonical roster and pinned evidence while the BAU run owns those facts.
- The existing scanner does not yet retain canonical identity-bound raw yfinance payloads. This story therefore owns the new scanner capture boundary; reusing `CanonicalSnapshotMonthProcessor` or passing `observed_bau` to the historical reconstruction path is explicitly non-compliant.
- “Atomic alongside” means a crash-safe, reloadable run-capture/artifact protocol, not merely two sequential `os.replace()` calls. Promotion always reloads the durable capture after publication.
- AD-21 requires visible local failure. Never convert corrupt/missing evidence into empty coverage, a partial observed month, or a source-gap inventory.

### Existing Code to Reuse and Preserve

- `app/services/backtest/snapshot_profile.py`: `SnapshotMemberV1`, `MonthlySnapshotCommitV1.build`, manifest digest construction, provenance invariants, and `CoverageSummaryV1`.
- `app/repositories/backtest_repo.py`: `commit_snapshot_month`, `_validate_profile_authority`, immutable duplicate/conflict checks, `snapshot_coverage`, and active-profile authority. Extend; do not open SQLite sessions in pipeline code.
- `app/services/backtest/trading_calendar.py`: closed month and per-MIC session authority.
- `app/workflows/pipeline.py`, `app/api/routes/pipeline.py`, scanner artifacts: identify the authoritative successful BAU completion seam. Preserve current pipeline locking, artifact promotion, and failure reporting.
- `app/orchestration/orchestrator.py`: the export transaction publishes run-owned analysis output last. Extend this publication boundary with `BauSnapshotCaptureV1`; do not attempt promotion from `StockRecord` or `analysis_results.json`.
- `app/services/backtest/historical_price_evidence.py`, reconstruction/profile/identity services: reuse their typed authority and immutable persistence. Do not duplicate yfinance request logic in ScannerAgent.
- `tests/backtest/test_snapshot_coverage_repository.py` and `tests/backtest/test_snapshot_profile.py`: existing fixture/building patterns for canonical observed/reconstructed commits.
- Story 1.9 UI already renders provenance. Do not alter its routes/templates unless a minimal projection compatibility fix is proven necessary.

### Previous Story Intelligence

Story 1.9 merged as PR #230. Its responsive tab change must preserve the existing SIPP Import CSV browser flow: Bootstrap must be loaded exactly once. The suite’s browser tests are a required regression check whenever shared app-shell markup is touched.

Stories 1.7–1.8 establish that durable Strategy Manager lifecycle and notification authority belong to `BACKTEST_DB`; BAU promotion must not reuse that lifecycle merely to obtain background execution.

### Expected Files

- UPDATE `app/repositories/backtest_repo.py`
- UPDATE `app/workflows/pipeline.py` and/or the existing pipeline completion service only after identifying its authoritative scanner artifact
- UPDATE `app/orchestration/orchestrator.py` and the analysis-artifact publication contract to publish the BAU capture atomically with the owning run
- UPDATE scanner/backtest capture integration only as required to resolve the active profile roster, identities, aliases, evidence, and detector fragments at a closed month end
- UPDATE scanner raw-data collection boundary to preserve run-owned provider evidence and identity/session authority for eligible capture runs
- NEW observed-BAU record builder and durable run-capture persistence/replay service
- NEW focused BAU-promotion adapter/service under `app/services/backtest/`
- NEW/UPDATE focused tests under `tests/backtest/` and pipeline tests

Do not modify concurrent SIPP import files. No new third-party dependency or paid provider is authorized.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Epic 1, Story 1.10]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-13, AD-14, AD-16, AD-18, AD-21]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/EXPERIENCE.md` — Historical Coverage Rules]
- [Source: `_bmad-output/implementation-artifacts/1-9-use-historical-initialization-and-coverage-in-the-web-ui.md` — merged UI constraints]

## Dev Agent Record

### Completion Notes List

- Implemented scanner-owned, identity-bound raw yfinance capture for the complete active profile roster, with bounded concurrent acquisition and proof that every roster ticker participated in the eligible scanner run.
- Added a one-attempt-per-profile/month SQLite authority journal binding terminal pipeline status, run ID, analysis digest, capture digest, and prepared/completed envelope digests. Dashboard analysis cannot establish completion and standalone envelope files cannot authenticate themselves.
- Promotion now reloads journal-authorized envelopes, validates the complete runtime/source/identity/session/policy contract, compares immutable snapshots semantically, isolates replay failures, and reconciles pins only from the stored winner.
- Observed records retain honest survivorship-biased roster provenance and never claim a retained TradingView screen that does not exist.
- Verification: 342 focused Backtest/scanner/pipeline tests passed; full suite produced 1,104 passes plus four sandbox-only localhost binding errors; all four browser tests passed outside the sandbox. Scoped Ruff, format, Pyrefly, and `git diff --check` passed.

### Superseded Review Findings

- [ ] [Review][Patch] BAU capture failure must not fail a published scanner run [app/orchestration/orchestrator.py:1172] — Decision: retain the successful scanner outcome and record a visible BAU-capture warning/error separately.
- [ ] [Review][Patch] Reconstructed historical records are relabelled as observed BAU [app/services/backtest/bau_snapshot_capture_processor.py:69]
- [ ] [Review][Patch] Capture is not a durable, run-owned artifact and has no eligible month-end gate [app/orchestration/orchestrator.py:1172]
- [ ] [Review][Patch] Active-profile eligibility can change between predicate and immutable commit [app/repositories/backtest_repo.py:1951]
- [ ] [Review][Patch] Required BAU capture and pipeline rejection coverage is missing [tests/backtest/test_bau_snapshot_capture_processor.py:13]

### Superseded Re-review Findings (2026-08-13)

- [ ] [Review][Patch] Reject historical reconstruction as observed BAU [app/services/backtest/bau_snapshot_capture_processor.py:71] — The capture path still calls `CanonicalSnapshotMonthProcessor`/`HistoricalScanReconstructor` and selects `observed_bau` as a parameter. Records must originate from scanner-owned, point-in-time BAU inputs rather than a reusable historical replay path.
- [ ] [Review][Patch] Bind promotion to a durably completed, owned BAU run [app/repositories/backtest_repo.py:1936] — A non-empty `source_run_id` is not ownership proof; the predicate must validate the persisted run-owned capture and its successful authoritative artifact boundary.
- [ ] [Review][Patch] Promote only a loaded published capture and make replay possible [app/orchestration/orchestrator.py:1200] — Promotion currently consumes the pre-publication object. Load and validate the published immutable capture, and retry durable unpromoted captures after crash/restart.
- [ ] [Review][Patch] Publish analysis and capture without a cross-artifact crash gap [app/orchestration/orchestrator.py:1195] — The analysis file is replaced before the separate capture. Use one durable envelope/transactional publication contract so analysis cannot claim a capture that was never published.
- [ ] [Review][Patch] Isolate capture-artifact write failures from scanner success [app/orchestration/orchestrator.py:1158] — Capture serialization and temporary-file I/O must be warning-only, retaining normal scanner artifact publication and terminal outcome.
- [ ] [Review][Patch] Make the sole eligibility predicate validate complete roster and source-cutoff evidence [app/repositories/backtest_repo.py:1936] — Validate expected member identity, canonical sessions, cutoff, evidence revision and payload digest inside the predicate rather than relying on a later commit failure.
- [ ] [Review][Patch] Make evidence references transactional with BAU promotion [app/services/backtest/bau_snapshot_capture_processor.py:100] — Pins written before promotion survive a failed/conflicting commit. Tie pin persistence to the immutable snapshot transaction or compensate safely.
- [ ] [Review][Patch] Add required BAU rejection, boundary, and recovery coverage [tests/backtest/test_bau_snapshot_capture_processor.py:13] — Cover owner/completion, reconstructed/post-cutoff rejection, mixed MICs, atomic publication/replay, warning-only failure, conflict/race, and Strategy Manager isolation.

### Review Findings (2026-08-14)

- [x] [Review][Patch] Capture is a separate pre-scan refetch, not scanner-owned evidence [app/orchestration/orchestrator.py:932] — Fixed with a run-scoped capture session invoked inside `ScannerAgent`; overlapping live tickers consume the exact captured response without a second yfinance price fetch. (AC 3–5)
- [x] [Review][Patch] A crash after normal artifact publication can permanently lose an eligible capture [app/orchestration/orchestrator.py:1198] — Fixed with a durable prepared-envelope transition and recovery from the matching run-owned analysis artifact. (AC 6, 9)
- [x] [Review][Patch] Repository eligibility does not validate durable ownership or complete source facts [app/repositories/backtest_repo.py:1688] — Fixed by requiring the reloaded envelope store and validating complete roster/session/cutoff/raw-manifest/detector/profile facts. (AC 7)
- [x] [Review][Patch] Required BAU rejection and recovery coverage is absent [tests/backtest/test_bau_run_envelope.py:1] — Added scanner reuse, raw tamper, partial evidence, prepared recovery, fabricated artifact, mixed US/UK gate, and idempotent winner-pin tests; broad pipeline/backtest regressions remain green. (AC 1–12)

### Review Findings (2026-08-14 rerun)

- [x] [Review][Patch] Add a dedicated SQLite run-authority journal — Decision: atomically record the first eligible capture attempt, terminal pipeline outcome, run ID, analysis digest, and envelope digest. Recovery and promotion require the journal record; dashboard analysis cannot establish success and a standalone fabricated envelope cannot authenticate itself. [app/repositories/backtest_repo.py:1776, app/orchestration/orchestrator.py:793] (AC 2, 6, 7, 9)
- [x] [Review][Patch] Explicitly scan and prove participation for the complete profile roster during eligible runs [app/agents/scanner/scanner_agent.py:521, app/services/backtest/bau_capture_coordinator.py:40] (AC 3–5; AD-18)
- [x] [Review][Patch] Isolate replay failures and compare an existing month with each completed envelope before treating replay as successful [app/services/backtest/bau_snapshot_promotion.py:106]
- [x] [Review][Patch] Make immutable snapshot idempotence compare semantic content rather than audit-only source run and observation timestamps [app/services/backtest/snapshot_profile.py:394]
- [x] [Review][Patch] Validate the full runtime, roster, request, detector, policy, currency/unit/timezone, and permitted capture-window authority before capture and promotion [app/repositories/backtest_repo.py:1945]
- [x] [Review][Patch] Emit truthful observed provenance; no retained TradingView screen may be claimed and the captured profile roster remains survivorship-biased [app/services/backtest/observed_bau_record_builder.py:134]
- [x] [Review][Patch] Preserve scanner skip semantics and responsiveness by rejecting short captured frames and avoiding serial full-history downloads across the complete roster [app/agents/scanner/scanner_agent.py:611, app/services/backtest/bau_capture_coordinator.py:75]
- [x] [Review][Patch] Add end-to-end tests for the chosen completion authority, first-attempt gate, valid fabrication, failed/export-crash outcomes, actual scanner participation, replay isolation/conflict, profile races, and Strategy Manager isolation [tests/backtest/test_bau_run_envelope.py:1]

### File List

- `_bmad-output/implementation-artifacts/1-10-extend-coverage-with-eligible-bau-scanner-snapshots.md`
- `app/agents/scanner/scanner_agent.py`
- `app/core/config.py`
- `app/orchestration/orchestrator.py`
- `app/repositories/backtest_repo.py`
- `app/services/backtest/bau_capture_coordinator.py`
- `app/services/backtest/bau_run_envelope.py`
- `app/services/backtest/bau_snapshot_promotion.py`
- `app/services/backtest/observed_bau_record_builder.py`
- `app/services/backtest/snapshot_profile.py`
- `app/services/backtest/source_manifest.py`
- `tests/backtest/test_bau_run_envelope.py`

## Change Log

- 2026-08-13: Created implementation-ready Story 1.10 context.
- 2026-08-13: Expanded scope to capture immutable BAU month-end evidence during the owning scanner run before promotion.
- 2026-08-13: Began implementation of the canonical BAU capture and promotion boundary.
- 2026-08-13: Widened after adversarial review: Story 1.10 now explicitly owns scanner-bound raw evidence capture, a separate observed-BAU builder, durable capture replay/ownership validation, warning-only scanner isolation, and recoverable evidence-retention reconciliation.
- 2026-08-14: Rewritten around one scanner-owned raw-evidence and durable run-envelope boundary; prior post-analysis reconstruction/capture approach is superseded and must not be merged.
- 2026-08-14: Adversarial re-review completed; added SQLite run authority, explicit roster participation, semantic replay idempotence, complete promotion validation, truthful provenance, bounded capture concurrency, and end-to-end recovery/fabrication tests.
