---
title: 'Add six deterministic backtest Strategy Skills'
type: 'feature'
created: '2026-08-20'
status: 'done'
baseline_commit: 'de1339c7508f2b735d8360e9edd14365041d59b3'
review_loop_iteration: 0
context:
  - '{project-root}/docs/strategy-manager/strategy-authoring-v1.md'
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Strategy Manager has a complete replay engine but no production backtest Strategy Skills, so users cannot compare real methodologies or a passive benchmark.

**Approach:** Add six independently tracked, discoverable, parameterized long-only skills—Minervini, Weinstein, Darvas Box, Buy and Hold, Turtle Trend, and Moving Average—implemented against `StrategyProtocolV1` and delivered as one parallelized suite.

## Boundaries & Constraints

**Always:** Use one configured `security_id` and fixed-share sizing per run; decide on bounded close-of-session data and accept next-session-open fills; exclude the current bar from reference windows; require the target history's latest session to equal `view.as_of_session`; return the exact integral held quantity for SELL; keep every runtime helper inside its hashed `strategy.py`; create deterministic contract tests and `agents/openai.yaml`; preserve unrelated worktree changes.

**Ask First:** Change StrategyProtocolV1, discovery/source-manifest rules, engine behavior, the six agreed methodologies, or GitHub issue scope; introduce shared runtime modules, mutable strategy state, new dependencies, or multi-security/universe selection.

**Never:** Read future data, live accounts, repositories, agents, brokers, or order paths; invoke per-signal subprocesses or network APIs; pyramid, partially exit, simulate intraday stops, infer FX-aware percentage sizing, or modify active Story 2.9 files.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Minervini | Visible Stage 2 valid VCP breakout; score/trend/volume/pivot-extension gates pass | BUY; exit on configured loss or Stage 2/50-SMA failure | Missing/stale/short evidence emits no signal |
| Weinstein | Stage 2 daily/monthly evidence plus strict prior-high breakout and volume confirmation | BUY; exit on configured loss, non-Stage-2, or SMA150 failure | Insufficient daily/weekly history emits no signal |
| Darvas Box | Close strictly breaks a valid prior N-bar box top with inclusive volume threshold | BUY; close strictly below current prior-window box bottom exits | Equality does not trigger price break |
| Turtle Trend | Current high strictly exceeds prior entry channel | BUY; current low strictly breaches prior exit channel | Each channel applies its own warm-up |
| Moving Average | Fast SMA strictly crosses above/below slow SMA | BUY/SELL on true crossover only | Require slow window plus one; fast >= slow emits none |
| Buy and Hold | Active target session is on/after valid configured date | Deterministic BUY candidate; never SELL | Invalid date emits none; engine rejects later held-position candidates |
| Sizing | BUY or SELL signal | Fixed integer BUY shares; full integral held SELL quantity | Missing/fractional held quantity returns zero |

</frozen-after-approval>

## Code Map

