---
title: 'Bootstrap Setup, Readiness/Diagnostics, and Universe Selection'
type: 'feature'
created: '2026-08-21'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
  - '{project-root}/_bmad-output/planning-artifacts/epics.md'
warnings: ['multiple-goals', 'oversized']
baseline_revision: '1c5ff8909347432272db7be188323dda9346fe57'
final_revision: 'b9f66dca5adb6358ef6f71d97aa302586b123b08'
---

<intent-contract>

## Intent

**Problem:** The four-activity job schema and fenced lease lifecycle from Story 4.1/4.2 are in place, but the Bootstrap `StageWalkEngine` is a no-op stub (no real qualification/roster-capture/profile-activation), there is no `StrategyReadinessService` to project typed prerequisite state, and the Strategy configuration form has no roster-backed multi-select universe selector — so Edyau cannot set up Strategy Manager, see what's blocking, or choose securities for a Backtest through supported surfaces.

**Approach:** (1) Replace the `StageWalkEngine` stub with a real `StrategyBootstrapService` that runs qualification → roster capture → profile activation through the existing `QualificationAvailabilityService`, `ReconstructionRosterPolicyV1`, and `BacktestRepository.activate_snapshot_profile()`, plus a setup route/template with guarded confirmation and idempotent no-op detection. (2) Add `StrategyReadinessService` composing six prerequisites (qualification, roster, active profile, coverage, worker, discovery) plus worker state and bounded recent failures, with a read-only readiness/diagnostics route and template. (3) Add a roster-backed multi-select universe selector to the configuration form, with server-side canonicalization via `canonical_run_universe`/`run_universe_digest`, stale-profile rejection, and the host-bound parameter hidden from generic fields.

## Boundaries & Constraints

**Always:**
- Bootstrap is one guarded, idempotent action: compatible repeat is a verified no-op; failed/interrupted partial work is never usable.
- Bootstrap final activation and the atomic stage transition to `COMPLETE` are non-cancellable.
- Readiness/diagnostics GETs are read-only — they never create, repair, acquire, activate, or queue anything.
- Universe selection canonicalizes to a sorted, deduplicated, immutable tuple of security IDs; digest is order-independent.
- All mutations use `require_local_or_token`; page loads, readiness polling, and unauthorized requests never start work.
- Leaving/refreshing any page never cancels or duplicates durable work.
- Every surface stays labelled Production or "Fixture — not production readiness".
- Worker interruption keeps stored status "Running" but shows "Worker interrupted" and suppresses owner-dependent actions until reconciliation.
- WCAG 2.2 AA floor: status text is mandatory (never color-only), keyboard-operable selector, one page-level polite status coordinator.

**Block If:**
- A real provider qualification attempt is needed in a non-Fixture environment and the provider is unavailable — HALT with `provider_unavailable`.
- The roster capture sources (DataHub/TradingView) return conflicting identities that cannot be resolved without human judgement — HALT with `roster_identity_conflict`.

**Never:**
- No live SIPP/ISA portfolio access, `TraderAgent`, or order submission path from any Strategy Manager code.
- No database edits or manual identity assignment in the setup flow.
- No per-Strategy maximum on universe size; no free-text identity entry.
- No secrets, local paths, raw provider payloads, stack traces, or database-edit instructions in diagnostics.
- No V1 manifest creation or V1 comparison changes — V2 preparation/execution is Story 4.6/4.7's scope.
- No live-valuation FX in Bootstrap or readiness — historical-price FX only where applicable.

## I/O & Edge-Case Matrix

