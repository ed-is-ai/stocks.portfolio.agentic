# Story 4.6.3: Seal the Selected-Universe Digest

Status: done

## Story

As a portfolio owner,
I want my selected securities to have a durable, order-independent Run identity,
so that preparation, replay, and comparison use exactly the evidence I chose.

## Acceptance Criteria

1. At the host boundary, a valid roster-backed selection becomes one `RunUniverseSelectionV1` with the active `profile_hash`, `activation_seq`, sorted immutable security IDs, and `run_universe_digest` computed by the existing `run_universe_digest()` canonicalizer.
2. The typed selection is persisted on the V2 `preparation` contract and carried unchanged into `RunInputManifestV2`, the initial V2 Backtest run, result provenance, and V2 comparison identity. It is never injected as an undeclared generic Strategy parameter.
3. The V2 manifest content digest includes the universe schema/mapping, sorted selected IDs and digest, profile identity/activation sequence, plus existing required preparation identities. Two equal selected sets in different UI orders have identical V2 universe/manifest identity; changed membership, profile, mapping, or evidence cannot be treated as equal.
4. Preparation revalidates profile hash, activation sequence, declared universe mapping, canonical selected IDs, and digest immediately before sealing. Empty, malformed, out-of-profile, tampered, or mismatched selection evidence fails closed before Backtest creation.
5. V1 Run-input manifests, V1 Runs, V1 Results and their comparison/replay semantics remain byte-for-byte and behaviourally unchanged. Schema-version dispatch rejects cross-version comparison rather than retrofitting a selected-universe rule onto V1.
6. End-to-end tests demonstrate selected-only evidence preparation identity, order independence, tamper rejection, stale-profile rejection, persistence/readback, and V1 compatibility.

## Tasks / Subtasks

- [x] Make selection a typed preparation input (AC: 1-4)
  - [x] Complete route/launch handoff so it constructs `RunUniverseSelectionV1` after roster/staleness validation; keep host-bound strategy parameter binding separate from run identity.
  - [x] Extend preparation submission/run models and the `run_preparations` schema/repository contract with profile hash, activation sequence, universe schema/mapping, canonical IDs JSON and `run_universe_digest`.
  - [x] Retain one normalizer: `canonical_run_universe()` and `run_universe_digest()`; do not reimplement sorting, hashing, or a maximum selection size.

- [x] Define V2 manifest/run provenance (AC: 2-5)
  - [x] Add `RunInputManifestV2` and version-dispatched readers/builders alongside `RunInputManifestV1`; preserve V1 serialization, digest and execution-contract methods exactly.
  - [x] Carry V2 digest into initial Backtest job/run/result fields and eventual comparison predicate. Use nullable/required-by-version constraints, not an ambiguous unversioned optional contract.
  - [x] Ensure profile activation sequence is validated and stored where required but is not accidentally added to the AD-30 universe-digest payload.

- [x] Fail closed at the sealing boundary (AC: 3-4)
  - [x] Verify selected IDs against the immutable active-profile roster and declared Strategy universe before dispatch and again in the final seal-and-create transaction.
  - [x] Reject a mismatch between canonical IDs, stored JSON, digest, profile/mapping/schema and manifest evidence with stable sanitized integrity reasons.
  - [x] Do not fetch evidence, create a Backtest, or mutate a V1 record for invalid selection state.

- [x] Prove provenance and compatibility (AC: 1-6)
  - [x] Add unit tests for typed selection construction and exact digest payload semantics; include reordered/duplicate input and a changed-membership negative case.
  - [x] Add repository/manifest/preparation integration tests for durable readback, stale activation, tampered digest/ID mapping, selected-only evidence scope and atomic no-child-on-failure.
  - [x] Add V1 fixture/replay/comparison regression tests and version-dispatch/cross-version rejection tests.
  - [x] Run focused universe, manifest, preparation, job/run/result/comparison tests plus full Backtest regressions, Ruff, Pyrefly, and `git diff --check`.

## Dev Notes

### Scope and dependency boundaries