- `skills/rtly-backtest-{minervini,weinstein,darvas-box,buy-and-hold,turtle-trend,moving-average}/` -- six isolated Skill folders with metadata, runtime, UI metadata, and tests.
- `app/services/backtest/{strategy_protocol,skill_discovery,backtest_engine,worker}.py` -- stable contracts consumed but not changed.
- `tests/backtest/test_skill_discovery.py` -- production-skill discovery/default integration coverage.
- `tests/backtest/test_strategy_runtime_import_boundary.py` -- parameterized safety check for every production runtime.
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` -- links issues #246–#251 and progress.

## Tasks & Acceptance

**Execution:**
- [x] `skills/rtly-backtest-minervini/` and `skills/rtly-backtest-weinstein/` -- scaffold with Skill Creator; implement scan-plus-daily-history rules, closed schemas, and contract tests.
- [x] `skills/rtly-backtest-darvas-box/` and `skills/rtly-backtest-turtle-trend/` -- implement prior-window breakout/channel rules and boundary tests.
- [x] `skills/rtly-backtest-moving-average/` and `skills/rtly-backtest-buy-and-hold/` -- implement crossover and passive benchmark rules, including documented V1 limitations.
- [x] `tests/backtest/test_skill_discovery.py` and `tests/backtest/test_strategy_runtime_import_boundary.py` -- prove all six discover cleanly and cannot reach forbidden imports.
- [x] `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` and GitHub issues -- keep each strategy's status externally visible.

### Review Findings

- [x] [Review][Patch] Keep Weinstein methodology self-contained and amend AD-2 for independently releasable skills [_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md:101]
- [x] [Review][Patch] Enforce a closed runtime import allowlist and reject relative or dynamic imports [tests/backtest/test_strategy_runtime_import_boundary.py:26]
- [x] [Review][Patch] Allow Minervini and Weinstein price-risk exits when scan evidence is unavailable [skills/rtly-backtest-minervini/scripts/strategy.py:154]
- [x] [Review][Patch] Reject SELL signals for zero-quantity positions [skills/rtly-backtest-minervini/scripts/strategy.py:160]
- [x] [Review][Patch] Reject zero-volume reference baselines for volume-confirmed entries [skills/rtly-backtest-darvas-box/scripts/strategy.py:117]
- [x] [Review][Patch] Validate only price evidence required by Buy and Hold [skills/rtly-backtest-buy-and-hold/scripts/strategy.py:35]
- [x] [Review][Patch] Assert deterministic live discovery ordering [tests/backtest/test_skill_discovery.py:95]
- [x] [Review][Patch] Load every live strategy through the production worker loader [tests/backtest/test_skill_discovery.py:95]

**Acceptance Criteria:**
- Given the repository `skills/` root, when discovery runs, then all six IDs appear in deterministic order with valid defaults and no warnings attributable to them.
- Given identical bounded views and parameters, when any runtime is called repeatedly, then validated signals and sizing are identical.
- Given empty, short, stale, equality-boundary, malformed-date, or fractional-position inputs, when rules run, then they fail closed as defined in the matrix without live access or mutation.
- Given the complete suite, when focused and repository quality checks run, then tests, lint, formatting, typing, and import-boundary checks pass without altering unrelated work.

## Spec Change Log

## Design Notes

Discovery hashes only each `scripts/strategy.py`; therefore calculation helpers stay in that file. Buy and Hold cannot see run start or portfolio state in `entry_signals`, so it emits repeat deterministic candidates after its cutoff and relies on the engine's position-conflict rule after the first fill. Generic Skill Creator validation rejects this repository's required Strategy frontmatter extensions; repository discovery is the authoritative final validator.

Use `security_id: sec-aapl` and `fixed_shares: 10` (range 1–100000) as common defaults. Strategy-specific defaults are: Minervini `minimum_vcp_score: 70`, `minimum_trend_score: 85`, `minimum_relative_volume: 1.5`, `maximum_pivot_extension_pct: 3`, `maximum_loss_pct: 8`; Weinstein `breakout_lookback_sessions: 50`, `minimum_relative_volume: 1.5`, `maximum_loss_pct: 10`; Darvas `box_lookback_sessions: 20`, `maximum_box_depth_pct: 15`, `volume_multiplier: 1.5`; Turtle `entry_lookback_sessions: 20`, `exit_lookback_sessions: 10`; Moving Average `fast_window: 50`, `slow_window: 200`; Buy and Hold `entry_on_or_after: 2000-01-01`.

Minervini entry additionally requires Stage 2, `valid_vcp`, `trend_template_passed`, `Breakout`, breakout volume, scores at least their minima, close from pivot through its maximum extension, and current volume at least the configured multiple of the prior 50-session mean; exit at loss threshold, below SMA50, non-Stage-2, Invalid, or Damaged. Weinstein entry requires visible and daily Stage 2, close strictly above the prior lookback high, and current volume at least the prior-50-session multiple; exit at loss threshold, non-Stage-2, or below SMA150. Threshold score/volume/depth equality qualifies; price breakout/cross/channel equality does not.

## Verification

**Commands:**
- `uv run pytest skills/rtly-backtest-*/scripts/tests -q` -- all strategy contracts pass without module-name collisions.
- `uv run pytest tests/backtest/test_skill_discovery.py tests/backtest/test_strategy_runtime_import_boundary.py -q` -- discovery and safety integration pass.
- `uv run pytest -q` -- full suite passes.
- `uv run ruff check . && uv run ruff format --check .` -- lint and formatting pass.
- `uv run pyrefly check` -- type checks pass.
- `git diff --check` -- patch has no whitespace errors.

## Suggested Review Order

**Strategy rules**

- Start with the richest scan-plus-daily breakout strategy and its fail-closed gates.
  [`strategy.py:84`](../../skills/rtly-backtest-minervini/scripts/strategy.py#L84)

- Review self-contained Stage 2 classification and breakout confirmation.
  [`weinstein/strategy.py:132`](../../skills/rtly-backtest-weinstein/scripts/strategy.py#L132)

- Inspect prior-box breakout and breakdown rules with strict price boundaries.
  [`darvas/strategy.py:70`](../../skills/rtly-backtest-darvas-box/scripts/strategy.py#L70)

- Inspect independent Turtle entry and exit channel windows.
  [`turtle/strategy.py:87`](../../skills/rtly-backtest-turtle-trend/scripts/strategy.py#L87)

- Review true moving-average crossover detection and invalid-window handling.
  [`moving-average/strategy.py:104`](../../skills/rtly-backtest-moving-average/scripts/strategy.py#L104)

- Review the deterministic passive benchmark and finite-history validation.
  [`buy-and-hold/strategy.py:69`](../../skills/rtly-backtest-buy-and-hold/scripts/strategy.py#L69)

**Discovery and safety**

- Confirm all expected strategies discover cleanly without blocking future additions.
  [`test_skill_discovery.py:95`](../../tests/backtest/test_skill_discovery.py#L95)

- Verify import safety automatically covers every discoverable production runtime.
  [`test_strategy_runtime_import_boundary.py:57`](../../tests/backtest/test_strategy_runtime_import_boundary.py#L57)

**Tracking**

- Map each independently reviewable strategy to its GitHub issue.
  [`github-bmad-tracking.yaml:69`](github-bmad-tracking.yaml#L69)