### Story 4.3: Bootstrap Setup

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| No active profile, page load | No `active_snapshot_profile` | Setup-required banner with "Set up Strategy Manager" action | No error |
| Setup confirmed, authorized | Local-or-token POST with idempotency key | One `bootstrap` job enqueued, redirect to activity | Repeated key returns existing job |
| Setup running, stage progress | Job `RUNNING`, stage `QUALIFICATION` | Stages: "Verifying historical data", "Capturing securities", "Validating scanner profile", "Activating setup" | Leaving page does not cancel |
| All stages succeed | Final activation commits | `COMPLETE`, one active profile available | No error |
| Setup fails before activation | Stage fails | `FAILED`, no partial profile usable, sanitized reason + recovery | No usable partial state |
| Cancel before activation | Acknowledged at safe step | `CANCELLED`, no profile activated | Retained audit evidence |
| Cancel during activation | Activation started | Cancel unavailable, "Finishing setup" | Committed activation completes |
| Compatible repeat | Active profile already exists | "Strategy Manager is already set up" with verification time | No recapture, no new profile |
| Fixture environment | `STRATEGY_FIXTURE=1` or test env | Same service/route/worker path, "Fixture — not production readiness" label | No error |
| Unauthorized setup POST | No local-or-token | 403, safe page state preserved | No activity created |

### Story 4.4: Readiness & Diagnostics

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Readiness requested | GET readiness | Six independent prerequisite rows with state/reason/time/recovery | Read-only, no mutation |
| Prerequisite unavailable | e.g. qualification missing | Row shows `missing` + reason + recovery action | One generic warning does not mask others |
| Worker interrupted | Persisted lease has stale heartbeat | Worker row shows `unavailable_interrupted`, activity stays "Running" | Suppress owner-dependent actions |
| Malformed Strategy Skills | Discovery returns warnings | Sanitized warnings with stable reasons, valid Strategies remain selectable | No blocking |
| Diagnostics without complete config | GET diagnostics, no Strategy/universe | Only allowlisted general readiness identities/states/codes | No Run-specific guessing |
| Recent job failures in diagnostics | Failed jobs exist | Bounded entries: type/stage/code/time/recovery | No secrets/paths/payloads |
| No activities or notifications | Empty state | Explicit empty state message | No stale progress |
| Unauthorized diagnostics GET | No local-or-token | Safe non-mutating state, concise message | No privileged detail |

### Story 4.5: Universe Selection

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Active profile, config opens | `active_snapshot_profile` exists | Multi-select with roster securities, symbol/MIC/currency per option | No error |
| Roster ordered | Options displayed | Deterministic symbol-and-market ordering | Similar symbols distinguishable |
| Search filters roster | User types in search | Hidden options stay selected, count + removable chips shown | No error |
| Keyboard selection | Tab/Space/Enter | All operations available without pointer | Labels and counts announced |
| Strategy declares `strategy_universe.v1` | `mode: selected-securities` | Host-bound parameter hidden from generic fields | Roster selector is only input |
| Valid non-empty selection | Unique security IDs submitted | Canonicalized to sorted tuple, `run_universe_digest` computed | Any non-empty count accepted |
| Same IDs, different order | Two submissions | Same canonical universe and digest | UI order irrelevant |
| Empty/duplicate/unknown/malformed selection | Invalid submission | Run cannot proceed, affected selection identified | Valid config preserved |
| Stale profile | Profile changed since load | Activation sequence + profile hash fail validation | Stale selection cleared, safe fields preserved |
| Valid universe selected | Config summary displayed | Shows selected securities + count alongside Strategy/period/capital/currency | No evidence acquired, no activity created |

</intent-contract>

## Code Map

