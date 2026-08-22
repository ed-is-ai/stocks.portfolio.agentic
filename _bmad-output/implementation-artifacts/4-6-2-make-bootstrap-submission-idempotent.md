# Story 4.6.2: Make Bootstrap Submission Idempotent

Status: done

## Story

As a portfolio owner,
I want a repeated setup submission to return its original activity,
so that refreshes, retries, and double-clicks cannot make setup confusing or duplicate work.

## Acceptance Criteria

1. The setup form submits one non-empty Bootstrap idempotency key. Retries of the same submission/key return the original Bootstrap job, including while it is queued, running, or terminal, without creating another job or returning a conflict page.
2. The Bootstrap idempotency key and the immutable submission identity it protects are durably stored in `BACKTEST_DB` under a database uniqueness constraint and one `BEGIN IMMEDIATE` transaction with the job, subtype row, and notification outbox.
3. Concurrent same-key calls return exactly one persisted Bootstrap job. Reusing a key for a materially different supported Bootstrap submission fails with a stable safe conflict; a genuinely different submission while setup is active retains the current conflict/no-op policy.
4. The setup POST remains guarded by `require_local_or_token`; GET/refresh/polling remains non-mutating. The response redirects to the original Activity URL after accepted creation or replay, and does not expose idempotency internals.
5. Existing initialization, preparation, Backtest launch and restart idempotency contracts remain unchanged. The feature preserves active-profile compatible no-op behaviour and all claim/lease/CAS lifecycle semantics.

## Tasks / Subtasks

- [x] Add a typed Bootstrap submission contract (AC: 1-3)
  - [x] Introduce `BootstrapSubmissionV1`/result models alongside existing lifecycle contracts with a bounded non-blank key and canonical submission digest.
  - [x] Pass the key from `_strategy_setup.html` through the route, `StrategyBootstrapService.start_setup()`, `StrategyJobService.enqueue_bootstrap()`, and repository API. Generate once per rendered form; preserve it on a correctable response.

- [x] Persist and replay atomically (AC: 1-3)
  - [x] Add a purpose-specific Bootstrap idempotency table/constraint following `backtest_submission_idempotency` and restart-action patterns; it must reference exactly one Bootstrap job and canonical submission digest.
  - [x] In the repository transaction, read existing key before creating a job; identical replay returns the original loaded job, mismatched replay fails safely, and a race resolves from the durable winner.
  - [x] Keep `strategy_jobs`/`bootstrap_runs` creation and notification-outbox writing atomic; never use an in-memory lock or response cache as the authority.

- [x] Preserve UX and lifecycle boundaries (AC: 4-5)
  - [x] Render a hidden key in the setup confirmation form; do not make it user-editable or display it in diagnostics/notifications.
  - [x] Ensure duplicate requests yield the durable Activity redirect/fragment rather than a 422 conflict template.
  - [x] Keep no-op for an already compatible active profile distinct from a replay of a previously submitted setup request.

- [x] Verify races and regressions (AC: 1-5)
  - [x] Add repository tests for replay, mismatched key, concurrent same-key writers, transaction rollback, loaded job/subtype/outbox consistency, and persistence after reopen.
  - [x] Add setup-route tests for form key, repeat/double POST, refresh/retry redirect, unauthorised POST, and active-profile no-op.
  - [x] Run focused job repository/service/recovery/setup-route tests plus full Backtest tests, Ruff, Pyrefly, and `git diff --check`.

### Review Findings

- [x] [Review][Patch] Make exact replay, active-profile no-op, and new enqueue one repository-owned `BEGIN IMMEDIATE` decision so neither an exact retry nor a genuinely new request can race profile activation [app/repositories/backtest_repo.py:1753]
- [x] [Review][Patch] Make `bootstrap_enqueue_actions` immutable and enforce its Bootstrap job/subtype invariant on every persisted state, not only insert [app/repositories/backtest_repo.py:960]
- [x] [Review][Patch] Return a stable safe outcome when a retained Bootstrap key is replayed after its failed/cancelled activity was deleted, instead of raising an integrity error [app/repositories/backtest_repo.py:1868]
- [x] [Review][Patch] Add the specified typed Bootstrap enqueue result contract and use it through repository and job-service boundaries [app/services/backtest/strategy_job.py:514]
- [x] [Review][Patch] Add synchronized concurrent service-level coverage for same-key replay and profile-activation/no-op races; the current executor test does not guarantee overlap [tests/backtest/test_strategy_job_repository.py:419]
- [x] [Review][Patch] Strengthen route/service acceptance tests for divergent preflight replay, authorized/unauthorized mutation boundaries, non-mutating GET/refresh, and real durable replay behavior [tests/backtest/test_strategy_setup_routes.py:143]
- [x] [Review][Patch] Reconcile the story's verification counts and record the final reproducible commands/results after review fixes [4-6-2-make-bootstrap-submission-idempotent.md:70]
- [x] [Review][Defer] Validate Bootstrap parent-job type, terminal/deleted state, and lineage semantics before accepting a supplied parent [app/repositories/backtest_repo.py:1798] — deferred, pre-existing

