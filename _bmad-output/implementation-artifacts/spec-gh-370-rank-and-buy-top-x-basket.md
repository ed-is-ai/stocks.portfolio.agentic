---
title: 'Rank and buy the strongest top-X basket'
type: 'feature'
created: '2026-08-29'
status: 'done'
baseline_commit: 'b9dc757be11168c48d7badacef4106a5a829e06b'
github_issue: 370
parent_issue: 366
depends_on: [369, 368]
context:
  - '{project-root}/_bmad-output/planning-artifacts/feature-gh-366-buy-and-hold-top-x-strength.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-gh-369-initial-ranked-selection.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-gh-368-equal-capital-allocation.md'
---

<intent-contract>

## Intent

**Problem:** Buy and Hold currently emits repeat, fixed-universe BUY candidates. Although the engine now owns equal-capital allocation, the Strategy neither selects a point-in-time momentum basket nor records why a security was selected or excluded.

**Approach:** Make Buy and Hold an `InitialEntrySelectionProviderV1`. On the engine-owned first normalized union session, use only each pinned security's bounded, split-continuous `MarketView.price_history` to compute a 252-session price return, rank all eligible securities deterministically, return one complete decision batch, and let the already-merged #368 engine allocate its selected BUY cohort. Do not alter the engine's allocator or add Strategy-owned share arithmetic.

## Boundaries & Constraints

**Always:** Declare `top_x` as a positive plain integer with default `10`; require 253 valid close observations before the first selection session; calculate `last_close_before_D / close_252_security_sessions_earlier - 1` using `Decimal` and deterministic conventions; select by `(score descending, canonical security_id ascending)`; return exactly one decision per pinned security and BUY signals only for selected decisions; preserve no-lookahead, split-continuous history, next-MIC fills, #368 allocation, corporate actions, dividends, final marking, and replay behavior.

**Block If:** #368's engine-owned equal-capital allocator is absent, a change needs a second price plane, repository/network/live-account access from the Strategy, a migration/rewrite of historical Results/manifests, or a new rebalancing/sell/allocation policy.

**Never:** Use provider adjusted closes, current/future D rows, floats, `fixed_shares`, input/insertion ordering, a mutable “already selected” flag, ordinary recurring BUY signals, SELL signals, reranking, rebalancing, pyramiding, or Buy-and-Hold-specific allocation arithmetic.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected output | Error behavior |
| --- | --- | --- | --- |
| Valid history | 253+ split-continuous finite positive closes strictly before D | eligible ranked decision; selected if in top X | signal uses `buy_and_hold_top_x_entry_v1` only when selected |
| Tied scores/reordered universe | More eligible securities than X | score descending, then security ID ascending; byte-identical batch | none |
| Short/missing/invalid history | <253 usable rows; endpoint absent, NaN, infinity, zero, or negative | one excluded decision with stable reason | rest of universe still ranks |
| Zero eligible | every pinned security excluded | complete valid selection, no BUY signals/trades | successful Run, not a fatal error |
| Cutoff after first union session | `entry_on_or_after` is later than selection D | all members excluded with a stable cutoff reason | no later retry/rerank |
| Later sessions | selection has already returned | `entry_signals` remains empty; `exit_signals` remains empty | selected positions hold; exclusions never enter |
| Multi-MIC/currency basket | selected securities fill on different next sessions | engine's #368 reservation/whole-share policy decides quantities | no Strategy sizing callback for BUY |

</intent-contract>

## Story

As a backtest user,
I want Buy and Hold to purchase the strongest configurable top-X basket with equal capital,
so that it is a meaningful, reproducible passive momentum benchmark.

## Acceptance Criteria

1. Given omitted `top_x`, when Buy and Hold metadata is discovered and a Run is prepared, then the canonical validated value is `10`; a Boolean, non-integer, zero, or negative value fails at the shared parameter-validation boundary.
2. Given security history, when selection occurs on D, then its score uses the last close strictly before D and the close 252 security sessions earlier. Changing D or later rows cannot affect the score.
3. Given insufficient/missing history or a non-finite/non-positive endpoint, when ranking runs, then that security receives one stable excluded decision and other universe members continue.
4. Given more than X eligible securities, ties, and reordered inputs, when selection repeats, then exactly X are selected by return descending then canonical security ID ascending and the decisions are byte-identical.
5. Given X or fewer eligible securities, including zero, when selection runs, then all eligible members are selected; zero completes successfully without trades.
6. Given a selected basket, when fills execute, then #368's shared allocator splits available capital under its existing deterministic policy; Buy and Hold has no fixed-share BUY sizing path and rounding/unaffordable events stay auditable in the Trade Log.
7. Given later sessions, price changes, split/dividend actions, or absent exits, when simulation continues, then initial selection is not invoked again, excluded securities never enter, selected positions are held, and existing action/dividend/final-mark policies apply.
8. Given identical pinned manifest and evidence, when the Run restarts or replays, then selection scores/decisions, allocation quantities, fills, residual cash, and open-position marks are identical.

