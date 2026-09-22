# Handoff — GH-601 cache-performance work (resume in fresh context)

## Current state

- Repository: `/Users/me/Git/Agents.stocks`
- Branch: `feat/refresh-warnings-in-menu`
- Stories in scope: GH-603, GH-604, GH-605, GH-606 under feature GH-601.
- The working tree contains the implementation and is intentionally uncommitted. Do not reset or discard it. `diagram/` is an unrelated untracked directory from an earlier diagram request; leave it in place.
- BMAD tracking currently keeps feature 601 and Story 601.4 (issue 606) **in-progress** because benchmark AC4 is not closed.
- Storage work for GH-444 remains follow-up work; GH-602 is the bounded historical-evidence follow-up.

## What is implemented

The current diff adds run-scoped derived-data preparation and bounded Strategy reads:

- Prepared market planes are shared between the Engine and `MarketView` for one Run.
- `MarketView.price_history()` supports bounded row/column reads with session bounds.
- Minervini and Weinstein reuse run/month scan state.
- FX closes are decoded and validated once per Run.
- Worker-owned planes, scan maps, and month state are cleared on every terminal path.
- All six Strategy runtimes use the bounded views. Buy-and-Hold keeps its selection path separate from unnecessary full-history reads.
- Split-created fractional holdings use exact `Decimal` quantities. New BUY fills remain whole shares; SELL closes the complete held quantity, including fractional shares. Staging tracks entry, split, mark, and exit quantities.
- `ExitFillEventV1` and `OpenPositionMarkEventV1` preserve legacy integral share JSON as numbers while retaining fractional values exactly.
- Runtime identity is `backtest_engine.v6` / `strategy_protocol.v4`. Worker resolution now verifies the complete execution-contract digest: source digests, numeric policy, lockfile, and calendar identity.
- The shared regime filter widens bounded history until enough finite closes are available or the pinned evidence is exhausted.

Primary implementation files are under `app/services/backtest/`, especially:

- `backtest_engine.py`
- `worker.py`
- `run_input_manifest.py`
- `market_planes.py`, `market_view.py`, `scan_view.py`, `currency.py`, `corporate_actions.py`, `regime_filter.py`, and `strategy_protocol.py`

Strategy runtime and contract changes are under `skills/rtly-backtest-*/`.

## Evidence and benchmark status

The local benchmark artifact is `_bmad-output/implementation-artifacts/benchmark-601-2026-09-12.md`. It covers the approved 738-security, January 2025–July 2026 reference period.

Successful real staging replays under the final runtime:

| Mode | Wall time | Events | Final positions |
| --- | ---: | ---: | ---: |
| Buy and Hold | 119.5 s | 33 | 10 |
| Darvas | 202.5 s | 4,687 | 131 |
| Moving Average | 351.4 s | 1,131 | 61 |
| Minervini | 547.5 s | 0 | 0 |
| Minervini upgrade | 534.3 s | 0 | 0 |
| Turtle Trend | 355.262 s | 49,851 | 331 |
| Weinstein | 818.164 s | 1,891 | 59 |
| Weinstein upgrade | 855.140 s | 1,891 | 59 |

Each successful run published 406 sessions and removed staging after atomic promotion. The missing 2 March 2026 observations for NXT.L and SMIN.L now carry their prior as-traded close into fills; provider evidence remains unchanged and still rejects Yahoo's invalid OHLC geometry.

The read-workload matrix for all six Strategies and upgrade modes is also in the benchmark artifact. It is post-change only. A matched pre-change baseline is still missing, so strict AC4 (“before/after improvement” and complete reference runs) remains open.

## Review and quality gates

The final BMAD Sol review found no remaining code findings. It confirmed that AC4 is still unmet and that the story must remain in progress.

Most recent checks:

- Full non-browser pytest: **3,244 passed, 42 warnings**.
- Backtest plus six Strategy suites: **1,316 passed, 2 warnings**.
- Focused engine/worker/manifest tests: **95 passed**.
- Regime-filter tests: **17 passed**.
- Ruff: passed on changed backtest and Strategy files.
- `git diff --check`: passed.
- Pyrefly: **38 existing baseline errors**, outside the changed backtest/Strategy modules.

## GitHub updates

Sanitized benchmark and status updates were posted to:

- Issue 601: https://github.com/ed-is-ai/Agents.stocks/issues/601
- Issue 606: https://github.com/ed-is-ai/Agents.stocks/issues/606

The comments state that all Strategy and upgrade staging modes are complete; AC4 remains in progress only because the matched pre-change baseline has not yet been captured.

## Next agent actions

1. Start by reading this handoff, `git diff`, the active Story 601.4 artifact, and the benchmark artifact. Preserve all current edits.
2. Obtain a matched pre-change baseline to close strict AC4, or record an approved follow-up that keeps 601.4 open. Do not mark the story complete from the current evidence.
3. Do not add rolling arithmetic or staging-shape changes without measured materiality. The current measurements did not justify either optimization.
4. Re-run the relevant tests and quality gates after any change, update the BMAD status files, and post a sanitized GitHub update.

Avoid destructive operations on the large local databases under `data/`; they are the durable benchmark evidence stores used by the replay scripts.
