---
baseline_commit: 535a08f
github_issue: 190
---

# Story 1.7: Run Initialization Through the Durable Job Lifecycle

Status: done

## Story

As a portfolio owner,
I want historical initialization to run durably in the background with explicit monthly progress,
so that I can leave the page and return without losing or duplicating work.

## Acceptance Criteria

1. Given a valid, qualified, non-Ready closed historical interval for one compatible active snapshot profile, submitting Initialize atomically creates exactly one `queued` `strategy_jobs` row and one matching `initialization_runs` row with an immutable FIFO `enqueue_seq`; the caller receives the durable job ID without waiting for reconstruction. A wholly Ready interval returns a typed no-op and creates no job.
2. Claiming work uses one `BEGIN IMMEDIATE` transaction to select the smallest queued `enqueue_seq`, conditionally transition it to `running`, assign a unique `claim_token`, clear any impossible queued-only progress fields, and increment `status_version`. Exactly one `python -m app.services.backtest.worker --job-id <id> --claim-token <token>` subprocess is launched for the claimed job; initialization and later Backtest work share this queue while BAU scanning remains independent.
3. The initialization worker revalidates job type, status, claim token, qualification, pinned profile, requested inclusive range, and immutable initialization configuration before doing work. It processes missing months in ascending calendar order and roster members in stable `security_id` order, sets `current_month` before the month's work starts, reuses verified price/detector/snapshot caches, and commits each complete month through Story 1.6's sole transaction boundary.
4. The first unresolved member or month failure stops the attempt immediately, writes a stable `failure_code`, `failed_month`, and safe human-readable detail, preserves earlier committed shared months, and performs no later month. No partial month becomes Ready and no user-facing source-gap inventory is created.
5. Every progress, cancellation, fallback, and terminal mutation is compare-and-swap guarded by job ID, current status, current `status_version`, and claim token where worker-owned. Every actual mutation increments `status_version`; stale workers and stale service instances cannot mutate a newer or terminal row. Legal stored transitions are only `queued -> running|cancelled|failed` and `running -> complete|failed|cancelled`.
6. Cancelling queued work atomically writes terminal `cancelled`. Cancelling running work records `cancel_requested_at` without inventing another stored status; the worker checks before a month, after its transactional commit, and in final completion. It stops as `cancelled` only at a safe month boundary, while a completion that commits first wins over a late cancellation request.
7. At application startup, every unowned `running` claim is conditionally failed with `worker_interrupted`; queued jobs stay queued and no job auto-replays. Spawn failure, owned-child shutdown, or child exit without a terminal worker write also becomes a conditional `worker_interrupted` failure. A stale child cannot write after reconciliation.
8. When all requested months are committed, the worker rechecks interval readiness and conditionally writes `complete` exactly once with the final ordered snapshot-evidence digest. The job's type, parent and enqueue sequence and the initialization subtype's profile/range/calendar identity remain immutable; the final digest is nullable before completion and write-once at the successful terminal transition. Terminal state remains queryable after process/page restart.

## Tasks / Subtasks

- [x] Define the closed job and initialization contracts (AC: 1-8)
  - [x] Add frozen, strict typed models for job type/status, initialization submission/no-op, job detail, claim, cancellation intent, progress, and failure. Store UTC ISO-8601 instants and canonical `YYYY-MM` labels; keep `cancel_requested_at` as intent rather than a sixth status.
  - [x] Keep stable failure codes closed to `provider_unavailable`, `provider_throttled`, `provider_contract_error`, `required_data_missing`, `identity_ambiguous`, `calendar_error`, `integrity_error`, and `worker_interrupted`; do not leak tracebacks/provider payloads as user copy.