- `app/services/backtest/strategy_bootstrap_service.py` -- NEW: orchestrates Bootstrap stages (qualification, roster capture, profile activation), idempotent no-op check, delegates to `StrategyJobService.enqueue_bootstrap`
- `app/services/backtest/worker.py` -- MODIFY: replace `StageWalkEngine` no-op with real stage logic calling `StrategyBootstrapService` stage methods
- `app/services/backtest/strategy_readiness_service.py` -- NEW: composes six prerequisites + worker state + bounded recent failures into `StrategyReadinessV1`
- `app/services/backtest/strategy_job.py` -- MODIFY: add `StrategyReadinessV1`, `WorkerReadinessV1`, `RecentJobFailureV1`, `PrerequisiteState`/`WorkerState` enums, `RunUniverseSelectionV1` model
- `app/repositories/backtest_repo.py` -- MODIFY: add `recent_job_failures(limit)` query, `roster_member_identities(profile_hash)` for universe selector, `run_preparations` table deferred to 4.6
- `app/api/routes/strategy_manager.py` -- MODIFY: add setup route (`GET/POST /strategy-manager/setup`), readiness route (`GET /strategy-manager/readiness`), diagnostics route (`GET /strategy-manager/diagnostics`), universe selector partial (`GET /strategy-manager/configuration/universe`), modify configuration POST to accept `security_ids` and canonicalize
- `app/api/dependencies.py` -- MODIFY: wire `StrategyBootstrapService`, `StrategyReadinessService`
- `app/api/templates/_strategy_manager.html` -- MODIFY: add setup-required banner and setup action when no active profile
- `app/api/templates/_strategy_setup.html` -- NEW: setup confirmation form with guarded action
- `app/api/templates/_strategy_readiness.html` -- NEW: readiness prerequisite rows + diagnostics
- `app/api/templates/_strategy_configuration.html` -- MODIFY: add universe multi-select section, hide host-bound parameter
- `app/api/templates/_universe_selector.html` -- NEW: accessible searchable multi-select partial
- `tests/backtest/test_strategy_bootstrap_service.py` -- NEW: Bootstrap service tests (stages, idempotent no-op, failure, cancellation, fixture)
- `tests/backtest/test_strategy_readiness_service.py` -- NEW: readiness composition tests (six prerequisites, worker states, diagnostics, empty states)
- `tests/backtest/test_universe_selection_routes.py` -- NEW: universe selector route tests (ordering, search, canonicalization, stale profile, validation)
- `tests/backtest/test_strategy_setup_routes.py` -- NEW: setup route tests (confirmation, idempotency, unauthorized, fixture label)

## Tasks & Acceptance

**Execution:**
- [x] `app/services/backtest/strategy_job.py` -- add readiness/universe models (`PrerequisiteState`, `WorkerState`, `StrategyReadinessV1`, `WorkerReadinessV1`, `RecentJobFailureV1`, `RunUniverseSelectionV1`) -- these typed models are the contract for services and routes
- [x] `app/services/backtest/strategy_bootstrap_service.py` -- create `StrategyBootstrapService` with `is_setup_required()`, `start_setup(idempotency_key)`, stage execution methods (`_run_qualification`, `_capture_roster`, `_validate_profile`, `_activate_profile`), and `is_already_set_up()` no-op check -- orchestrates real Bootstrap domain logic through existing qualification/roster/profile services
- [x] `app/services/backtest/worker.py` -- replace `StageWalkEngine` no-op stage transitions with calls to `StrategyBootstrapService` stage methods so the worker process runs real Bootstrap logic -- the worker already dispatches `bootstrap` jobs to `StageWalkEngine`, so this is wiring real logic into the existing scaffold
- [x] `app/services/backtest/strategy_readiness_service.py` -- create `StrategyReadinessService` with `evaluate()` returning `StrategyReadinessV1` composing six prerequisites (qualification, roster, active profile, coverage, worker, discovery) each with closed-vocabulary state/reason/recovery, plus `diagnostics()` returning bounded recent failures -- read-only, never mutates
- [x] `app/repositories/backtest_repo.py` -- add `recent_job_failures(limit)` returning bounded `RecentJobFailureV1` tuples, and `roster_member_identities(profile_hash)` returning `(security_id, provider_symbol, mic, quote_currency)` tuples for the universe selector -- both are read-only queries over existing tables
- [x] `app/api/dependencies.py` -- wire `StrategyBootstrapService` and `StrategyReadinessService` with `get_bootstrap_service()` and `get_readiness_service()` providers -- follows existing `get_backtest_launch_service` pattern
- [x] `app/api/routes/strategy_manager.py` -- add `GET/POST /strategy-manager/setup` (guarded confirmation, idempotent enqueue, redirect to activity), `GET /strategy-manager/readiness` (read-only readiness rows), `GET /strategy-manager/diagnostics` (bounded diagnostics), `GET /strategy-manager/configuration/universe` (roster multi-select partial), modify `POST /strategy-manager/configuration` to accept `security_ids` and canonicalize via `canonical_run_universe`/`run_universe_digest` -- all mutations behind `require_local_or_token`
- [x] `app/api/templates/_strategy_manager.html` -- add setup-required banner with "Set up Strategy Manager" action when no active profile, link to readiness/diagnostics
- [x] `app/api/templates/_strategy_setup.html` -- create setup confirmation form explaining stages, guarded action, fixture label
- [x] `app/api/templates/_strategy_readiness.html` -- create readiness prerequisite rows (state/reason/time/recovery per item), worker state, discovery warnings, diagnostics section, empty states, production/fixture label
- [x] `app/api/templates/_strategy_configuration.html` -- add universe multi-select section before period/capital fields, hide host-bound parameter from generic fields, show selected count and removable chips
- [x] `app/api/templates/_universe_selector.html` -- create accessible searchable multi-select partial with deterministic ordering, keyboard operability, selected count, no-match state, removable chips
- [x] `tests/backtest/test_strategy_bootstrap_service.py` -- test all Bootstrap I/O matrix scenarios: setup required, idempotent no-op, stage progression, failure before activation, cancellation, fixture labelling
- [x] `tests/backtest/test_strategy_readiness_service.py` -- test six prerequisites independently, worker states, diagnostics bounding, empty states, read-only guarantee, fixture label
- [x] `tests/backtest/test_universe_selection_routes.py` -- test roster ordering, search persistence, canonicalization, stale profile rejection, empty/duplicate/unknown rejection, keyboard accessibility
- [x] `tests/backtest/test_strategy_setup_routes.py` -- test setup confirmation, idempotency, unauthorized rejection, fixture label, activity redirect

