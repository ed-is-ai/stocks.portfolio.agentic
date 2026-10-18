---
title: 'Engine: target-weight sizing and rebalancing (trims and top-ups)'
type: 'feature'
created: '2026-10-18'
status: 'done'
baseline_revision: 'dcdf6a1'
final_revision: '56b111a'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** A `Signal` is BUY/SELL with optional `priority`; the engine sizes every BUY as one equal-capital slot, rejects adding to an open position ("V1 forbids pyramiding"), and only fully exits. Strategies needing unequal or changing weights (Easy Rider 30/20/10/7.5, Momentum Rider BND 25%->50%) cannot be expressed (GH #118).

**Approach:** Add optional `Signal.target_weight` (like `priority`: default `None`, absent from `sort_key`). Strategies that opt in through a runtime-checkable capability protocol get a target-weight path in the engine: at each fill, the difference between target value and held value is traded in whole shares, trims/closes before top-ups/opens, remainder kept as cash. Strategies that do not opt in run the existing slot path untouched.

## Boundaries & Constraints

**Always:**
- Signals without `target_weight` replay byte-identically: same events, equity curve, candidate audits, final cash, and the same stored-result digest (`_canonical_result_payload`) for results written before this change.
- New optional event fields are omitted from `model_dump` when `None` (precedent: `RunInputManifestV1.terminal_exits`), so stored results still pass the read-time digest check.
- Opt-in is per strategy (`TargetWeightSizingV1` protocol, checked with `isinstance` like `InitialEntrySelectionProviderV1`). A non-opted-in strategy keeps no-pyramiding and full-exit-only rules.
- SELL/trim fills before BUY/add fills (existing `_fill_sort_key` SELL rank). Whole shares, floor. A BUY never spends more than cash.
- Weights outside [0, 1], or summing above 1 in one session, fail closed: no orders that session, one `SkippedSignalEventV1` per target signal with reason `target_weights_invalid`.
- `rebalance_tolerance` is declared only in the SKILL.md `parameters:` of opted-in strategies (not in `COMMON_BACKTEST_STRATEGY_PARAMETERS`), so manifests of other strategies are unchanged.
- Deterministic: same inputs give identical output.

**Block If:**
- Any edit to `backtest_engine.py` / `strategy_protocol.py` changes `execution_contract_digest` for new runs (both files are hashed source in `run_input_manifest._LEDGER_ACTION_METRICS_ALLOWLIST` / `_MARKET_VIEW_ALLOWLIST`), so new runs are not comparable with existing runs and queued runs pinned to the old contract fail resolution (`worker.py:1383`). The caller declared this a breaking change -> HALT until a human accepts it. **(Triggered at planning; see Auto Run Result.)**
- Implementation would need to edit any of the 7 guarded files in `.claude/CLAUDE.md`, `CANONICALIZER_VERSION`/`REQUEST_CONTRACT_VERSION`, or pandas/yfinance/python versions.

**Never:** fractional shares; a new trade-log `kind` (avoids the SQL CHECK migration); changing slot/equal-capital semantics; adding Microsoft Agent Framework; LLM calls.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Legacy | Existing fixture, no target weights | Output identical to pre-change golden sha256 | none |
| Empty book | Targets 0.30/0.20/0.50, cash only | Each held value within one share of target; leftover in cash | none |
| Trim + top-up | Held A above target, B below | A partially sold (ExitFill, position stays open), then B added (EntryFill); sell fill sequence precedes buy | none |
| Close | Held A, target 0 | A fully sold | none |
| Over 1 | Targets sum 1.05 | No orders; skips with `target_weights_invalid` | fail closed |
| Out of range | Weight -0.1 or 1.2 | Same as over 1 | fail closed |
| Drift band | Abs(current - target) <= `rebalance_tolerance` | No order | none |
| Not opted in | Strategy without capability emits `target_weight` | Skip `target_weight_not_enabled`; no order | fail closed |
| Cash short | Top-up costs more than cash after sells | Buy floor(cash / price) shares | none |

</intent-contract>

## Code Map

- `app/services/backtest/strategy_protocol.py` -- `Signal` (:135); add `target_weight`; add `TargetWeightSizingV1` protocol next to `InitialEntrySelectionProviderV1` (:461).
- `app/services/backtest/backtest_engine.py` -- `_process_signals` (:1489), `_preflight_buy` no-pyramiding (:1997), `_execute_buy`/`_execute_sell` (:1267/:1374), `PendingOrderV1` (:454), events (:271/:290), `SkipReasonCode` (:157).
- `app/services/backtest/worker.py:840-848` -- open-position reconstruction overwrites on EntryFill and pops on ExitFill; must become a running sum.
- `app/services/backtest/run_input_manifest.py:94-95` -- `ENGINE_VERSION`/`PROTOCOL_SCHEMA_VERSION` bump per convention (asserted in `tests/backtest/test_run_input_manifest.py:318`).
- `app/repositories/backtest_repo.py:8311,8618` -- result digest and event parser (must accept the new optional fields).
- `tests/backtest/test_backtest_engine.py` -- `_ScriptedStrategy`, `_manifest`, `_build_security`, `_run_capped` helpers.
- `tests/backtest/test_engine_replay_golden.py` -- golden digest recorded on unchanged main (commit dcdf6a1); must stay green unedited.

## Tasks & Acceptance

**Execution:**
- [x] `tests/backtest/test_engine_replay_golden.py` -- FIRST, on unchanged main, record sha256 of `SimulationOutputV1.model_dump(mode="json")` plus candidate audits (sorted-key JSON) for a capped, ranked fixture with a swap, a split and a dividend; hardcoded (`4aae3001...cb11`, commit dcdf6a1) -- proves byte identity.
- [x] `app/services/backtest/strategy_protocol.py` -- add `target_weight: Decimal | None = None` (not in `sort_key`) and `TargetWeightSizingV1` (`target_weight_sizing: Literal[True]`) -- opt-in seam.
- [x] `app/services/backtest/backtest_engine.py` -- route target-weight signals of opted-in strategies to a target path: validate session weights, schedule one pending order per security with `target_weight`; at fill compute equity from cash plus positions marked at this session's fill/latest price, delta = floor(target*equity/price) - held, apply tolerance, trim/close or top-up/open; record `target_weight`, `current_weight` on Entry/ExitFill (omitted when None); partial ExitFill keeps the position open with unchanged per-share basis; top-up recomputes weighted basis. New opens still count against `max_concurrent_positions`.
- [x] `app/services/backtest/worker.py` -- running-sum reconstruction of open positions.
- [x] `app/services/backtest/run_input_manifest.py` -- bump `ENGINE_VERSION` to v11, `PROTOCOL_SCHEMA_VERSION` to v7; update the asserting test.
- [x] `tests/backtest/test_target_weight_sizing.py` -- one test per I/O matrix row plus determinism (run twice, equal dumps) and a stored-result read-back test.

**Acceptance Criteria:**
- Given any existing strategy, when replayed, then the golden digest test passes unchanged.
- Given a result stored before the change, when read via `backtest_result()`, then the digest check passes.
- Given the same inputs twice, then outputs are equal.

## Spec Change Log

- 2026-10-18 -- Block If "execution_contract_digest comparability reset" was triggered at planning (run HALTed `blocked`). The user then explicitly accepted the reset via the coordinator: new runs get a new `execution_contract_digest`, old-vs-new runs become non-comparable, and runs queued under the old contract may fail. That Block If is resolved; follow the #35/#33 precedent (bump `ENGINE_VERSION`/`PROTOCOL_SCHEMA_VERSION`). Status set to ready-for-dev. KEEP: every other constraint (golden byte identity, read-time digest, opt-in, guarded files).

## Review Triage Log

### 2026-10-18 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 8 (high 0, medium 5, low 3)
- defer: 0
- reject: 15 (high 0, medium 0, low 15)
- addressed_findings:
  - `[medium]` `[patch]` `_others_value` fell back to the fill-session close for a holding with no earlier close (look-ahead); now valued at cost basis.
  - `[medium]` `[patch]` FX rounding of a whole lot could exceed cash and skip the whole BUY; now buys one share fewer until affordable.
  - `[medium]` `[patch]` target 0 inside `rebalance_tolerance` never closed a small holding; an explicit 0 now always closes.
  - `[medium]` `[patch]` trims after a fractional split sold fractional shares; trims now sell whole shares (a close still sells everything).
  - `[medium]` `[patch]` default tolerance 0 scheduled orders that whole-share rounding turns into no trade (skip and audit noise each session); no order now when the whole-share delta at the signal close is 0.
  - `[low]` `[patch]` a duplicate target counted in the sum check could void the whole session; the sum now counts the first target per security.
  - `[low]` `[patch]` NaN `rebalance_tolerance` raised InvalidOperation instead of SimulationError; now fatal with the stable message.
  - `[low]` `[patch]` target skips and audits carried the strategy-declared side instead of the traded side; schedule-time skips now use the traded side.
  - Rejected as by-design or documented: trims count as closed trades in win rate (Design Notes); slot-sized and target signals mixed in one session; cash/slots allocated in deterministic security order; omitted holdings are held (now in docstring); mixed price/FX dates; per-order equity; reversal reason code; 8dp basis drift; tolerance validation only for opted-in strategies; golden pins its recorded identities; worker reformat by ruff; worker republish (one publish per session); oversell in reconstruction; pending-close slot count (fill-time re-check exists); extra test-gap wishes.

## Design Notes

Side of a target order is decided at signal time from the signal-session close (above target -> SELL rank, below -> BUY rank) so the existing fill ordering frees cash first; quantity is re-derived at the fill open, and a flipped direction clamps to no trade. Trims count as closed trades in win rate (realized P&L event); acceptable for V1 and documented.

## Verification

**Commands:**
- `uv run pytest tests -q` -- expected: all pass
- `uv run ruff format <changed> && uv run ruff check <changed>` -- expected: clean
- `uv run pyrefly check` -- expected: no new errors

## Auto Run Result

First pass (planning): Status blocked -- execution-contract comparability reset unavoidable (both hashed files must change). Unblocked by explicit user acceptance; see Spec Change Log.

Second pass (implement + review): Status done.
- Summary: optional `Signal.target_weight` + opt-in `TargetWeightSizingV1`; the engine trades whole shares toward target weights (trims/closes before top-ups/opens, cash remainder, fail-closed weight validation, optional `rebalance_tolerance`); fills record `target_weight`/`current_weight`, omitted from dumps when unset; the worker rebuilds positions as a running sum; `ENGINE_VERSION` v11, `PROTOCOL_SCHEMA_VERSION` v7.
- Files: `app/services/backtest/strategy_protocol.py` (field, protocol, omit-None serializer helper); `app/services/backtest/backtest_engine.py` (target path); `app/services/backtest/worker.py` (running-sum positions); `app/services/backtest/run_input_manifest.py` (version bump); `tests/backtest/test_target_weight_sizing.py` (25 tests); `tests/backtest/test_engine_replay_golden.py` (golden; pins the v10/v6 manifest identities it was recorded under); `tests/backtest/test_point_in_time_runs.py`, `tests/backtest/test_run_input_manifest.py` (version pins/asserts).
- Review: 8 patches applied, 0 deferred, 15 rejected.
- Verification: `uv run pytest tests -q` 4492 passed; ruff format/check clean on changed files; `uv run pyrefly check` 106 errors (baseline 106, none in changed files); golden digest `4aae3001...cb11` unchanged.
- Residual risks: trims inflate win rate and closed-trade counts; mixing slot-sized and target signals in one session is not modelled; the FX rounding fallback has no dedicated test.