- This is GitHub #281 and is intentionally coupled to Story 4.6’s V2 preparation work. It establishes the selected-universe identity contract required before Stories 4.7–4.9 can execute, restart, compare, or demonstrate a clean checkout.
- Current route code canonicalizes IDs and binds them into Strategy parameters but does not compute/store a digest. `BacktestLaunchService` validates parameters against the Strategy declaration, so adding a digest as a generic parameter is invalid.
- Story 4.6 owns actual evidence acquisition/preparation. This story establishes its typed input, persistence, manifest, and run identity seams; implementation may land as the first slice of 4.6 but must not ship an unsealed pseudo-V2 path.

### Existing implementation to reuse and preserve

- REUSE `app/services/backtest/run_universe.py` exactly for canonical IDs/digest and the existing `RunUniverseSelectionV1` contract in `strategy_job.py`.
- UPDATE `strategy_manager.py`, `backtest_launch_service.py`, `strategy_job.py`, `strategy_job_service.py`, `run_input_manifest.py`, `canonical_manifest.py`, `backtest_repo.py`, worker/readers and relevant tests as V2 work requires.
- REUSE existing V1 `RunInputManifestV1`, `BacktestSubmissionV1`, repository content-addressed manifest handling and comparison architecture; add version dispatch rather than mutating V1 fields/bytes.

### Guardrails

- AD-30 digest is a function of universe schema, mode/mapping, profile hash and sorted selected IDs—never UI order or activation sequence. Do not change `run_universe.py` to compensate for missing persistence.
- AD-31 requires preparation to be selected-universe scoped and atomic at seal-and-create: stale/invalid selection yields no Backtest and no sealed manifest.
- V1 must remain readable/replayable by its existing parser and comparison predicate. No migration/backfill/conversion of V1 manifests or Results.
- No free-text identities, no per-Strategy maximum universe, no live portfolio/order code, paid-provider/MCP fallback, or live FX valuation path.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Stories 4.5, 4.6, 4.7 and 4.8]
- [Source: GitHub issue #281 — required outcome]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-3, AD-4, AD-30, AD-31]
- [Source: `app/services/backtest/run_universe.py` — canonical V1 universe identity]
- [Source: `app/services/backtest/run_input_manifest.py` and `backtest_launch_service.py` — current V1 boundary]

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Implemented the complete selected-universe V2 preparation/seal contract and
  both adversarial review loops, including restart, deletion and navigation edges.
- Verification: 253 focused and 837 full Backtest tests passed; Ruff,
  changed-file Pyrefly, and `git diff --check` passed.
- Review pass 3 tightened closed preparation ranges and lineage-aware
  idempotency, V2 manifest/version and exactly-one-lineage database integrity,
  execution-contract sealing, preparation stage/cancellation boundaries,
  stable missing-evidence taxonomy, V1 typed serialization, and HTMX activity
  polling/child navigation. Direct regressions cover each localized finding.
- Review pass 3 verification: 153 focused and 840 full Backtest tests passed;
  Ruff and `git diff --check` passed. Changed-file Pyrefly still reports nine
  pre-existing test-fixture narrowing errors outside the pass-3 additions.

### File List

- `app/api/routes/strategy_manager.py`
- `app/api/templates/_preparation_activity.html`
- `app/repositories/backtest_repo.py`
- `app/services/backtest/backtest_launch_service.py`
- `app/services/backtest/run_input_manifest.py`
- `app/services/backtest/run_universe.py`
- `app/services/backtest/strategy_job.py`
- `app/services/backtest/strategy_job_service.py`
- `app/services/backtest/worker.py`
- `tests/backtest/test_backtest_worker.py`
- `tests/backtest/test_run_input_manifest.py`
- `tests/backtest/test_run_universe.py`
- `tests/backtest/test_strategy_job_repository.py`
- `tests/backtest/test_universe_selection_routes.py`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`

## Change Log

- 2026-08-22: Created implementation-ready selected-universe sealing context for GitHub #281.
- 2026-08-22: Applied review-pass-3 integrity, lifecycle, compatibility, and
  supported-route provenance fixes with direct regression coverage.
- 2026-08-22: Completed three adversarial review passes; all accepted findings
  are resolved and the full Backtest regression suite is green.
