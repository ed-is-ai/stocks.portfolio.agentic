---
title: 'Configure and explain the top-X basket'
type: 'feature'
created: '2026-08-29'
status: 'done'
baseline_revision: '16daf497f3b95a158270daa54608bd41ad97f46b'
final_revision: '29616ad1b77fdbfcb9faf5271eb57cf6d2f9cf71'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-366-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-gh-370-rank-and-buy-top-x-basket.md'
warnings: []
github_issue: 371
parent_issue: 366
---

<intent-contract>

## Intent

**Problem:** Buy and Hold now persists its one-time top-X decision evidence, but Strategy Manager does not yet make the configured basket or its exclusions understandable on a completed Result. Users need to verify what was selected, why, and that a configured basket remains reproducible through launch and restart.

**Approach:** Keep configuration discovery-driven, then project the immutable persisted initial-selection evidence into a prominent, accessible Result table. Extend focused route, presenter, and clean-checkout coverage without recalculating rankings or changing allocation/selection behavior.

## Boundaries & Constraints

**Always:** Render `top_x` from the Skill's shared integer metadata with default 10, minimum 1, description, and field-level shared-validation errors. Preserve the submitted canonical value in preparation, manifest, completed Result, and restart. On Results with recorded initial selection, show a default-visible deterministic table with selection session; metric/version; the label `Trailing return (252 sessions)`; the explanation “Calculated from the 252 prior trading sessions, ending before the basket-selection date”; rank; canonical/display security identity; return at one decimal; outcome; and plain-language exclusion text. An eligible score requires 253 valid closes strictly before selection; present insufficient history as `Insufficient price history for the 252-session return`. Use persisted evidence only, order rows by stored rank, and resolve unknown identities as `Unknown security` plus the canonical ID.

**Block If:** A required Result presentation field cannot be derived from the immutable stored `InitialEntrySelectionV1` evidence without recomputing it, or a current migration/compatibility constraint prevents old Results from remaining readable.

**Never:** Re-rank, refetch data, mutate decision evidence, add UI-specific parameter validation, expose internal reason codes, treat an all-excluded basket as an engine failure, add interactive decision editing, or change comparison eligibility, equal-capital allocation, or Buy-and-Hold entry/exit behavior.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
| --- | --- | --- | --- |
| Recorded basket | Completed V2 Result with selected and non-selected decisions | Default-visible table in stored rank order, one-decimal returns, readable identities/outcomes | No error expected |
| No eligible members | Recorded selection with every decision excluded | Evidence table plus explicit `No securities qualified for the initial basket.` state | Remains a successful Result |
| Historical Result | Completed Result has no initial-selection evidence | Existing Result renders with compatible “not recorded for this historical run” state | No integrity failure |
| Unknown identity | Roster lacks a persisted decision's display identity | `Unknown security` and canonical ID remain visible | Do not hide the Result |
| Invalid top-X | Boolean, fractional, zero, or negative form value | Shared error attaches to `param__top_x`; submitted value remains available for correction | No launch/preparation |

</intent-contract>

## Code Map

