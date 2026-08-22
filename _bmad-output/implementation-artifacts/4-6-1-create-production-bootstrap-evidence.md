---
baseline_commit: 68d80dace10cdd2698adcf42aabb37e1b27d8e0c
---

# Story 4.6.1: Create Production Bootstrap Evidence

Status: done

## Story

As a portfolio owner,
I want a clean production installation to complete Strategy Manager setup,
so that preparation can begin from a real, immutable active scanner profile.

## Acceptance Criteria

1. A clean Production `bootstrap` job runs the existing qualification suite through `QualificationRunner`; on success its recorded contract is current and usable, and on provider/contract failure it fails closed with the existing sanitized failure taxonomy.
2. After qualification, the same fenced worker captures the required current DataHub S&P 500, TradingView US, and TradingView UK source evidence through `ReconstructionRosterCaptureService`. It commits the immutable roster, aliases, and identities only through the existing repository transaction; conflicting or incomplete source evidence produces no usable profile.
3. Bootstrap constructs, validates, persists, and atomically activates exactly one valid `SnapshotProfileV1` from the captured roster and installed detector/calendar/provider policy identities. No hand-written placeholder profile, identity, or roster is permitted.
4. If a Bootstrap stage fails or cancellation is acknowledged before final activation, its retained audit/captured evidence is not an active profile and cannot enable initialization or configuration. Final activation remains the existing non-cancellable, claim-token/lease-fenced terminal transaction.
5. Fixture mode uses the same composition boundary with explicitly pinned fixture evidence and continues to display “Fixture — not production readiness”; it must not become an implicit Production fallback.
6. A clean-store integration test proves the supported Production composition can reach one active profile using deterministic injected provider adapters/fixtures. Tests also prove qualification failure, roster identity conflict, profile-validation failure, cancellation before activation, and stale worker fencing cannot activate a profile.

## Tasks / Subtasks

- [x] Define the Bootstrap provider composition boundary (AC: 1-5)
  - [x] Add a typed `StrategyProviderBundleV1`/factory at the Strategy Manager composition root; Production and Fixture must be explicit variants, never inferred from missing data.
  - [x] Use the existing `QualificationRunner`, its fixture/probe definitions, `QualificationRecorder`, and `QualificationAvailabilityService`; do not reproduce qualification checks in Bootstrap.
  - [x] Use `ReconstructionRosterCaptureService`, `DataHubRosterSourceAdapter`, `TradingViewRosterSourceAdapter`, and `YFinanceMarketIdentityResolver`; retain the fixed required-source order and source outcome taxonomy.

- [x] Execute real Bootstrap stages from the fenced worker (AC: 1-4)
  - [x] Replace inspection-only `_run_qualification()` and `_capture_roster()` behaviour in `StrategyBootstrapService` with injected runner/capture execution.
  - [x] Construct `SnapshotProfileV1` from the committed roster and real detector/calendar/request-contract identities; validate before persistence.
  - [x] Extend repository APIs only where needed to persist/reuse the profile and perform the final profile activation with job completion/outbox under one fenced transaction.
  - [x] Preserve `StrategyJobService` FIFO, claim token, lease generation, `status_version`, cancellation, notification-outbox and no-op semantics. Do not create a second worker, queue, or lifecycle table.

- [x] Keep failure and fixture boundaries explicit (AC: 1-5)
  - [x] Map `RosterCaptureError` and qualification outcomes to stable `JobFailureCode` values without raw provider data, local paths, or stack traces.
  - [x] Preserve the existing active profile on a failed replacement attempt; never activate partial data.
  - [x] Do not add a paid provider, live valuation FX, SIPP/ISA/`TraderAgent` import, live order path, database-edit UI, or source fallback.

- [x] Prove the supported first-run journey (AC: 1-6)
  - [x] Add a clean-store Production composition integration test with deterministic fake adapters, then verify the worker reaches `complete`, has exactly one valid active profile, and readiness/configuration can read it.
  - [x] Add focused qualification, roster capture, profile-validation, cancellation, stale-token/lease, Fixture-label, and no-partial-profile tests.
  - [x] Run focused Bootstrap/qualification/roster/profile/worker/repository tests, the Backtest regression suite, Ruff, Pyrefly, and `git diff --check`.

### Review Findings