- [x] Extend `BACKTEST_DB` with one race-safe lifecycle ledger (AC: 1, 2, 5-8)
  - [x] Extend `app/repositories/backtest_repo.py` schema with `strategy_jobs` and `initialization_runs` using AD-9 keys/FKs/CHECKs, a unique immutable integer `enqueue_seq`, exactly one matching subtype while live, nullable claim/current-month/cancel/failure/tombstone fields, and monotonic `status_version`.
  - [x] Enforce the v1 cross-process single-running-job invariant in SQLite (for example, a partial unique index), not only with a Python lock.
  - [x] Add repository methods for create/no-op, get/list, FIFO claim, progress, cancel request, worker terminal transition, fallback failure, startup reconciliation, and final readiness verification. Routes/services must never write lifecycle SQL directly.
  - [x] Allocate enqueue order and create job+subtype in the same `BEGIN IMMEDIATE` transaction. Equivalent deliberate submissions may create distinct attempts; any explicit idempotency key handling belongs to Story 1.8 restart actions.
  - [x] Enforce immutable configuration and terminal rows with repository predicates/triggers; normalize expected conflicts to typed lifecycle/integrity errors rather than raw `sqlite3` exceptions.

- [x] Implement `StrategyJobService` and the one-child dispatcher (AC: 1, 2, 5-7)
  - [x] Add `app/services/backtest/strategy_job_service.py` as the sole owner of enqueue, claim, subprocess ownership, cancellation, child-exit fallback, startup reconciliation, and shutdown handling.
  - [x] Launch the exact module command with `sys.executable` and an argv list, never `shell=True`; do not use FastAPI `BackgroundTasks`, an in-memory-only queue, CSV/status sidecars, the BAU pipeline lock, or a second lifecycle representation. Do not attach unread unbounded stdout/stderr pipes that can deadlock a long-running child.
  - [x] Ensure one local Uvicorn process owns at most one Strategy Manager child at a time. Queue polling must not hold a SQLite transaction while reconstruction or a subprocess is running.

- [x] Compose the historical initialization engine (AC: 3, 4, 6, 8)
  - [x] Add `app/services/backtest/historical_initialization_engine.py` to orchestrate the existing qualification, roster, identity/alias, trading-calendar, yfinance evidence, market-plane, reconstruction, and snapshot-commit APIs; do not duplicate their canonicalization, retry taxonomy, detector calls, or commit predicate.
  - [x] Pin the active `SnapshotProfileV1`, requested range, calendar dataset version, and canonical ordered requested-month labels at enqueue. Keep the final Story 1.6 ordered snapshot-evidence digest null until every month is Ready, then write it once with completion.
  - [x] Reuse `BacktestRepository.interval_readiness()` to skip committed months and `commit_snapshot_month()` to publish a month. Resolve each target session through `TradingCalendar`; fetch/verify evidence through `HistoricalPriceRepository` and the Story 1.3 yfinance adapter.
  - [x] Process members by the immutable captured roster's `security_id`; support only Story 1.6's `before_first_provider_observation` exclusion proof and fail fast for every other unresolved outcome.

- [x] Add the claimed worker entry point and application lifecycle integration (AC: 2, 5-8)
  - [x] Add `app/services/backtest/worker.py` with strict CLI parsing and typed dispatch. It must open configured repositories independently, verify the claim before work, and write progress/terminal state only through compare-and-swap repository APIs.
  - [x] Wire schema initialization, interrupted-claim reconciliation, queue dispatch, and owned-child shutdown into the FastAPI lifespan in `app/api/app.py`; keep `app.main` reload behavior and tests free of real worker spawning.
  - [x] Keep all Strategy Manager mutation paths behind the existing `require_local_or_token` guard when routes arrive in Story 1.9.

- [x] Prove determinism, durability, and race safety (AC: 1-8)
  - [x] Add focused repository tests for schema constraints, atomic subtype creation, FIFO across initialization/backtest placeholders, no-op intervals, immutable config, legal/illegal transitions, monotonic versions, and independent-connection races.
  - [x] Add service/worker tests with fake subprocesses for exactly-one spawn, spawn failure, non-terminal exit, startup reconciliation, owned shutdown, stale token/version rejection, and queued preservation.
  - [x] Add engine tests for ascending month/member order, cached-month reuse, first-member fail-fast, earlier-month retention, no later work, cancellation at each safe boundary, completion-vs-cancel races, reopen durability, and no network when evidence is cached.
  - [x] Preserve import-graph tests proving no backtest lifecycle/engine/worker module imports live portfolio, trade, cash, `TraderAgent`, order submission, or notification authority.
  - [x] Run focused Backtest tests, Ruff/format/Pyrefly on touched files, then the complete repository suite.