**Acceptance Criteria:**
- Given no active profile, when Edyau opens Strategy Manager, then it shows setup-required banner with one "Set up Strategy Manager" action
- Given setup is confirmed by authorized request, when the command is accepted, then one durable `bootstrap` job is created and repeated submission returns the existing activity
- Given setup is running, when Edyau opens the activity, then progress shows user-readable stages and leaving/refreshing does not cancel or duplicate
- Given all setup stages succeed, when final activation commits, then one compatible active profile is available and Historical initialization is enabled
- Given setup fails before activation, when Edyau reviews the activity, then no partial profile is usable and the page shows failed stage, reason, time, and one recovery action
- Given cancellation is acknowledged before activation, when the activity reaches a safe step, then it becomes Cancelled and activates no profile
- Given activation has started, when cancellation is requested, then Cancel is unavailable and the page says "Finishing setup"
- Given compatible setup is already active, when Edyau repeats the action, then it reports "Strategy Manager is already set up" with verification time and does not recapture
- Given readiness is requested, when `StrategyReadinessService` evaluates, then it evaluates six prerequisites independently and read-only
- Given a non-worker prerequisite is evaluated, when its state is returned, then it uses closed vocabulary `missing`/`running`/`ready`/`stale_incompatible`/`failed`/`integrity_error` with reason and recovery
- Given worker readiness is evaluated, when the persisted lease is inspected, then it reports `disabled`/`unavailable_interrupted`/`busy`/`ready` from storage not in-process memory
- Given one or more prerequisites are unavailable, when readiness is displayed, then each appears as an independent row and one generic warning does not mask others
- Given qualification/roster/profile are ready but coverage is missing, when readiness is displayed, then it identifies Historical initialization as the next action
- Given a persisted worker interruption, when Edyau views readiness, then worker shows "Worker interrupted" while activity remains "Running" until reconciliation
- Given malformed Strategy Skills coexist with valid ones, when discovery readiness is displayed, then affected Skills appear as sanitized warnings and valid Strategies remain selectable
- Given diagnostics are requested, when the projection is built, then it contains only allowlisted identities/states/timestamps/recovery codes without secrets/paths/payloads
- Given a compatible active profile, when the Run configuration opens, then it shows a searchable multi-select with securities from that profile showing symbol/MIC/currency
- Given the roster is displayed, when options are ordered, then they use deterministic symbol-and-market ordering
- Given Edyau searches the roster, when options become hidden, then previously selected securities remain selected with count and removable items
- Given a Strategy declares `strategy_universe.v1` with `selected-securities`, when its parameter form renders, then the host-bound parameter is hidden and the roster selector is the only input
- Given one or more unique securities are selected, when validated, then any non-empty number is accepted and canonicalized to a sorted immutable tuple
- Given selections in different orders with same IDs, when canonicalized, then they produce the same canonical universe and `run_universe_digest`
- Given empty/duplicate/unknown/malformed/out-of-profile selection, when the server validates, then the Run cannot proceed and the page identifies the affected selection without discarding valid config
- Given the active profile changes after loading, when Edyau submits a stale selection, then activation sequence and profile hash fail validation, stale selection is cleared, and safe fields are preserved
- Given a valid universe is selected, when the configuration summary displays, then it shows selected securities and count alongside Strategy/period/capital/currency without acquiring evidence or creating an activity