## Dev Notes

### Scope and dependency boundaries

- This is GitHub #280. It fixes the missing durable key in the Bootstrap POST path identified in PR #278. It does not implement provider qualification/roster/profile composition (4.6.1) or V2 preparation identity (4.6.3).
- Existing Backtest submission and restart idempotency are the implementation precedent. Bootstrap has no request payload today; define a canonical minimal submission identity rather than silently treating all keys as interchangeable.

### Existing implementation to reuse and preserve

- UPDATE `_strategy_setup.html`, `app/api/routes/strategy_manager.py`, `strategy_bootstrap_service.py`, `strategy_job_service.py`, `strategy_job.py`, `backtest_repo.py`, and focused tests.
- REUSE `BacktestSubmissionV1.idempotency_key`, `backtest_submission_idempotency`, `strategy_job_restart_actions`, `create_backtest_job()` and restart repository patterns for transaction/race semantics.
- `create_bootstrap_job()` currently delegates to `_create_stage_job()` with no key; evolve this path rather than building a parallel setup queue.

### Guardrails

- The database, not HTML or process memory, is the idempotency authority. The durable replay lookup and job creation must run under the same SQLite transaction.
- Never turn a new Bootstrap request into a replay solely because another Bootstrap is queued/running; distinguish exact key replay, compatible active-profile no-op, and a genuine competing request.
- Never log/render keys, raw exceptions, database paths, or stack traces. Preserve `require_local_or_token`, HTMX/non-HTMX response conventions, and one `strategy_jobs` ledger.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Story 4.6.2 and Story 4.3 acceptance]
- [Source: GitHub issue #280 — required outcome]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-4, AD-28]
- [Source: `app/repositories/backtest_repo.py` — Backtest/restart idempotency transactions]
- [Source: `_bmad-output/implementation-artifacts/spec-4-3-4-4-4-5-bootstrap-readiness-universe-selection.md` — PR #278 follow-up code review]

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Added `BootstrapSubmissionV1` and a versioned canonical digest that excludes the opaque form key.
- Added `bootstrap_enqueue_actions`; its same-key lookup, Bootstrap job/subtype, binding, and notification outbox write share one `BEGIN IMMEDIATE` transaction.
- Setup GET now renders an opaque hidden key and exact POST retries redirect to the original durable activity.
- Review fixes made Bootstrap enqueue a single atomic repository decision, added an explicit typed result, protected immutable replay bindings, and made deleted-activity replay fail safely.
- Review-focused suite: `uv run pytest tests/backtest/test_strategy_job_repository.py tests/backtest/test_strategy_job_service.py tests/backtest/test_strategy_bootstrap_service.py tests/backtest/test_strategy_setup_routes.py tests/backtest/test_strategy_job_recovery.py tests/backtest/test_backtest_worker.py tests/backtest/test_strategy_readiness_service.py` — 185 passed.
- Backtest regression suite: `uv run pytest tests/backtest` — 821 passed, 2 warnings.
- Touched-scope `uv run ruff check ...` passed; application-scope `uv run pyrefly check ...` reported 0 errors.

### File List

- app/services/backtest/strategy_job.py
- app/repositories/backtest_repo.py
- app/services/backtest/strategy_job_service.py
- app/services/backtest/strategy_bootstrap_service.py
- app/api/routes/strategy_manager.py
- app/api/templates/_strategy_setup.html
- tests/backtest/test_strategy_job_repository.py
- tests/backtest/test_strategy_job_service.py
- tests/backtest/test_strategy_bootstrap_service.py
- tests/backtest/test_strategy_setup_routes.py
- tests/backtest/test_strategy_job_recovery.py
- tests/backtest/test_backtest_worker.py
- tests/backtest/test_strategy_readiness_service.py

## Change Log

- 2026-08-22: Created implementation-ready Bootstrap idempotency context for GitHub #280.
- 2026-08-22: Implemented durable Bootstrap submission replay and submitted for review.
- 2026-08-22: Applied all code-review patches, completed regression verification, and marked the story done.
