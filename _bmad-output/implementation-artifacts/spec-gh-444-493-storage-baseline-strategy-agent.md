---
title: 'GH-444 prerequisite storage baseline and GH-493 Strategy Manager agent'
type: feature
created: '2026-09-08'
status: done
final_revision: 5ef0276a15e387811560b1c1db6db78fe7161a7a
baseline_revision: add484a2bee51b080687cd811cf3d526890e3cb1
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [multiple-goals, oversized]
github_issues: [444, 535, 493]
---

<intent-contract>

## Intent

**Problem:** Evidence storage has no repeatable operator benchmark or consistent SQLite contention diagnostics. Strategy Manager's primary job submission bypasses the domain Agent entry-point convention.

**Approach:** This auto-development iteration implements the first dependency-ordered story of epic #444 (#535), alongside #493. Establish repeatable offline measurements and bounded evidence-database contention handling; wrap the existing validated launch service with a synchronous StrategyManagerAgent that returns its existing durable job handle. Track subsequent epic stories #536–#540 separately; this iteration makes no whole-epic completion or performance-target claim.

## Boundaries & Constraints

**Always:** Preserve evidence identity, immutable references, repository ownership, financial behavior, validation errors, FIFO dispatch, claim tokens, worker leases and cancellation. Reuse existing models/services. Benchmark only consistent offline copies selected by the operator. Preserve original SQLite error class/code/name in safe persisted diagnostics, excluding raw SQL, parameters and sensitive paths.

**Block If:** Offline benchmark inputs cannot be obtained consistently, or an acceptance failure cannot be resolved without changing evidence/job semantics.

**Never:** Rewrite worker execution into Agent.run; add an async Agent base; fetch provider data during benchmarks; migrate/delete live evidence; imply a synthetic or application-cold measurement proves the epic's real-data cold/warm targets.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Launch | Valid existing command | Delegate once; return unchanged enqueue result/job | Existing service owns validation |
| Invalid launch | Rejected parameters or universe | No additional enqueue; existing HTTP field error | Preserve exception |
| Contention | Active reader plus writer | WAL permits short writer transaction; reader retains snapshot | Bounded busy timeout for competing writers |
| Exhausted SQLite wait | Locked evidence DB | Original SQLite type/code/name and safe operation context survive | Do not replay an arbitrary partially completed transaction |
| Benchmark | Explicit offline DB files | JSON sizes, counts, repeatable component timing and sample metadata | Fail clearly on missing/invalid input; open read-only |

</intent-contract>

## Code Map

- `app/agents/base.py`, `app/agents/price_backfill/price_backfill_agent.py` — existing Pydantic Agent pattern.
- `app/api/dependencies.py`, `app/api/routes/strategy_manager.py` — service composition and primary configuration POST.
- `app/services/backtest/backtest_launch_service.py` — existing validated launch command and preparation/backtest results.
- `app/repositories/db.py`, `app/repositories/historical_price_repo.py`, `app/repositories/backtest_repo.py` — connection/session ownership; Backtest already uses WAL and a busy timeout.
- `app/services/backtest/worker.py`, `app/services/backtest/historical_initialization_engine.py`, `app/services/backtest/backtest_engine.py` — exception-to-durable-failure boundaries.
- `tests/backtest/` — evidence, launch, coverage and lifecycle regression tests.

## Tasks & Acceptance

