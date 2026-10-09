---
title: 'GH-15 Strategy experiment agent'
type: 'feature'
created: '2026-10-09'
status: 'done'
baseline_revision: b89b168814ce6b7c78315ed9a96af02caad9fbda
review_loop_iteration: 0
final_revision: edc0151e2b07429c14f45d8ee5cfdd6b757c2608
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/planning-artifacts/feature-gh-15-strategy-experiment-agent.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** Strategy Manager has no controlled path from a research hypothesis to a candidate backtest with the same immutable evidence as its baseline. Manual reconfiguration can silently change the universe, period, or evidence and invalidate the comparison.

**Approach:** Generate an auditable draft from one completed baseline and one validated declared Strategy parameter change. Require a separate approval before enqueueing exactly one candidate copied from the baseline manifest, then record a canonical comparison and conclusion.

## Boundaries & Constraints

**Always:** Treat model output as untrusted proposal data; validate one Strategy-declared parameter with the shared validator. Keep every non-parameter baseline manifest field identical. Drafting never queues. Approval binds to the stored draft digest and is idempotent. Failed/ineligible comparisons are inconclusive. Persist the plan, approval, run IDs, comparison, conclusion, provenance, and append-only audit events. Do not modify live Strategy/portfolio state.

**Block If:** No unresolved user decision is needed. If the model cannot produce one valid parameter delta plus a canonical metric and expected direction, return an explicit no-draft outcome.

**Never:** Generate or publish Strategy source, optimize multiple parameters, change universe/dates/capital/evidence, enqueue before approval, activate candidate parameters, or claim future performance. Do not implement GH-20's landing-view contextual surfaces here.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Valid draft | Hypothesis and complete, verified baseline | Persist one typed delta, metric/direction, locked pins and draft audit; enqueue nothing | None |
| No usable proposal | Model disabled/refuses, malformed output, or invalid parameter | No draft/job; explicit unavailable or validation message | Preserve audit of the attempt without storing fabricated output |
| Invalid baseline | Missing, incomplete, tombstoned, corrupt, or source-incompatible run | No draft/job | Return a bounded reason |
| Approval retry | Same valid draft digest submitted repeatedly or concurrently | One candidate job/run using cloned baseline manifest; same result returned | Conflict on changed/stale draft |
| Candidate terminal | Complete eligible result pair | Compare selected metric and direction; persist deterministic outcome and audit | Integrity/ineligibility becomes inconclusive |
| Failed or weak sample | Failed/cancelled run, missing metric, equal values, or zero closed trades | Inconclusive with both run IDs, metric availability, sample counts, and limitations | Never report a win/loss |

</intent-contract>

## Code Map

