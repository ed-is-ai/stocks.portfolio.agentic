---
title: 'Prepare the Evidence Needed for a Backtest'
type: 'feature'
created: '2026-08-24'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
baseline_revision: '50860f25'
final_revision: '7ea6a1a9'
---

<intent-contract>

## Intent

**Problem:** Preparation has durable selected-universe and manifest seams, but its worker does not expose the evidence stages at their actual boundaries. Cancellation can be accepted during manifest sealing, violating the atomic finish contract.

**Approach:** Make preparation execute evidence selection, FX pinning, and manifest sealing as explicit fenced stage boundaries. Preserve selected-only evidence, immutable revisions, V2 provenance, and the repository-owned atomic seal transaction.

## Boundaries & Constraints

**Always:** Preserve V1 behavior; acquire only selected securities; use existing historical-price and FX repositories; fail closed with sanitized failure codes; keep the final seal-and-create transaction authoritative.

**Block If:** The existing typed contracts cannot represent the required stage or cancellation semantics without changing the public V1/V2 identity contracts.

**Never:** Do not add provider fallbacks, live FX, generic Strategy parameters for provenance, partial manifests, or a second lifecycle/queue implementation.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | Valid selected roster and pinned evidence | Three stages are reported in order; one V2 manifest and child Backtest are atomically created | No error expected |
| MISSING_EVIDENCE | Selected price/action/FX revision unavailable | Preparation fails before sealing and creates no Backtest | Stable required-data failure |
| EARLY_CANCEL | Cancellation before manifest sealing | Preparation becomes Cancelled with no child Backtest | Safe boundary honors cancellation |
| LATE_CANCEL | Cancellation requested while manifest sealing | Cancellation is unavailable; seal transaction completes or fails atomically | Ownership/fence decides outcome |

</intent-contract>

## Code Map