**Execution:**
- [x] `app/agents/strategy_manager/strategy_manager_agent.py`, `app/agents/strategy_manager/__init__.py` — add typed dispatch-only Agent delegating to BacktestLaunchService.launch.
- [x] `app/api/dependencies.py`, `app/api/routes/strategy_manager.py` — compose Agent through existing launch dependency; route primary launch via run, retaining discovery/form behavior.
- [x] `app/repositories/db.py`, `app/repositories/historical_price_repo.py`, `app/repositories/backtest_repo.py` — reuse an explicit bounded evidence connection policy, enable historical WAL at schema initialization, and document transaction/checkpoint decisions without broad retry wrappers.
- [x] `app/services/backtest/worker.py` and engine failure boundaries — preserve safe SQLite diagnostics in persisted failure detail, including chained SQLite causes where applicable.
- [x] `app/repositories/evidence_benchmark.py`, `scripts/benchmark_evidence_databases.py` — repository-owned read-only benchmark with size/count metrics and reproducible Result component timings, explicit repetitions/profile/run arguments and honest cache-state labels.
- [x] `tests/backtest/test_strategy_manager_agent.py`, existing route tests, `tests/backtest/test_evidence_database_policy.py`, `tests/backtest/test_evidence_benchmark.py` — verify delegation/error identity, HTTP routing, reader snapshot/writer coexistence, timeout exhaustion, diagnostic persistence, and read-only benchmark output.
- [x] `docs/evidence-database-operations.md` — document Agent boundary, busy/journal/checkpoint policy, consistent backup procedure, repeatable benchmark method, observed results and remaining epic gates.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` and tracking artifacts — link all six epic child issues; record this iteration and verification evidence.

**Acceptance Criteria:**
- Given a valid primary HTTP backtest submission, when dispatched, then StrategyManagerAgent receives the existing typed command and returns the same job result through the unchanged service.
- Given a rejected submission, when processed, then existing validation responses and no-extra-enqueue behavior remain unchanged.
- Given initialized evidence databases, when concurrent readers and writers run, then readers keep consistent snapshots and writers either succeed or exhaust a documented bounded wait with actionable SQLite diagnostics.
- Given a SQLite error at a durable worker failure boundary, when persisted, then safe original exception class/code/name and operation context are present without raw sensitive error text.
- Given the operator-approved main database, when a consistent offline copy is benchmarked, then size/count/timing output and methodology are recorded with explicit limits; the source is unchanged.
- Given the changed code, when repository tests and quality checks run, then regressions introduced by this work are resolved and pre-existing/environment failures are distinguished.

## Spec Change Log

## Review Triage Log

### 2026-09-08 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 3, low 0)
- defer: 8: (high 4, medium 4, low 0)
- reject: 1: (high 0, medium 0, low 1)
- addressed_findings:
  - `[medium]` `[patch]` Resolve inventory paths before reading both the database and its WAL; regression test covers symlinked WAL inputs.
  - `[medium]` `[patch]` Report actual executed warm repetitions separately from requested repetitions when Result integrity fails before warm-up.
  - `[medium]` `[patch]` Require code revision and offline snapshot identifiers in CLI metadata. Attach known execution provenance transparently to the existing baseline artifact; timings are unchanged.

Both independent hunters reviewed the complete diff from the original baseline, including incoming main changes. Deferred findings concern existing inability to persist failure into a still-locked database and incoming cash reconstruction, alias truncation, marker state and negative-cache behavior. No new caller mutation of the alias cache was demonstrated, so that speculative finding was rejected. Existing alert-agent type errors are recorded separately in deferred work.

## Design Notes

The agent boundary is dispatch-only and synchronous: wrap BacktestLaunchService.launch, which already selects preparation versus backtest. Bootstrap, initialization, polling and worker execution retain their existing contracts. This leaves the architecture's stateful engine outside Agent.run while giving primary job dispatch an Agent entry point. Reuse existing dependency overrides; avoid eagerly constructing unrelated services.

Epic #444's first story establishes measurements before schema format and performance decisions. Subsequent work remains explicitly tracked under #536–#540. Main's database files are symlinks to the original checkout's data; copy via SQLite backup from read-only source connections, never via live-file copying.

## Verification

- `python -m pytest tests/backtest -q` — evidence, dispatch, worker and route regression checks.
- `python -m pytest -q` — repository suite; record external/environment failures separately.
- `ruff check` on changed Python files and `git diff --check` — no introduced lint/whitespace errors.
- Offline benchmark on main's database backup — JSON artifact with reproducible size/count/latency metadata; no provider calls.

## Auto Run Result

Implemented GH-493 and the first ordered GH-444 story, GH-535, in a new worktree from main. Created child issues GH-535 through GH-540; GH-444 remains in progress. At the operator's request, fetched and merged main through `85e0d48c`, preserving the implementation in checkpoint `925b0f0` and merge `1d538ef`.

### Changed files

- `app/agents/strategy_manager/__init__.py`, `strategy_manager_agent.py`: typed primary-launch Agent and package export.
- `app/api/dependencies.py`, `app/api/routes/strategy_manager.py`: FastAPI Agent composition and primary POST delegation.
- `app/repositories/db.py`: shared evidence connection policy and safe SQLite failure details.
- `app/repositories/backtest_repo.py`, `app/repositories/historical_price_repo.py`: apply shared connection policy and historical WAL setup.
- `app/services/backtest/worker.py`, `historical_initialization_engine.py`: retain SQLite identity at durable failure boundaries.
- `app/repositories/evidence_benchmark.py`, `scripts/benchmark_evidence_databases.py`: read-only inventory and honest component measurements with provenance.
- `tests/backtest/test_strategy_manager_agent.py`, `tests/test_strategy_manager_routes.py`: Agent delegation, both result types, errors and HTTP dispatch.
- `tests/backtest/test_evidence_database_policy.py`, `test_historical_initialization_engine.py`: real contention, rollback/recovery and safe persisted diagnostics.
- `tests/backtest/test_evidence_benchmark.py`: read-only inputs, sampling, integrity failure, symlink WAL and provenance checks.
- `docs/evidence-database-operations.md`: concurrency, backup, benchmark method/results and Agent boundary.
- BMAD feature/context/spec, sprint and GitHub tracking, baseline JSON and backup metadata: scope, evidence and follow-up work.

### Verification

- Merged-branch full suite: 3,022 passed; four browser setup errors because the sandbox denied localhost binding. Those four passed on an authorized rerun (19.52 s).
- Focused final route/policy/benchmark suite: 190 passed (5.21 s).
- Post-review benchmark regression suite: 4 passed (3.57 s), including the two new edge cases.
- Changed-file Ruff and whitespace checks passed.
- Pyrefly with the actual project interpreter: 33 errors, all in main's unchanged `app/agents/alert/alert_agent.py`; no introduced type errors remain. This is an existing quality-check failure, not a green global type check.
- Offline main database baseline: 27,366,866,944 historical bytes, 6,111 revisions, 44,603,636 observations; 10,550,337,536 Backtest bytes. Source access was read-only SQLite backup. No live database migration or deletion occurred.
- The 241-month profile is present but coverage/member checks reject its detector authority. The report records rejection durations; successful Result-rendering acceptance remains unproven and is tracked under GH-539.

### Residual work and review

Three localized benchmark-reporting patches applied; eight pre-existing/merge-added findings deferred; one speculative finding rejected. Follow-up review is not required for these localized fixes, which have regression checks. The existing locked-ledger failure-persistence ceiling remains documented: if even the failure write cannot acquire the database, lease recovery may lose the original detail. A separate durable fallback/recovery design is outside this first-story change.

GH-536–GH-540 remain backlog: compact schema, migration/rollback, GC, bounded Result reads and rollout. The auto-development iteration is complete; the whole storage epic is not. Changes are committed locally, not pushed.