- `app/services/backtest/run_input_manifest.py` -- typed immutable V1/V2/V3 run pins to clone and compare.
- `app/services/backtest/strategy_protocol.py` -- shared validator for declared Strategy parameters.
- `app/repositories/backtest_repo.py` -- durable jobs, verified results, canonical eligibility, and SQLite schema.
- `app/services/backtest/backtest_launch_service.py` -- normal launch rebuilds from active state; experiment launch must preserve the baseline manifest instead.
- `app/services/backtest/worker.py` -- durable candidate execution and terminal completion hook.
- `app/agents/strategy_experiment/agent.py` -- Claude-first structured proposal client with a fixed-loopback Foundry Local fallback; only the approved hypothesis and declared Strategy parameter context are sent remotely.
- `app/schemas/strategy_experiment.py` -- strict typed proposal and durable lifecycle contracts, including provider attempt provenance; model output contains no run inputs or enqueue fields.
- `app/api/routes/strategy_manager.py`, `app/api/dependencies.py` -- synchronous threadpool-backed Strategy Manager experiment routes and service composition.
- `app/api/templates/_strategy_experiments.html` and `app/api/templates/_strategy_experiment_detail.html` -- baseline selection, data-use disclosure, provider-attempt provenance, review, and approval dialog.
- `tests/test_strategy_experiment_browser.py` -- Playwright keyboard and dialog interaction coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/schemas/strategy_experiment.py` -- define strict draft, state, metric/direction, comparison, and outcome models -- keep persisted/API contracts typed.
- [x] `app/agents/strategy_experiment/__init__.py` and `app/agents/strategy_experiment/agent.py` -- request a strict structured proposal from Claude, falling back to the fixed local Foundry endpoint -- model output cannot choose run inputs or enqueue.
- [x] `app/repositories/backtest_repo.py` -- persist experiments/audit events and atomically enqueue a child candidate from a verified manifest -- support V1/V2/V3 while preserving all baseline pins.
- [x] `app/services/backtest/strategy_experiment_service.py` and `app/services/backtest/strategy_job_service.py` -- validate drafts, bind approval to a digest, discard drafts, and reconcile terminal runs -- enforce lifecycle invariants in one service boundary.
- [x] `app/services/backtest/worker.py` -- trigger idempotent experiment reconciliation when the candidate reaches a terminal state -- do not depend on a GET request to finalize results.
- [x] `app/api/dependencies.py`, `app/api/routes/strategy_manager.py`, and both experiment templates -- add draft/list/detail/discard/approve routes and an accessible approval dialog -- expose review without changing the landing view.
- [x] `tests/backtest/test_strategy_experiment_agent.py`, `tests/backtest/test_strategy_experiment_service.py`, `tests/backtest/test_backtest_repo_experiments.py`, `tests/backtest/test_strategy_experiment_worker.py`, `tests/test_strategy_experiment_routes.py`, and `tests/test_strategy_experiment_browser.py` -- cover the I/O matrix, parameter validation, manifest equality, race/idempotency, worker reconciliation, audit traceability, and dialog keyboard behavior -- prove no draft path can enqueue and only approval can create one candidate.
- [x] `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` and `_bmad-output/implementation-artifacts/sprint-status.yaml` -- record feature/story issue IDs and their review status -- keep BMAD and GitHub progress aligned.

**Acceptance Criteria:**
- Given a completed verified baseline, when a valid hypothesis is drafted, then only one declared parameter changes and all Strategy, universe, date, currency, capital, host-parameter, and evidence pins are preserved; no job is queued.
- Given unavailable, malformed, ambiguous, or invalid model output, when drafting is requested, then no draft or job is created and the response explains why.
- Given a draft, when the user reviews it, then the approval dialog shows the baseline, every locked input, the exact write, and how the result will be verified; keyboard focus stays in the dialog and returns to its trigger when it closes.
- Given explicit approval, when the request is accepted, then one durable candidate is enqueued with a cloned baseline manifest whose only semantic change is the approved parameter; duplicate or concurrent approval returns that same candidate.
- Given terminal baseline and candidate jobs, when reconciliation runs, then the canonical comparison predicate and verified metrics produce supported/contradicted/inconclusive deterministically, and the audit records both run IDs, values, sample counts, provenance, and limitations.
- Given any failed, cancelled, ineligible, corrupt, metric-unavailable, equal-metric, or zero-trade outcome, when reconciliation runs, then the experiment is inconclusive and no win/loss or future-performance claim is made.

## Spec Change Log

- 2026-10-09: Proposal generation uses the fixed localhost Foundry Local endpoint, following the existing Analyst implementation. The externally hosted model call was rejected by automatic privacy review because it would transmit the hypothesis and Strategy parameter context outside the app; no remote fallback was added. Unavailable local model returns no draft.
- 2026-10-09: The user directed Claude to be the default and explicitly approved sending the hypothesis, Strategy ID, declared parameter definitions, and current parameter values to Anthropic. The baseline ID and manifest, Strategy source, other run inputs, and historical results stay local. `claude-sonnet-5` is primary; fixed-loopback Foundry Local is tried when Claude is unconfigured, unavailable, or cannot return schema-valid output. A schema-valid proposal that fails local Strategy parameter validation is rejected without a draft. This user-approved decision supersedes the earlier local-only choice.
- 2026-10-09: Persist the provider/model attempts and their outcomes in each successful draft so fallback drafts retain the audit trail showing whether Anthropic received the proposal context.

## Review Triage Log

### 2026-10-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 13 (high 1, medium 5, low 7)
- defer: 0
- reject: 0
- addressed_findings:
  - `[high]` `[patch]` Localhost inference could inherit proxy environment settings and expose hypotheses/Strategy parameters; disable environment proxy discovery for the Foundry HTTP client.
  - `[medium]` `[patch]` Proposal generation ran synchronously inside async routes; make experiment routes synchronous so FastAPI runs them in its threadpool.
  - `[medium]` `[patch]` Transient comparison/database failures could be finalized as inconclusive; narrow integrity catches and let retryable storage failures propagate.
  - `[medium]` `[patch]` A terminal candidate could remain approved after reconciliation failure; add a durable pending-terminal scan to the dispatcher and retry reconciliation.
  - `[low]` `[patch]` Failed candidate runs omitted their stored manifest digest; populate it from the candidate run record even when no result exists.
  - `[low]` `[patch]` Rejected/unavailable draft attempts had no reader; expose recent attempt audit records in the experiment list.
  - `[medium]` `[patch]` One corrupt recent result could suppress every baseline option; verify options individually and skip only damaged rows.
  - `[low]` `[patch]` Baseline selection read every historic result; cap the verified option list to the 25 most recent completed runs.
  - `[low]` `[patch]` Token approvals were attributed to a local user; record the API-token actor explicitly.
  - `[low]` `[patch]` Discard was immediately irreversible; add a confirmation step.
  - `[low]` `[patch]` The code map pointed at a nonexistent browser test path; update it to the implemented file.
  - `[medium]` `[patch]` Recreating an unchanged SQLite index changed `schema_version` and invalidated snapshot caches; preserve the index when its definition matches.
  - `[low]` `[patch]` JSON `NaN` could pass the proposal numeric contract; reject non-finite parameter values.

### 2026-10-09 — Claude-first provider follow-up review
- intent_gap: 0
- bad_spec: 0
- patch: 2 (medium 1, low 1)
- defer: 0
- reject: 0
- addressed_findings:
  - `[low]` `[patch]` The UI implied that every invalid Claude proposal triggers Foundry; clarify fallback only handles missing/unavailable Claude or schema-invalid output, then state local Strategy parameter validation can reject a schema-valid proposal without creating a draft.
  - `[medium]` `[patch]` A saved Foundry fallback draft hid the earlier Claude attempt; persist and display provider/model attempts with their outcomes so the audit shows if Anthropic received proposal context.

## Design Notes

The ordinary `BacktestLaunchService.launch()` reconstructs inputs from the active profile and current evidence. It cannot safely relaunch an old baseline. Approval must verify the stored baseline result/manifest, make a typed copy changing only its parameter mapping, and enqueue through the durable job path. The manifest digest will change; its execution contract and every other manifest field must remain equal. The canonical comparison service does not compare all experiment-specific constraints (for example starting capital or Strategy source identity), so the experiment service must enforce those before recording an outcome.

The draft locks one key from `BacktestMetricsV1` plus an expected numeric direction. The deterministic verdict uses only that approved metric: aligned change is supported, opposite change contradicted; equal/unavailable values or zero closed trades are inconclusive. Always disclose both closed-trade counts; no arbitrary statistical cutoff is introduced.

## Verification

**Commands:**
- `rtk .venv/bin/pytest -q tests/backtest tests/test_strategy_manager_routes.py tests/test_strategy_experiment_routes.py tests/test_strategy_experiment_browser.py` -- 1,730 passed, 1 skipped (Chromium unavailable), 3 warnings.
- `rtk .venv/bin/ruff check app tests` -- passed.
- `rtk .venv/bin/pyrefly check app/agents/strategy_experiment app/api/dependencies.py app/api/routes/strategy_manager.py app/repositories/backtest_repo.py app/schemas/strategy_experiment.py app/services/backtest/strategy_experiment_service.py app/services/backtest/strategy_job_service.py app/services/backtest/worker.py` -- 0 errors (4 suppressed, 3 warnings not shown).
- `rtk .venv/bin/python -m compileall -q` on changed Python modules -- passed.
- `rtk git diff --check` -- passed.
- `rtk .venv/bin/pytest -q` -- 4,508 passed, 1 skipped, 7 failed, 11 errors. All 18 failures/errors were browser tests blocked by sandbox localhost/Chromium permissions.
- Escalated browser-only rerun -- 18 passed, 1 existing boot-splash test failed due to a Chromium launch timeout; the new Strategy experiment browser test passed.
- Claude-first provider follow-up: `rtk .venv/bin/pytest -q tests/backtest/test_strategy_experiment_agent.py tests/backtest/test_strategy_experiment_service.py tests/backtest/test_backtest_repo_experiments.py tests/test_strategy_experiment_routes.py tests/test_strategy_experiment_browser.py` -- 37 passed, 1 skipped (Chromium unavailable).
- Provider follow-up: `rtk .venv/bin/ruff check app tests`, scoped Pyrefly, `compileall`, and `rtk git diff --check` -- passed.
- No live Anthropic or Foundry proposal request was made during verification.

## Auto Run Result

### Summary

Implemented the fixed-input Strategy experiment workflow: generate and validate a Claude proposal with local Foundry fallback, preserve the verified baseline manifest, require digest-bound approval before a single candidate enqueue, reconcile results durably, and expose audit and comparison details. The experiment screen discloses the proposal data sent to Anthropic and records the winning provider/model plus every attempted provider/model and outcome.

### Files changed

- `app/agents/strategy_experiment/` -- Claude-first proposal agent with strict output, call-time API key lookup, and local Foundry fallback with proxy isolation.
- `app/api/dependencies.py` -- inject experiment service and agent dependencies.
- `app/api/routes/strategy_manager.py` -- list, draft, detail, discard, approve, and audit routes.
- `app/api/templates/_strategy_experiments.html` -- experiment list, bounded baseline choices, attempt history, and proposal data-use disclosure.
- `app/api/templates/_strategy_experiment_detail.html` -- locked manifest, comparison, audit, provider/model attempt history, and approval confirmation.
- `app/repositories/backtest_repo.py` -- experiment persistence, cloned candidate enqueue, audit queries, verified baselines, and durable reconciliation scan.
- `app/schemas/strategy_experiment.py` -- strict typed proposal, draft, provider attempt, approval, comparison, and conclusion models.
- `app/services/backtest/strategy_experiment_service.py` -- proposal validation, lifecycle, manifest checks, and deterministic conclusion.
- `app/services/backtest/strategy_job_service.py` and `app/services/backtest/worker.py` -- retry and terminal reconciliation integration.
- `tests/backtest/test_backtest_repo_experiments.py` -- persistence, atomic approval, baseline filtering, and audit coverage.
- `tests/backtest/test_strategy_experiment_agent.py` -- Claude-first strict output, provider attempt outcomes, invalid-response fallback, call-time key lookup, and non-finite-value rejection.
- `tests/backtest/test_strategy_experiment_service.py` -- lifecycle, manifest, error, and verdict coverage.
- `tests/backtest/test_strategy_experiment_worker.py` -- durable retry coverage.
- `tests/test_strategy_experiment_routes.py` -- route behavior, audit actor coverage, provider disclosure, and detail provenance.
- `tests/test_strategy_experiment_browser.py` -- review-dialog keyboard and focus behavior.
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` and `sprint-status.yaml` -- feature/story issue and progress tracking.
- `_bmad-output/planning-artifacts/feature-gh-15-strategy-experiment-agent.md` and this spec -- scope and implementation record.

### Findings and disposition

- Patches applied: 13 (1 high, 5 medium, 7 low).
- Deferred: 0.
- Rejected as noise: 0.
- Follow-up review recommended: `true`, because the review changes span privacy boundaries, durable worker recovery, persistence, and UI audit behavior.

### Residual risks

- No live Anthropic or Foundry proposal round-trip was run; provider success and fallback behavior are covered with mocked clients.
- Chromium launch is intermittent in this environment: browser suites first failed when sandbox permissions blocked local sockets/process startup; after an escalated rerun, the feature browser test passed and one unrelated splash test timed out launching Chromium.
- No feature-related failures remain; the repository-wide non-browser tests and the full affected backtest/Strategy Manager suites passed.
