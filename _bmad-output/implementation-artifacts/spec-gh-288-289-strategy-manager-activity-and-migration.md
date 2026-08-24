---
title: 'Repair Strategy Manager legacy schema startup and Bootstrap activity rendering'
type: 'bugfix'
created: '2026-08-24'
status: 'done'
baseline_revision: '4c6b086e'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [multiple-goals]
---

<intent-contract>

## Intent

**Problem:** Existing `backtest.db` files can fail worker startup when schema indexes reference newly introduced `strategy_runs` columns before their migrations run. Separately, Bootstrap setup redirects to an Activity URL that always returns 404, leaving users unable to see setup progress or failure recovery information.

**Approach:** Preserve the already-correct migration-before-dependent-index ordering and make the regression fixture explicitly exercise the old `strategy_runs` shape. Add Bootstrap to the shared activity context/template flow with stage progress, failure details, actions, and versioned polling consistent with the existing activity contracts.

## Boundaries & Constraints

**Always:** Keep legacy V1 rows readable; create dependent indexes/triggers only after all referenced columns exist; use the existing job status/version and terminal-state contracts; keep Bootstrap rendering free of initialization/backtest-only run fields; sanitize displayed failure detail through existing template behavior.

**Block If:** Bootstrap activity data requires a new persistence contract or a user-facing recovery action not represented by existing `StrategyJobService.legal_actions`.

**Never:** Do not change Bootstrap worker semantics, job identity, redirect URLs, schema versions, or unrelated activity types; do not add compatibility aliases for obsolete routes.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Legacy schema | Existing DB lacks migrated `strategy_runs` columns | `ensure_schema()` completes and recreates the dependent partial index/trigger | No worker-startup `OperationalError` |
| Bootstrap running | Bootstrap job has current stage and status version | Activity renders status, stage, and an htmx poll URL | Polling stops when terminal |
| Bootstrap failed | Bootstrap job has failure code/detail | Activity renders failed stage/reason and legal recovery action(s) | No exception or 404 |
| Bootstrap terminal | Complete or cancelled Bootstrap job | Activity renders final state without polling | No periodic request attributes |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py` -- legacy column migrations and dependent `strategy_runs` index/trigger creation.
- `app/api/routes/strategy_manager.py` -- generic activity template selection and context assembly.
- `app/api/templates/_bootstrap_activity.html` -- Bootstrap stage/status activity partial.
- `tests/backtest/test_strategy_job_repository.py` -- durable repository migration regression coverage.
- `tests/test_strategy_manager_routes.py` -- generic activity rendering and polling route coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py` and `tests/backtest/test_strategy_job_repository.py` -- preserve/verify migration ordering and explicitly assert a pre-migration `strategy_runs` shape upgrades successfully.
- [x] `app/api/routes/strategy_manager.py` -- map `StrategyJobType.BOOTSTRAP` to its activity template and load `repo.bootstrap_run(job_id)` in `_activity_context`.
- [x] `app/api/templates/_bootstrap_activity.html` -- render Bootstrap status, current stage, failure detail, legal actions, and versioned polling only while non-terminal.
- [x] `tests/test_strategy_manager_routes.py` -- cover Bootstrap running, complete, failed, and same-version polling behavior.

**Acceptance Criteria:**
- Given a database whose `strategy_runs` table lacks the four migrated provenance columns, when `ensure_schema()` runs, then it succeeds and the dependent unique index and contract trigger exist afterward.
- Given a queued or running Bootstrap job, when its Activity URL is requested, then it returns 200 and displays Bootstrap status/stage without requiring an initialization, preparation, or strategy run.
- Given a non-terminal Bootstrap job, when the Activity HTML is rendered, then it polls `/status?last_seen_version=<status_version>` using the existing htmx contract.
- Given a complete, failed, or cancelled Bootstrap job, when its Activity URL is requested, then it returns 200 and contains no polling attributes.
- Given a failed Bootstrap job, when its Activity is rendered, then the failure detail and supported legal recovery action are visible.
- Given an Activity status request with a version equal to or newer than the job version, when it is received, then the route returns 204 with an empty body.

### Review Findings

- [x] [Review][Patch] Do not advertise cancellation during Bootstrap profile activation [app/repositories/backtest_repo.py:4129]
- [x] [Review][Patch] Confirm Bootstrap cancellation/deletion and provide a setup recovery destination after deletion [app/api/templates/_bootstrap_activity.html:9]
- [x] [Review][Patch] Cover Bootstrap cancel and delete endpoints, including their guarded success responses [tests/test_strategy_manager_routes.py:464]
- [x] [Review][Patch] Assert the legacy migration recreates the required partial unique index predicate [tests/backtest/test_strategy_job_repository.py:462]
- [x] [Review][Patch] Correct the contradictory targeted-test count in the verification record [_bmad-output/implementation-artifacts/spec-gh-288-289-strategy-manager-activity-and-migration.md:101]
- [x] [Review][Defer] Shared activity polling may display an older response after a newer request and stale-version responses use generic 409 text [app/api/routes/strategy_manager.py:989] — deferred, pre-existing

## Spec Change Log

- 2026-08-24: Implemented legacy schema regression coverage and Bootstrap activity rendering/context, including terminal polling behavior and legal actions.

## Review Triage Log

### 2026-08-24 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 9: (high 0, medium 3, low 6)
- defer: 1: (high 0, medium 1, low 0)
- reject: 8: (high 0, medium 1, low 7)
- addressed_findings:
  - `[medium]` `[patch]` Bootstrap cancel/delete actions did not use the existing htmx fragment contract; added htmx targets/swaps and hid duplicate cancellation after a request.
  - `[medium]` `[patch]` Shared deletion response was initialization-specific for Bootstrap jobs; return setup history markup for Bootstrap deletion.
  - `[medium]` `[patch]` Strengthened migration regression coverage to verify the unique index definition, trigger recreation, and V1 run readability.
  - `[low]` `[patch]` Added Bootstrap status-version markup, failure fallback copy, repository subtype-call assertion, and newer-version polling coverage.
  - `[low]` `[defer]` Destructive Bootstrap actions still lack a dedicated confirmation-modal and end-to-end delete-route test; the existing shared guarded CAS route remains unchanged and the current issue contract is satisfied.
  - `[low]` `[reject]` Dropped findings that required real-database route fixtures, separate failure-code rendering, or duplicated existing terminal/polling and template auto-escaping coverage.

## Design Notes

Bootstrap uses `current_stage` rather than `current_month`, and `BootstrapRunV1` is intentionally only a job-id identity record. The template should therefore use `job.current_stage`, `job.failure_detail`, `actions`, `terminal`, and `job.status_version` directly, matching the minimal preparation activity pattern while providing the Story 4.3 setup outcome information.

## Verification

**Commands:**
- `uv run pytest -q tests/backtest/test_strategy_job_repository.py tests/test_strategy_manager_routes.py` -- passed: 172 tests after review patches.
- `uv run pytest -q` -- passed: 1,931 tests after review patches.
- `uv run ruff check app tests` -- passed.
- `git diff --check` -- passed.

Results: `uv run pytest -q tests/backtest/test_strategy_job_repository.py tests/test_strategy_manager_routes.py` passed (172 tests); `uv run pytest -q` passed (1,931 tests); `uv run ruff check app tests` passed; `git diff --check` passed.