- `skills/rtly-backtest-buy-and-hold/SKILL.md` -- canonical discovery metadata and user-facing top-X description.
- `app/api/templates/_strategy_configuration_fields.html` and `app/api/routes/strategy_manager.py` -- generic discovery-driven form, validation/error preservation, launch/restart context, and Result route context.
- `app/services/backtest/result_presenter.py` -- pure persisted-Result view models and identity-safe display formatting.
- `app/api/templates/_backtest_result.html` -- accessible completed-Result hierarchy and tables.
- `app/repositories/backtest_repo.py` and `app/services/backtest/strategy_protocol.py` -- immutable `InitialEntrySelectionV1` retrieval contract; inspect but do not change unless a compatibility defect is proven.
- `tests/test_strategy_manager_routes.py`, `tests/backtest/test_result_presenter_trade_log.py`, and `tests/backtest/test_strategy_manager_clean_checkout_journey.py` -- presentation, form/launch/restart, and clean-checkout regression coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/backtest/result_presenter.py` -- add immutable initial-basket view models and projection from `BacktestResultV1.initial_entry_selection`; format scores at one decimal, map the known Buy-and-Hold reason vocabulary to stable plain language, and preserve unknown IDs.
- [x] `app/api/routes/strategy_manager.py` and `app/api/templates/_backtest_result.html` -- provide and render the new view directly after Run identity; support recorded, all-excluded, and legacy-not-recorded states with existing accessibility/table conventions.
- [x] `skills/rtly-backtest-buy-and-hold/SKILL.md` -- clarify the 253-close eligibility/disqualification rule and user-facing trailing-return terminology without changing runtime semantics.
- [x] `tests/backtest/test_result_presenter_initial_basket.py` and `tests/test_strategy_manager_routes.py` -- cover deterministic rows, exact display copy, one-decimal score, plain exclusions, unknown identity fallback, all-excluded state, legacy compatibility, and concrete Buy-and-Hold top-X form validation/defaults.
- [x] `tests/backtest/test_strategy_manager_clean_checkout_journey.py` -- prove a `top_x=1` launch, manifest/Result preservation, equal-capital one-time entry/no exits, persisted decisions, restart equality, and final open-position marks.

**Acceptance Criteria:**
- Given Buy and Hold is configured, when its fields render or validation fails, then `top_x` defaults to 10, is a positive integer with clear help, and errors remain scoped to that field without affecting another Strategy form.
- Given a valid non-default `top_x`, when preparation, execution, completion, and restart occur, then the same canonical value appears in the manifest, Result, and restarted run.
- Given a completed Result with decisions, when it opens, then it shows the initial basket by default with required metadata, security identities, one-decimal trailing returns, rank, outcomes, and deterministic plain-language exclusions.
- Given every member is excluded for insufficient history or another valid cause, when the Result opens, then the exclusions are visible and the page states that no securities qualified rather than reporting failure.
- Given a historical Result without decisions, when it opens, then all existing Result content remains available with a compatible not-recorded state.
- Given clean-checkout fixture evidence, when Buy and Hold runs and restarts, then selection, allocation, no-exit behavior, persisted decisions, and final marks are reproducible.

## Design Notes

The Result presenter is a projection boundary, not a strategy implementation: it must never derive scores, infer eligibility, or look up live data. The 252-session window is fixed semantics of the stored metric ID; display that fixed definition rather than manufacturing calendar endpoints.

## Spec Change Log

## Review Triage Log

### 2026-08-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3 (high 0, medium 1, low 2)
- defer: 0
- reject: 0
- addressed_findings:
  - `[low] [patch]` Render the persisted metric version directly, avoiding `vv1` for a `v1` value.
  - `[low] [patch]` Distinguish insufficient-length history from invalid/missing price inputs in Buy-and-Hold documentation.
  - `[medium] [patch]` Format large finite persisted Decimal scores under a sufficient local precision so Result rendering cannot raise `InvalidOperation`.

## Verification

**Commands:**
- `uv run pytest tests/backtest/test_result_presenter_initial_basket.py tests/test_strategy_manager_routes.py tests/backtest/test_strategy_manager_clean_checkout_journey.py -q` -- expected: #371 presentation, form, launch, restart, and journey coverage passes.
- `uv run pytest tests/backtest/test_backtest_repo_results.py tests/backtest/test_backtest_worker.py tests/backtest/test_skill_discovery.py skills/rtly-backtest-buy-and-hold/scripts/tests -q` -- expected: persisted selection and all affected strategy contracts pass.
- `uv run ruff check app/services/backtest/result_presenter.py app/api/routes/strategy_manager.py tests` -- expected: no new lint findings.
- `uv run ruff format --check app/services/backtest/result_presenter.py app/api/routes/strategy_manager.py app/api/templates/_backtest_result.html tests` -- expected: formatting clean.
- `uv run pyrefly check app/services/backtest/result_presenter.py app/api/routes/strategy_manager.py` -- expected: no new type errors.
- `git diff --check` -- expected: no whitespace errors.

## Auto Run Result

Implemented the Story #371 Strategy Manager surface for the Buy-and-Hold top-X basket. Completed Results now explain their immutable initial selection before performance data, while historical Results remain compatible and all-excluded baskets are clearly successful no-entry outcomes.

Files changed:

- `app/services/backtest/result_presenter.py` -- initial-basket view models, score formatting, readable outcomes/exclusions, and legacy handling.
- `app/api/routes/strategy_manager.py` -- Result-page projection wiring.
- `app/api/templates/_backtest_result.html` -- default-visible accessible initial-basket table and empty/legacy states.
- `skills/rtly-backtest-buy-and-hold/SKILL.md` -- user-facing trailing-return and history eligibility explanation.
- `tests/backtest/test_result_presenter_initial_basket.py` -- projection, ordering, formatting, fallback, and large-Decimal coverage.
- `tests/test_strategy_manager_routes.py` -- Buy-and-Hold metadata/error and Result-page evidence coverage.
- `tests/backtest/test_strategy_manager_clean_checkout_journey.py` -- top-X launch, persisted Result, cancelled-run restart, and replay-equivalence journey.

Review findings: three patches applied (metric-version rendering, truthful history wording, and large-Decimal display precision); no deferred or rejected findings. Follow-up review recommendation: false; the review fixes were localized and regression-tested.

Verification: 147 focused presenter/route/clean-checkout tests passed; 157 repository/worker/discovery/Buy-and-Hold contract tests passed; scoped Ruff check and Python formatting passed; presenter Pyrefly passed with 0 errors; `git diff --check` passed. `pyrefly check app/api/routes/strategy_manager.py` retains two pre-existing errors at lines 235 and 1080 outside this change. Ruff does not support the checked Jinja/Markdown paths as formatter input, so those were inspected through their render/docs tests and diff check.

Residual risk: the Result presentation maps the known Buy-and-Hold exclusion vocabulary; an intentional future runtime reason code must add a corresponding plain-language mapping.
