---
title: 'Record one initial ranked entry selection'
type: 'feature'
created: '2026-08-28'
status: 'done'
baseline_revision: '1d7a21509ef19ff44a2f45f0c8e387b7a9af75f8'
review_loop_iteration: 2
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md'
warnings: [oversized]
github_issue: 369
parent_issue: 366
---

<intent-contract>

## Intent

**Problem:** The engine can persist trades and equity, but a Strategy cannot return one complete, ranked initial-basket decision or explain selected and excluded securities. Repeated `entry_signals` cannot guarantee one-time selection or durable audit evidence.

**Approach:** Add an optional provider-neutral initial-entry-selection capability beside unchanged `StrategyProtocolV1`, invoke and validate it atomically on the first normalized Run session, and persist its canonical decision batch as versioned Result evidence. No production Strategy adopts it in this story.

## Boundaries & Constraints

**Always:** Preserve the three V1 Strategy methods and observable behavior of all six production Strategies; validate the entire selection before pending-order or sink mutation; cover every pinned Run security exactly once; require selected decisions and BUY signals to agree; keep decisions separate from the Trade Log; bind new Results to a versioned canonical digest; preserve historical V1 Result and V1/V2 manifest bytes.

**Block If:** The change requires rewriting an existing manifest/Result, changing comparison eligibility beyond the normal execution-contract digest, or selecting a product ranking/allocation rule.

**Never:** Modify a production Strategy, add Buy and Hold ranking or equal allocation, expose live accounts/repositories/network to Strategy code, encode decisions as fake trade events, or silently accept partial/contradictory batches.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Legacy Strategy | Plain `StrategyProtocolV1` | Existing calls, events, metrics, and V1 Result payload remain equivalent; no selection | No new error |
| Valid selection | Complete canonical batch on first union session | Called once; selected BUYs use existing next-MIC fill path; decisions stage/promote atomically | Ordinary fill/size skips retain existing codes |
| No selections | Every member excluded or eligible-not-selected | Successful decision-bearing Run with zero initial BUYs | No error |
| Malformed selection | Duplicate/missing ID or rank, gap, invalid score/state, wrong session/rule/side, unpinned ID, signal mismatch | No pending mutation and no first-session publish | Stable protocol/integrity error |
| Legacy Result | Existing result has no selection schema/rows | Exact V1 digest rebuild and typed `None` selection | Tampered legacy evidence still fails normally |
| New Result tampering | Header/decision row removed, added, or changed | Retrieval cannot reinterpret it as legacy | `BacktestIntegrityError` |

</intent-contract>

## Code Map

