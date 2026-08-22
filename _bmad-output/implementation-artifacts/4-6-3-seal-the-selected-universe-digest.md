# Story 4.6.3: Seal the Selected-Universe Digest

Status: ready-for-dev

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

- [ ] Make selection a typed preparation input (AC: 1-4)
  - [ ] Complete route/launch handoff so it constructs `RunUniverseSelectionV1` after roster/staleness validation; keep host-bound strategy parameter binding separate from run identity.
  - [ ] Extend preparation submission/run models and the `run_preparations` schema/repository contract with profile hash, activation sequence, universe schema/mapping, canonical IDs JSON and `run_universe_digest`.
  - [ ] Retain one normalizer: `canonical_run_universe()` and `run_universe_digest()`; do not reimplement sorting, hashing, or a maximum selection size.

- [ ] Define V2 manifest/run provenance (AC: 2-5)
  - [ ] Add `RunInputManifestV2` and version-dispatched readers/builders alongside `RunInputManifestV1`; preserve V1 serialization, digest and execution-contract methods exactly.
  - [ ] Carry V2 digest into initial Backtest job/run/result fields and eventual comparison predicate. Use nullable/required-by-version constraints, not an ambiguous unversioned optional contract.
  - [ ] Ensure profile activation sequence is validated and stored where required but is not accidentally added to the AD-30 universe-digest payload.

- [ ] Fail closed at the sealing boundary (AC: 3-4)
  - [ ] Verify selected IDs against the immutable active-profile roster and declared Strategy universe before dispatch and again in the final seal-and-create transaction.
  - [ ] Reject a mismatch between canonical IDs, stored JSON, digest, profile/mapping/schema and manifest evidence with stable sanitized integrity reasons.
  - [ ] Do not fetch evidence, create a Backtest, or mutate a V1 record for invalid selection state.

- [ ] Prove provenance and compatibility (AC: 1-6)
  - [ ] Add unit tests for typed selection construction and exact digest payload semantics; include reordered/duplicate input and a changed-membership negative case.
  - [ ] Add repository/manifest/preparation integration tests for durable readback, stale activation, tampered digest/ID mapping, selected-only evidence scope and atomic no-child-on-failure.
  - [ ] Add V1 fixture/replay/comparison regression tests and version-dispatch/cross-version rejection tests.
  - [ ] Run focused universe, manifest, preparation, job/run/result/comparison tests plus full Backtest regressions, Ruff, Pyrefly, and `git diff --check`.

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

### Debug Log References

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.

### File List

## Change Log

- 2026-08-22: Created implementation-ready selected-universe sealing context for GitHub #281.