## Tasks / Subtasks

- [x] Update Buy and Hold's public contract and capability (AC: 1, 6, 7)
  - [x] Add `top_x` metadata with `type: integer`, `default: 10`, and `minimum: 1`; retain `entry_on_or_after` only as an explicit first-session eligibility constraint.
  - [x] Replace recurring `entry_signals` with an empty return and implement `initial_entry_selection(view, parameters)` using `InitialEntrySelectionV1`, `EntrySelectionDecisionV1`, and `EntrySelectionState`.
  - [x] Keep empty `exit_signals`; BUY `position_size` returns `0` because #368 is the only BUY-sizing authority; retain defensive integral full-SELL sizing for protocol completeness.
  - [x] Amend the Skill prose so consumers know the selection is one-shot on the first normalized union session, requires a 252-security-session lookback, records exclusions, and delegates allocation to the engine.

- [x] Implement pure deterministic strength ranking and decision construction inside the production Strategy runtime (AC: 2–5, 7–8)
  - [x] Read `view.price_history(security_id)` once per canonical universe member; use only indexed sessions strictly `< view.as_of_session`, never `== D` or later.
  - [x] Require 253 valid, finite, positive `Decimal` close rows ending before D. Use the last as numerator and index `-253` as denominator, then calculate/serialize the finite Decimal return without float conversion.
  - [x] Assign stable exclusion codes for cutoff, missing/short history, invalid close column/endpoint, and malformed/unavailable bounded history; ensure excluded decisions have no score and selected/eligible-not-selected decisions have valid scores.
  - [x] Order eligible members by score descending then ID ascending, choose `min(top_x, eligible_count)`, and assign all decisions a unique contiguous rank in the canonical order expected by `validate_initial_entry_selection`.
  - [x] Return metric/rule identities that are stable and specific to this formula/version. Build canonical selected BUY signals whose session/rule exactly match the selection header.

- [x] Prove the production Skill contract and shared parameter behavior (AC: 1–5, 7)
  - [x] Replace repeat-candidate tests with one-shot selection capability tests; assert no ordinary entries/sells and #368-compatible BUY sizing.
  - [x] Add parameter discovery/launch/manifest tests for default normalization and invalid `top_x` values, including Boolean rejection.
  - [x] Add direct ranking tests for no-lookahead (D/future-row mutation), 253-close off-by-one behavior, ties, shuffled universe/history, X bounds, zero eligibility, and each stable exclusion code.
  - [x] Maintain the runtime import-boundary guarantee: the Skill imports only the approved Strategy protocol/standard-library dependencies, never repositories, live agents, network, broker, or order code.

- [x] Prove engine integration, deterministic replay, and non-regression (AC: 6–8)
  - [x] Exercise mixed XNYS/XLON-calendar and GBP-to-USD equal-capital allocation through #368's existing engine coverage, and prove the production Skill emits no later ordinary entries after its initial selection batch.
  - [x] Cover dividends/final open-position marking through existing engine coverage and a no-eligible successful production selection, while preserving #369 persisted decision evidence.
  - [x] Assert sealed-V2 restart manifest/selection continuity; run all six production Skill contract suites and manifest/result compatibility suites. Direct selection replay equality is covered by canonical deterministic ranking tests.

### Review Findings

- [x] [Review][Patch] Use a fixed local Decimal context for strength scores [skills/rtly-backtest-buy-and-hold/scripts/strategy.py:98]

## Dev Notes

### Existing seams to reuse