### Review Findings

- [x] [Review][Patch] Validate the complete current qualification contract and bind enqueue validation atomically [app/services/backtest/strategy_job_service.py:60]
- [x] [Review][Patch] Dispatch claimed work by its typed job type instead of sending every FIFO entry to the initialization factory [app/services/backtest/worker.py:79]
- [x] [Review][Patch] Convert worker construction failures into their stable lifecycle failure instead of `worker_interrupted` [app/services/backtest/worker.py:88]
- [x] [Review][Patch] Preserve `calendar_error` when calendar resolution fails [app/services/backtest/historical_initialization_engine.py:127]
- [x] [Review][Patch] Resolve cancellation/version races at the failure boundary without overwriting cancellation intent as `worker_interrupted` [app/services/backtest/historical_initialization_engine.py:461]
- [x] [Review][Patch] Prevent reconciled or unterminated stale children from publishing snapshot months after losing their claim [app/services/backtest/historical_initialization_engine.py:150]
- [x] [Review][Patch] Keep the dispatcher alive after a transient polling or repository exception [app/services/backtest/strategy_job_service.py:135]
- [x] [Review][Patch] Close the SQLite trigger hole that permits standalone or arbitrary `status_version` changes [app/repositories/backtest_repo.py:454]
- [x] [Review][Patch] Add the specified strict submission, cancellation, progress, and failure contracts instead of untyped service configuration [app/services/backtest/strategy_job_service.py:60]
- [x] [Review][Patch] Replace stubbed or call-only assertions with integration coverage for real no-op readiness, retained committed months, and every cancellation boundary [tests/backtest/test_strategy_job_repository.py:354]
- [x] [Review][Patch] Disable real Strategy Manager startup before module-scoped application fixtures rather than in a later function-scoped fixture [tests/conftest.py:11]

## Dev Notes

### Scope Boundary

This story owns the durable Strategy Manager lifecycle and historical initialization execution. It does not implement restart/delete/outbox projection (Story 1.8), HTTP forms/activity rendering (Story 1.9), BAU promotion (Story 1.10), or strategy simulation (Epic 2). Do not add route-local execution or notification-driven state.

### Existing Foundations to Reuse

- `app/repositories/backtest_repo.py`: current `BACKTEST_DB` schema, `BEGIN IMMEDIATE`, immutable compare-and-insert, active profile, coverage, readiness, and complete month commit.
- `app/services/backtest/trading_calendar.py`: sole closed-month, inclusive-month, MIC session, and calendar-digest authority.
- `app/services/backtest/historical_data_qualification.py`: current-contract availability and closed provider outcome/retry rules.
- `app/services/backtest/reconstruction_roster.py`: immutable captured roster and stable security IDs.
- `app/services/backtest/historical_price_evidence.py` and `app/repositories/historical_price_repo.py`: yfinance acquisition normalization and immutable evidence.
- `app/services/backtest/historical_scan_reconstruction.py` and `snapshot_profile.py`: complete member reconstruction, legitimate exclusion, monthly commit, coverage, and readiness contracts.

Story 1.6's adversarial review repeatedly established that supplied identities/digests are not trusted when repository-owned evidence can be reverified. Apply the same rule to job configuration, progress, claim tokens, and final readiness.

### Lifecycle and Transaction Guardrails

- A SQLite transaction covers only one bounded ledger or month commit; never hold it across network access or subprocess execution.
- A service crash between claim and spawn leaves `running`, which startup reconciliation fails visibly. It must not reset to queued.
- A child exit code is not authoritative success: only a valid terminal database row is. Conversely, a terminal worker write wins over a late launcher fallback.
- `current_month` is null for queued work and means actively processing for running work. Do not present “last completed month” through this field.
- Initialization restarts from the beginning in Story 1.8 but naturally skips months already committed by this story.

### Project Structure Notes

Expected files:

- UPDATE `app/repositories/backtest_repo.py`
- NEW `app/services/backtest/strategy_job_service.py`
- NEW `app/services/backtest/historical_initialization_engine.py`
- NEW `app/services/backtest/worker.py`
- UPDATE `app/api/app.py`
- UPDATE `app/api/dependencies.py`
- NEW focused tests under `tests/backtest/`