- `app/services/backtest/strategy_protocol.py` -- unchanged V1 seam; add strict selection models, optional protocol, and pure canonical validator.
- `app/services/backtest/backtest_engine.py` -- one-shot first-session dispatch, prepare-before-commit scheduling, selection output, and sink publication.
- `app/services/backtest/run_input_manifest.py` -- bump named engine/protocol/policy versions so new semantics change new execution manifests without schema rewrites.
- `app/services/backtest/worker.py` -- accumulate the optional selection in the full-replace fenced staging sink.
- `app/repositories/backtest_repo.py` -- additive staging/completed schema, atomic promotion, typed retrieval, immutable rows, and V1/V2 Result digest dispatch.
- `tests/backtest/test_strategy_protocol.py` -- model and full-batch validation matrix.
- `tests/backtest/test_backtest_engine.py` -- exactly-once, atomicity, scheduling, and legacy behavior.
- `tests/backtest/test_backtest_worker.py` -- fenced staging/promotion integration.
- `tests/backtest/test_backtest_repo_results.py` -- migration, digest, tamper, idempotency, and legacy compatibility.
- `tests/backtest/test_run_input_manifest.py` and `tests/backtest/test_strategy_runtime_import_boundary.py` -- identity/version and safe optional-provider coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/backtest/strategy_protocol.py` -- add frozen strict `EntrySelectionDecisionV1`/`InitialEntrySelectionV1`, a three-state vocabulary, independent runtime-checkable capability, stable error codes, and a pure validator that detaches/canonicalizes input and enforces unique contiguous ranks, common session/metric/rule semantics, and exact selected-decision/BUY-signal agreement.
- [x] `app/services/backtest/backtest_engine.py` -- detect the optional capability only for the first union session, validate exact pinned-universe coverage before mutation, suppress ordinary first-session entries for capable Strategies, retain later V1 entries, preflight selection scheduling, and publish the selection only with a fully successful session.
- [x] `app/services/backtest/run_input_manifest.py` -- advance the explicit semantic versions/digests for new Runs while leaving manifest schemas and stored bytes unchanged.
- [x] `app/services/backtest/worker.py` -- carry the optional canonical batch through cumulative full-replace staging under existing claim/lease fences.
- [x] `app/repositories/backtest_repo.py` -- add backward-compatible schema upgrade plus selection staging/header/decision persistence, immutable triggers, atomic promotion/retrieval, and explicit `backtest_result.v1` versus selection-bearing `backtest_result.v2` canonical payloads so absence/deletion cannot masquerade as legacy.
- [x] `tests/backtest/test_strategy_protocol.py` and `tests/backtest/test_backtest_engine.py` -- test canonical reorder, all invalid combinations, first-union-session behavior across MICs, empty selection, exact coverage/agreement, no mutation/publish on malformed batches, ordinary skips, and unchanged plain-V1 execution.
- [x] `tests/backtest/test_backtest_worker.py` and `tests/backtest/test_backtest_repo_results.py` -- test migration from legacy schema, full-replace/idempotent staging, atomic completion, deterministic retrieval, restart equivalence, immutable/tampered/missing/extra rows, and exact legacy V1 digest compatibility.
- [x] `tests/backtest/test_run_input_manifest.py`, `tests/backtest/test_backtest_repo_comparison.py`, `tests/test_strategy_manager_routes.py`, and runtime import-boundary fixtures -- prove new execution identity, comparison/presenter compatibility, and no forbidden imports without changing production Skills.

**Acceptance Criteria:**
- Given any existing production Strategy and pinned Run, when it executes after this story, then its Strategy calls, signals, fills, Trade Log, metrics, and Result payload remain behaviorally equivalent and it has no initial selection evidence.
- Given a valid optional provider, when the first normalized union session is processed, then it is called exactly once, its ordinary first-session `entry_signals` is suppressed, later V1 entry behavior is preserved, and selected signals follow existing next-MIC fills.
- Given a complete selection batch in any input order, when validated repeatedly, then decisions and signals canonicalize identically, cover the pinned universe exactly once, and selected decision/session/rule identities match BUY signals exactly.
- Given any malformed batch or protocol exception, when the first session runs, then no pending order, cash, position, staged decision, or session batch is committed and a stable code identifies the failure.
- Given a valid batch and worker retry/completion, when staging is replaced and promoted, then one immutable selection is atomically tied to the Result and repeated completion creates no duplicates.
- Given an existing Result without selection evidence, when read, compared, or presented, then its exact V1 digest remains valid and the typed selection is absent; given a new selection-bearing Result with any missing/extra/mutated selection evidence, retrieval fails integrity validation.

## Spec Change Log

## Review Triage Log

### 2026-08-28 — Review pass 1

- intent_gap: 0; bad_spec: 0; patch: 2; defer: 0; rejected: 0.
- Bumped the named execution semantic versions to `v3`, so new Run manifests
  record the changed engine/protocol/policy semantics.
- Rebuilt the legacy Result-evidence immutable trigger during schema upgrade,
  so existing databases protect `result_schema_version` too.

### 2026-08-28 — Review pass 2

- intent_gap: 0; bad_spec: 0; patch: 1; defer: 0; rejected: 0.
- Revalidate selection-bearing staging against the pinned V2 Run universe and
  first persisted equity session before it can be promoted; added the
  immutable V2 provenance fixture and malformed-staging regression test.
- Final independent review: no unresolved findings.

## Design Notes

Use a separate selection header plus decision rows rather than Trade Log events. New selection-bearing Results use `backtest_result.v2`; historical rows continue rebuilding the byte-identical V1 payload. `InitialEntrySelectionV1` remains a Strategy-facing pure value, while the engine supplies pinned-universe and first-session context to validation. Preflight all selection instructions before committing pending orders; ordinary business skips remain normal after a valid batch.

## Verification

**Commands:**
- `uv run pytest tests/backtest/test_strategy_protocol.py tests/backtest/test_backtest_engine.py tests/backtest/test_backtest_worker.py tests/backtest/test_backtest_repo_results.py tests/backtest/test_run_input_manifest.py tests/backtest/test_backtest_repo_comparison.py tests/test_strategy_manager_routes.py -q` -- focused contract/lifecycle compatibility passes.
- `uv run pytest skills/rtly-backtest-*/scripts/tests tests/backtest/test_strategy_runtime_import_boundary.py -q` -- all production Skills and the import boundary pass.
- `uv run pytest -q` -- complete repository suite passes.
- `uv run ruff check . && uv run ruff format --check .` -- lint and format pass.
- `uv run pyrefly check app/repositories/backtest_repo.py app/services/backtest/strategy_protocol.py app/services/backtest/backtest_engine.py app/services/backtest/worker.py app/services/backtest/run_input_manifest.py` -- direct changed-module checking passes (the full-project command retains unrelated baseline diagnostics).
- `git diff --check` -- patch is whitespace-clean.