- [x] [Review][Patch] [High] Correct the production probe currencies, quote units, timezones, and sessions so valid yfinance responses can qualify [app/services/backtest/strategy_bootstrap_service.py:236]
- [x] [Review][Patch] [High] Normalize DataHub's documented capitalized CSV headers before passing rows to the strict roster adapter [app/services/backtest/strategy_bootstrap_service.py:241]
- [x] [Review][Patch] [High] Implement an explicit Fixture provider composition and derive the UI label from that selected bundle [app/services/backtest/strategy_bootstrap_service.py:83]
- [x] [Review][Patch] [Medium] Package the qualification fixture as an application resource instead of reading from the test tree [app/services/backtest/strategy_bootstrap_service.py:195]
- [x] [Review][Patch] [High] Compare reusable qualification evidence to the configured runner contract rather than accepting any repository-current digest [app/services/backtest/strategy_bootstrap_service.py:123]
- [x] [Review][Patch] [Medium] Preserve the qualification and roster provider failure taxonomy instead of collapsing outcomes to provider-unavailable or required-data-missing [app/services/backtest/strategy_bootstrap_service.py:127]
- [x] [Review][Patch] [High] Build and persist the yfinance alias evidence required for initialized snapshot members instead of an empty alias manifest [app/services/backtest/strategy_bootstrap_service.py:208]
- [x] [Review][Patch] [High] Verify current qualification, job-bound roster lineage, and profile-activation stage inside the fenced terminal transaction [app/repositories/backtest_repo.py:2563]
- [x] [Review][Patch] [High] Fence roster capture so an expired worker cannot bind evidence later consumed by its replacement [app/services/backtest/strategy_bootstrap_service.py:140]
- [x] [Review][Patch] [High] Acknowledge cancellation before activation and safely handle terminal activation conflicts without leaving the job running [app/services/backtest/worker.py:261]
- [x] [Review][Patch] [High] Validate compatibility and captured-roster identity before treating a concurrently active profile as successful [app/services/backtest/strategy_bootstrap_service.py:156]
- [x] [Review][Patch] [Medium] Exercise the clean-store journey through the real injectable Bootstrap worker and verify readiness/configuration [tests/backtest/test_strategy_bootstrap_service.py:274]
- [x] [Review][Patch] [Medium] Add the required roster-conflict, profile-validation, cancellation, stale-fence, fixture-boundary, and no-partial-profile tests [tests/backtest/test_strategy_bootstrap_service.py:296]
- [x] [Review][Patch] [Medium] Fix the focused Pyrefly error and correct the checked verification/completion claim [tests/backtest/test_strategy_bootstrap_service.py:202]
- [x] [Review][Patch] [High] Repair the malformed GitHub BMAD tracking YAML [_bmad-output/implementation-artifacts/github-bmad-tracking.yaml:36]

## Dev Notes

### Scope and dependency boundaries

- This is a corrective prerequisite to Story 4.6, tracked by GitHub #279. It repairs Story 4.3’s promised first-run success path; it does not implement preparation, V2 manifests, selected-universe run identity, execution, results, or comparison.
- Reuse the existing evidence primitives. The current `StrategyBootstrapService` merely verifies rows that already exist and cannot activate a clean Production database; that is the defect this story closes.
- A compatible already-active profile remains a verified no-op. Durable submission-key retry behaviour belongs to Story 4.6.2, although this work must not make that later addition harder.

### Existing implementation to reuse and preserve

- UPDATE `app/services/backtest/strategy_bootstrap_service.py`: keep the route/worker entry point, but inject/run real qualification, capture, profile construction, and activation.
- UPDATE `app/services/backtest/worker.py`, `strategy_job.py`, `strategy_job_service.py`, `app/api/dependencies.py`, and `app/repositories/backtest_repo.py` only through their existing fenced-job and repository ownership patterns.
- REUSE `historical_data_qualification.py` (`QualificationRunner`), `reconstruction_roster.py` (`ReconstructionRosterCaptureService` and adapters), `snapshot_profile.py` (`SnapshotProfileV1`), `security_identity.py`, `TradingCalendar`, detector source manifests, and repository `commit_roster_capture()` / `activate_snapshot_profile()`.
- UPDATE `tests/backtest/test_strategy_bootstrap_service.py`; extend the adjacent qualification, roster, snapshot-profile, worker and repository suites instead of seeding `{}` rows as proof of a production setup.

### Guardrails

- AD-28 requires one final repository-owned transaction to verify claimed job/evidence, insert-or-reuse the profile, activate it, complete the job, and write the notification outbox. Do not split those durable writes across services.
- AD-22 permits only pinned yfinance and exchange-calendar mechanics. Provider unavailability, malformed/incomplete data and identity ambiguity fail closed; no Stooq/MCP/paid fallback.
- `SnapshotProfileV1` requires the exact detector set, closed provenance vocabulary, roster/alias/identity policy and calendar digest. Compute its content hash from the validated canonical model; do not use a test-only hash in Production.
- Production source calls must remain bounded and retry only under the established adapter policy. The release-gating clean journey must use deterministic adapters, while a real live-provider smoke remains optional/non-gating.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Story 4.6.1 and Story 4.6]
- [Source: GitHub issue #279 — required outcome]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-4, AD-22, AD-28, AD-29, AD-31]
- [Source: `_bmad-output/implementation-artifacts/spec-4-3-4-4-4-5-bootstrap-readiness-universe-selection.md` — PR #278 follow-up code review]

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Replaced inspection-only Bootstrap stages with explicit free-source provider composition, durable qualification/capture, and an authoritative SnapshotProfileV1.
- Added a repository-owned fenced final transaction which inserts/reuses the profile, activates it, completes the Bootstrap job, and writes its notification outbox together.
- Added deterministic clean-store evidence coverage; the full repository regression suite passed (1,867 tests).
- Resolved all 15 adversarial review findings; final validation passed 802 Backtest tests and 1,877 repository tests. Story-scoped Ruff and Pyrefly are clean; repository-wide checks retain unrelated pre-existing lint/type findings outside this story.

### File List

- app/services/backtest/strategy_bootstrap_service.py
- app/services/backtest/reconstruction_roster.py
- app/services/backtest/fixtures/market_mechanics_v1.json
- app/services/backtest/worker.py
- app/repositories/backtest_repo.py
- tests/backtest/test_backtest_worker.py
- tests/backtest/test_strategy_bootstrap_service.py
- _bmad-output/implementation-artifacts/sprint-status.yaml
- _bmad-output/implementation-artifacts/github-bmad-tracking.yaml

## Change Log

- 2026-08-22: Created implementation-ready corrective Bootstrap context for GitHub #279.
- 2026-08-22: Implemented production Bootstrap evidence capture and atomic activation; ready for review.
- 2026-08-22: Applied all 15 code-review patches and completed Story 4.6.1.