Do not modify the existing notifications repository/schema/templates in this story and do not touch concurrent SIPP files.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Epic 1, Story 1.7]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-4, AD-9, AD-10, AD-13–AD-16, AD-21]
- [Source: `_bmad-output/planning-artifacts/prds/prd-Agents.stocks-2026-08-09/prd.md` — FR-4–FR-9, lifecycle/recovery]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/EXPERIENCE.md` — background activity, cancellation, historical coverage rules]
- [Source: `_bmad-output/implementation-artifacts/1-6-commit-versioned-monthly-snapshot-coverage.md` — transactional coverage APIs and review learnings]
- [Source: `app/repositories/backtest_repo.py` — current persistence conventions]

## Dev Agent Record

### Agent Model Used

Codex (GPT-5)

### Debug Log References

- `uv run pytest -q tests/backtest` — 237 passed after rebasing onto `origin/main`.
- `uv run pytest -q` — 1,051 passed; four localhost browser tests could not bind inside the sandbox.
- `uv run pytest -q tests/test_portfolio_import_queue_browser.py` — 4 passed with localhost binding permitted.
- Ruff check/format and Pyrefly passed for all Story 1.7 application and test files.
- Repository-wide Ruff remains blocked by nine pre-existing violations in `scripts/` and bundled `skills/`; repository-wide Pyrefly retains one pre-existing Stocktwits test error.

### Completion Notes List

- Added a SQLite-enforced FIFO lifecycle ledger with active-profile enqueue validation, one-running-job serialization, legal-transition/version triggers, immutable initialization identity, and write-once completion evidence.
- Added qualified asynchronous enqueue, exact argv subprocess dispatch, startup reconciliation, child-exit/shutdown fallback, and FastAPI lifespan ownership without coupling to BAU scanning.
- Added deterministic month/member orchestration over the existing yfinance evidence, reconstruction, cache, exclusion-proof, and Story 1.6 commit boundaries; cached evidence is revalidated before reuse.
- Added focused durability, concurrency, cancellation, fail-fast, cache reuse, worker, lifespan, and import-boundary coverage. No Story 1.8 recovery/outbox, Story 1.9 route/UI, notification, or live portfolio authority was added.
- Applied all accepted adversarial-review patches: qualification identity is transactionally pinned, dispatch and worker failures are typed, stale claims cannot publish, cancellation races preserve intent, lifecycle versions are trigger-enforced, and dispatcher/test startup boundaries are resilient.
- Final review validation: 57 focused review tests passed; 4 localhost browser tests passed; Ruff and Pyrefly passed on touched files; the complete repository suite passed with 1,067 tests.

### File List

- `_bmad-output/implementation-artifacts/1-7-run-initialization-through-the-durable-job-lifecycle.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `app/api/app.py`
- `app/api/dependencies.py`
- `app/core/config.py`
- `app/repositories/backtest_repo.py`
- `app/repositories/historical_price_repo.py`
- `app/services/backtest/historical_data_qualification.py`
- `app/services/backtest/historical_initialization_engine.py`
- `app/services/backtest/historical_price_evidence.py`
- `app/services/backtest/strategy_job.py`
- `app/services/backtest/strategy_job_service.py`
- `app/services/backtest/trading_calendar.py`
- `app/services/backtest/worker.py`
- `tests/backtest/test_backtest_worker.py`
- `tests/backtest/test_historical_data_qualification.py`
- `tests/backtest/test_historical_initialization_engine.py`
- `tests/backtest/test_historical_price_repository.py`
- `tests/backtest/test_snapshot_coverage_repository.py`
- `tests/backtest/test_strategy_job_repository.py`
- `tests/backtest/test_strategy_job_service.py`
- `tests/backtest/test_strategy_manager_lifespan.py`
- `tests/conftest.py`

## Change Log

- 2026-08-13: Implemented and validated the durable historical-initialization job lifecycle; moved Story 1.7 to review.
- 2026-08-13: Applied all accepted code-review patches, passed the complete 1,067-test repository suite, and moved Story 1.7 to done.