## Spec Change Log

## Review Triage Log

### 2026-08-21 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 9: (high 1, medium 4, low 4)
- defer: 1: (low 1)
- reject: 0
- addressed_findings:
  - `[high]` `[patch]` Fixed "Strategyyies" typo in discovery reason string — was producing "Strategyyies" for plural instead of "Strategies"
  - `[medium]` `[patch]` Fixed `diagnostics()` double-evaluation — added optional `readiness` parameter to avoid re-evaluating all six prerequisites
  - `[medium]` `[patch]` Removed unused `idempotency_key` parameter from `start_setup()` — was accepted but never passed to `enqueue_bootstrap()`
  - `[medium]` `[patch]` Narrowed `_capture_roster` exception catch from bare `Exception` to `BacktestIntegrityError` — prevents masking unexpected errors
  - `[medium]` `[patch]` Added try/except around `JobFailureCode()` construction in `recent_job_failures()` — prevents crash on corrupt failure_code data
  - `[low]` `[patch]` Changed string comparison to enum identity comparison in `_evaluate_worker` — `j.status is StrategyJobStatus.RUNNING` instead of `j.status.value == "running"`
  - `[low]` `[patch]` Removed dead `_digest` variable and unused `run_universe_digest` import in configuration POST
  - `[low]` `[patch]` Made `jobs` parameter optional in `StrategyBootstrapService.__init__` — removed `type: ignore` hack in worker
  - `[low]` `[patch]` Added `StrategyJobConflict` exception handling in `submit_strategy_setup` — prevents 500 on concurrent enqueue

### 2026-08-22 — PR #278 follow-up code review

- [ ] [Review][Patch] Bootstrap cannot complete a first production setup [app/services/backtest/strategy_bootstrap_service.py:93] — all three stage methods only inspect pre-existing qualification/identity/profile state. A clean production database fails qualification at line 100; even pre-seeded qualification and identities cannot succeed because `_activate_profile()` always fails without an already-active profile (lines 143–160). This violates the bootstrap contract/AC that setup runs qualification → roster capture → profile activation and leaves one active profile when successful. Wire the existing qualification runner, roster capture policy, profile validation/persistence, and `activate_snapshot_profile()` into the fenced worker stages; add a clean-production-success integration test.
- [ ] [Review][Patch] Searching the universe selector discards every selected security [app/api/templates/_universe_selector.html:15] — the htmx request includes checked IDs, but the GET route neither accepts them nor passes a selected set back to the partial. The replacement renders every checkbox unchecked and hard-codes `0 selected` (lines 25–45); it also never renders removable chips or a no-match state when the roster is nonempty (line 53 tests the unfiltered list). This violates Story 4.5's search-persistence/accessibility AC. Carry selected IDs through the request/render context, mark retained options checked, render chips/count from the full selected set, and test search with selections that become hidden.
- [ ] [Review][Patch] Setup submissions have no idempotency identity [app/api/templates/_strategy_setup.html:31] — the form sends no idempotency key, `StrategyBootstrapService.start_setup()` takes none, and `enqueue_bootstrap()` delegates to a job creator with no request key. A repeated or double-clicked POST therefore returns the conflict template rather than the existing activity, contrary to Story 4.3 AC 2. Add and persist an idempotency key on bootstrap submissions, return the prior job for the same key, and cover repeat/double-submit behavior.
- [ ] [Review][Patch] Readiness can report a roster as ready when no usable roster exists [app/services/backtest/strategy_readiness_service.py:136] — `_evaluate_roster()` checks global `security_identities` only, not a captured reconstruction roster associated with the active profile/lineage. Any leftover identity makes the roster row `ready`, even if `roster_digest_for_lineage()` is absent or the profile's roster manifest is missing. Evaluate the profile-bound lineage roster and report missing/integrity state when it cannot be resolved.
- [ ] [Review][Patch] The selected-universe digest is never computed or stored by this flow [app/api/routes/strategy_manager.py:802] — the route canonicalizes IDs and injects the tuple into parameters, but never calls `run_universe_digest()`; `RunUniverseSelectionV1` has no call site. Thus the AC requiring same selections to produce a persisted/usable `run_universe_digest` is not implemented. Construct and carry the typed selection/digest through the launch/preparation identity, with an end-to-end order-independence test.