- `InitialEntrySelectionProviderV1` is runtime-checkable. The engine calls it once only on `union_sessions[0]`, validates full pinned-universe coverage plus decision/signal agreement, and otherwise uses ordinary `entry_signals`. A capable Buy and Hold must therefore provide a complete result on that first union session; do not add Strategy state to emulate one-shot behavior. [Source: app/services/backtest/backtest_engine.py#_process_signals; app/services/backtest/strategy_protocol.py#InitialEntrySelectionProviderV1]
- `validate_initial_entry_selection` sorts decisions by rank, requires ranks `1..N` across every decision (including exclusions), and derives expected BUYs only from `selected` decisions. Ensure all decision ranks are contiguous and all non-selected states carry no signal. [Source: app/services/backtest/strategy_protocol.py#validate_initial_entry_selection]
- `MarketView.price_history` exposes one split-continuous `Decimal` plane through `as_of_session`, ordered oldest-first. Filter D itself before reading lookback endpoints; selection must not reach into engine market planes or provider data. [Source: app/services/backtest/market_view.py#MarketView.price_history]
- #368 already reserves equal base-currency targets per BUY cohort, floors whole shares at each next-MIC open, and records `allocation_unaffordable`; do not change its semantics or calculate shares in the Strategy. [Source: app/services/backtest/backtest_engine.py#_process_signals; app/services/backtest/backtest_engine.py#_fill_buy]
- #369 persists a selection-bearing Result V2 atomically. This story only supplies valid production decisions through that seam; it must not change repository schemas/digests or fake Trade Log rows. [Source: _bmad-output/implementation-artifacts/spec-gh-369-initial-ranked-selection.md#Design Notes]

### Previous-story intelligence

- Story #369 deliberately kept all six production strategies behaviorally unchanged. This is the first production adopter and must preserve the exact legacy behavior of the other five Skills and V1/V2 historical Result compatibility.
- The #368 merged baseline already removed `fixed_shares` from production Skill metadata and made engine allocation authoritative. Do not restore the parameter or use a private replacement.

### File and test map

- Update: `skills/rtly-backtest-buy-and-hold/SKILL.md`
- Update: `skills/rtly-backtest-buy-and-hold/scripts/strategy.py`
- Update: `skills/rtly-backtest-buy-and-hold/scripts/tests/test_buy_and_hold_contract.py`
- Likely update/add coverage: `tests/backtest/test_skill_discovery.py`, `tests/backtest/test_backtest_launch_service.py`, `tests/backtest/test_run_input_manifest.py`, `tests/backtest/test_backtest_engine.py`, and `tests/backtest/test_backtest_worker.py`.
- Do not edit engine/repository/protocol implementation unless a failing, story-mapped integration test demonstrates an upstream contract defect; document and halt for a scope decision rather than silently redefining #368/#369.

### Quality gates

Run focused Buy and Hold, discovery/launch/manifest, engine/worker/replay, and all-six-production-Skill/import-boundary suites. Then run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, direct changed-module `uv run pyrefly check ...`, and `git diff --check`. No new dependency or web research is required: this uses pinned repository libraries and established seams.

### References

- [Source: _bmad-output/planning-artifacts/feature-gh-366-buy-and-hold-top-x-strength.md#Feature contract]
- [Source: _bmad-output/planning-artifacts/feature-gh-366-buy-and-hold-top-x-strength.md#Story 366.2 — Rank and buy the strongest top-X basket (#370)]
- [Source: _bmad-output/implementation-artifacts/spec-gh-369-initial-ranked-selection.md#Tasks & Acceptance]
- [Source: _bmad-output/implementation-artifacts/spec-gh-368-equal-capital-allocation.md#Tasks & Acceptance]

## Dev Agent Record

### Agent Model Used

GPT-5.6

### Debug Log References

- 2026-08-29: Created from the GH-366 feature plan after confirming GitHub #369 and #368 are closed/merged and #370 is open.

### Completion Notes List

- Story context created; implementation started from merged #368 baseline.
- Implemented production one-shot top-X selection, deterministic score/rank/exclusion handling, public metadata/documentation, and focused contract/discovery coverage.
- Added a sealed V2 preparation-to-worker integration test proving persisted production selection evidence and permitted restart continuity. Existing shared engine tests cover equal-capital cross-calendar allocation, FX direction, dividends, and final marking without altering #368/#369 behavior.
- BMAD code review fixed the ambient Decimal-context replay risk with a fixed local arithmetic context and regression coverage.
- Verification: 30 engine tests, 106 focused Buy-and-Hold/worker/discovery tests, 169 production-contract/import-boundary/manifest/result tests, and all 1,204 top-level test-module tests passed; scoped Ruff/format, direct Pyrefly, and `git diff --check` passed. The complete `pytest -q` channel was interrupted by the execution host without a result summary, so no single-command full-suite pass is claimed.

### File List

- _bmad-output/implementation-artifacts/spec-gh-370-rank-and-buy-top-x-basket.md
- _bmad-output/implementation-artifacts/github-bmad-tracking.yaml
- _bmad-output/implementation-artifacts/sprint-status.yaml
- skills/rtly-backtest-buy-and-hold/SKILL.md
- skills/rtly-backtest-buy-and-hold/scripts/strategy.py
- skills/rtly-backtest-buy-and-hold/scripts/tests/test_buy_and_hold_contract.py
- tests/backtest/test_backtest_worker.py
- tests/backtest/test_skill_discovery.py

## Change Log

- 2026-08-29: Created comprehensive implementation-ready story for GitHub #370 and started development.
- 2026-08-29: Completed implementation and verification; moved to review.
- 2026-08-29: BMAD code review completed; fixed deterministic Decimal-context finding and marked done.