- `app/services/backtest/worker.py` -- preparation stage execution and evidence-to-manifest handoff.
- `app/repositories/backtest_repo.py` -- cancellation legality and atomic preparation seal.
- `tests/backtest/test_backtest_worker.py` -- worker stage and cancellation coverage.
- `tests/backtest/test_strategy_job_repository.py` -- durable cancellation boundary coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/backtest/worker.py` -- execute preparation work at the matching evidence-selection, FX-pinning, and manifest-sealing boundaries -- keep progress truthful and cancellation safe.
- [x] `app/repositories/backtest_repo.py` -- reject cancellation once preparation reaches manifest sealing -- ensure late cancellation cannot beat the atomic seal.
- [x] `tests/backtest/test_backtest_worker.py` -- cover stage ordering, early cancellation, and full selected-only sealing behavior.
- [x] `tests/backtest/test_strategy_job_repository.py` -- cover preparation cancellation legality at the sealing stage.

**Acceptance Criteria:**
- Given valid preparation input, when the worker runs, then evidence selection, FX pinning, and manifest sealing are reported in order and the final V2 manifest plus exactly one linked Backtest are committed atomically.
- Given missing or invalid selected evidence, when the relevant stage runs, then preparation fails closed with a stable sanitized reason and no Backtest or partial manifest.
- Given cancellation before manifest sealing, when a safe boundary is reached, then preparation is Cancelled and creates no Backtest.
- Given cancellation during manifest sealing, when the cancellation endpoint is called, then it is unavailable and the seal transaction wins or fails atomically.

### Review Findings

- [x] [Review][Patch] Honour a cancellation that wins the stage-transition version race; the Preparation worker currently returns a still-running claimed job when `set_strategy_job_current_stage()` conflicts, so the dispatcher can classify the worker exit as interrupted rather than terminally cancelling it [app/services/backtest/worker.py:294] — fixed and regression-tested
- [x] [Review][Patch] Move actual conditional FX acquisition/pinning into `fx_pinning`; `_resolve_roster_evidence()` currently resolves FX while `evidence_selection` is displayed, leaving the user-visible FX stage as a no-op and violating the stage contract [app/services/backtest/worker.py:305] — fixed and regression-tested
- [x] [Review][Patch] Provide a supported Bootstrap qualification-failure recovery and retain a safe failure reason; the reported “Historical data qualification is not available” screen offered only deletion, while `_run_qualification()` discarded the recorded provider reason [app/services/backtest/strategy_bootstrap_service.py:152] — resolved in follow-up
- [x] [Follow-up][Patch] Restore live Bootstrap qualification after yfinance began returning DataFrame-valued history metadata and changed the GBP/USD probe timezone/session contract [app/services/backtest/historical_price_evidence.py; app/services/backtest/strategy_bootstrap_service.py] — resolved and live-verified
- [x] [Follow-up][Patch] Restore live Bootstrap roster capture against the maintained DataHub constituents schema, resolve DataHub identities in one bounded TradingView batch, apply provider-symbol differences through the versioned `config/provider_symbol_aliases.json` mapping, support Cboe BZX (`BATS`), and accept TradingView's `GBX` code while preserving the economic GBP/GBp contract [app/services/backtest/reconstruction_roster.py; app/integrations/tv_screener.py; config/provider_symbol_aliases.json] — resolved and live-verified
- [x] [Follow-up][Patch] Expand persisted MIC constraints and migrate existing immutable identity/alias/snapshot tables for Cboe BZX (`BATS`); fail directly on new identity constraint violations instead of masking them as canonical-identity conflicts [app/repositories/backtest_repo.py] — resolved and live-verified
- [x] [Follow-up][Patch] Persist and render safe stage-specific Bootstrap failure details for controlled provider, roster, persistence, and activation errors while reducing unexpected exceptions to their type plus a server-log instruction [app/services/backtest/strategy_bootstrap_service.py; app/api/templates/_bootstrap_activity.html] — resolved and regression-tested
- [x] [Follow-up][Patch] Render actionable readiness recoveries as links to their existing workflows; Coverage `Initialize` was a non-interactive badge despite the initialization route already existing [app/api/templates/_strategy_readiness.html] — resolved and regression-tested
- [x] [Follow-up][Patch] Swap expected Strategy Manager `422` validation and `409` conflict responses into their form targets; initialization correctly returned linked errors for invalid months, but HTMX's default 4xx handling discarded the fragment and made submit appear inert [app/api/static/js/strategy-manager.js] — resolved and regression-tested
- [x] [Follow-up][Patch] Make initialization validation independently robust to stale client assets by returning its error fragment as `200` for HTMX requests while retaining `422` for ordinary HTTP, and cache-version the Strategy Manager script URL [app/api/routes/strategy_manager.py; app/api/templates/index.html] — resolved and regression-tested
- [x] [Follow-up][Patch] Propagate the dispatcher's worker lease through historical initialization progress, month publication, failure/cancellation, and completion writes; without the fence every initialization exited on its first month and was mislabeled `worker_interrupted`. Add the missing BATS exchange timezone used by the live roster [app/services/backtest/worker.py; app/services/backtest/historical_initialization_engine.py; app/repositories/backtest_repo.py] — resolved and regression-tested

## Design Notes

The worker must not mark all stages complete before performing their work. Evidence resolution may be logically split across the two evidence stages even when existing resolver calls remain shared; the durable stage value is the user-visible lifecycle boundary, while the repository remains the authority for final identity and atomicity.

## Verification

**Commands:**
- `UV_CACHE_DIR=/tmp/agents-stocks-uv-cache uv run pytest tests/backtest/test_backtest_worker.py tests/backtest/test_strategy_job_repository.py` -- 110 passed.
- `UV_CACHE_DIR=/tmp/agents-stocks-uv-cache uv run pytest tests/backtest` -- 842 passed, 2 warnings.
- `UV_CACHE_DIR=/tmp/agents-stocks-uv-cache uv run ruff check app/services/backtest/worker.py app/repositories/backtest_repo.py tests/backtest/test_backtest_worker.py tests/backtest/test_strategy_job_repository.py` -- clean.
- `UV_CACHE_DIR=/tmp/agents-stocks-uv-cache uv run pyrefly check app/services/backtest/worker.py app/repositories/backtest_repo.py` -- 0 errors.
- `git diff --check` -- clean.
- `UV_CACHE_DIR=/private/tmp/agents-stocks-uv-cache uv run pytest tests/backtest/test_historical_initialization_engine.py tests/backtest/test_backtest_worker.py tests/backtest/test_snapshot_coverage_repository.py -q` -- 57 passed.
- `UV_CACHE_DIR=/private/tmp/agents-stocks-uv-cache uv run pytest tests/backtest -q` -- 856 passed, 2 warnings.
- `UV_CACHE_DIR=/private/tmp/agents-stocks-uv-cache uv run ruff check app/services/backtest/historical_initialization_engine.py app/services/backtest/worker.py app/repositories/backtest_repo.py tests/backtest/test_historical_initialization_engine.py tests/backtest/test_backtest_worker.py` -- clean.
- `UV_CACHE_DIR=/private/tmp/agents-stocks-uv-cache uv run pyrefly check app/services/backtest/historical_initialization_engine.py app/services/backtest/worker.py app/repositories/backtest_repo.py` -- 0 errors.

## Spec Change Log

- 2026-08-24: Normalized yfinance DataFrame metadata for deterministic response digests and aligned the GBP/USD production probe with the provider's current London timezone and New Year's Day session.
- 2026-08-24: Updated Bootstrap roster evidence for the maintained DataHub schema, batched TradingView identity evidence, config-backed class-share aliases, BATS listings, and TradingView GBX normalization; live capture normalized 876 members from all three required sources.
- 2026-08-24: Added the missing SQLite BATS constraint migration after a production-equivalent roster commit exposed the later persistence boundary; live qualification, 876-identity commit, and profile construction now complete against a fresh database.
- 2026-08-24: Replaced generic Bootstrap evidence failures with safe stage-specific activity details, including provider HTTP status and actionable database-integrity categories without exposing arbitrary exception text.
- 2026-08-24: Made readiness recovery actions navigable, including Coverage `Initialize` linking to the historical initialization form.
- 2026-08-24: Enabled scoped HTMX swaps for expected Strategy Manager form validation/conflict responses so invalid initialization months show their server-rendered errors instead of appearing to do nothing.
- 2026-08-24: Added server-side HTMX validation response handling and a Strategy Manager JavaScript cache version so initialization errors remain visible even when a browser cached the pre-fix script.
- 2026-08-24: Fenced the historical-initialization lifecycle and atomic month publication with the dispatcher's worker lease, and added BATS to the initialization exchange-timezone contract.

## Review Triage Log

### 2026-08-24 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 0
- reject: 0
- addressed_findings:
  - none

## Auto Run Result

Status: done

Implemented truthful preparation stage execution and the non-cancellable manifest-sealing boundary. Selected-only evidence resolution, FX pinning, V2 manifest provenance, and the repository-owned atomic seal remain unchanged.

Files changed:
- `app/services/backtest/worker.py` -- performs evidence work at the evidence-selection stage and preserves the sealing boundary.
- `app/repositories/backtest_repo.py` -- removes cancellation from preparation once manifest sealing begins.
- `tests/backtest/test_backtest_worker.py` -- verifies evidence failure occurs at the correct stage.
- `tests/backtest/test_strategy_job_repository.py` -- verifies sealing has no cancel action.
- `_bmad-output/implementation-artifacts/spec-4-6-prepare-the-evidence-needed-for-a-backtest.md` -- records scope and verification.

Review findings: no deferred or rejected findings; no review patches remained after verification.

Residual risk: GitHub API status could not be synchronized in this environment because `api.github.com` was unreachable; local BMAD tracking is updated.