## Design Notes

**Bootstrap stage mapping:** The existing `BootstrapStage` enum has `QUALIFICATION`, `ROSTER_CAPTURE`, `PROFILE_ACTIVATION`. The epics describe four user-readable stages ("Verifying historical data", "Capturing securities", "Validating scanner profile", "Activating setup") — map `QUALIFICATION` → "Verifying historical data", `ROSTER_CAPTURE` → "Capturing securities" + "Validating scanner profile", `PROFILE_ACTIVATION` → "Activating setup". The `StageWalkEngine` already walks `STAGE_SEQUENCES[StrategyJobType.BOOTSTRAP]` and calls `repo.set_strategy_job_current_stage()` / `repo.complete_claimed_stage_job()` — the service methods plug into this existing scaffold.

**Idempotent no-op:** `is_already_set_up()` checks `repo.active_snapshot_profile()` — if a compatible active profile exists, the setup action returns a no-op result without enqueuing a job. This mirrors `enqueue_initialization`'s no-op pattern.

**Readiness composition pattern:** Each prerequisite is an independent `(state, reason, recovery_action)` tuple. The service queries each via existing repo methods: `current_qualification_contract_digest()` for qualification, `roster_digest_for_lineage()` for roster, `active_snapshot_profile()` for profile, `snapshot_coverage()` for coverage, `read_worker_lease()` for worker, `discover_strategies()` for discovery. All read-only.

**Universe selector:** The multi-select loads `repo.roster_member_identities(profile_hash)` returning `(security_id, provider_symbol, mic, quote_currency)` tuples sorted by `(provider_symbol, mic)`. On submit, `security_ids` (a list of security_id values) is canonicalized via `canonical_run_universe()` and `run_universe_digest()`. The host-bound parameter (declared as `strategy_universe.v1` with `mode: selected-securities`) is detected from `StrategyDescriptorV1.universe` and hidden from the generic parameter fields rendered by `_strategy_configuration_fields.html`.

**Stale profile detection:** The form carries `profile_hash` and `activation_seq` (from `ActiveSnapshotProfileV1`). On submit, the route re-reads `active_snapshot_profile()` and compares — if the hash or sequence changed, the selection is stale. Safe fields (strategy_id, start_month, end_month, base_currency, starting_capital) are preserved; `security_ids` is cleared.

## Verification

**Commands:**
- `uv run pytest tests/backtest/test_strategy_bootstrap_service.py tests/backtest/test_strategy_readiness_service.py tests/backtest/test_universe_selection_routes.py tests/backtest/test_strategy_setup_routes.py -v` -- expected: all tests pass
- `uv run pytest tests/backtest/ -v` -- expected: no regressions in existing backtest tests
- `uv run pytest tests/test_strategy_manager_routes.py -v` -- expected: no regressions in existing route tests
- `uv run ruff format .` -- expected: no formatting errors in changed files
- `uv run ruff check .` -- expected: no lint errors in changed files
- `uv run pyrefly check` -- expected: no type errors in changed files
