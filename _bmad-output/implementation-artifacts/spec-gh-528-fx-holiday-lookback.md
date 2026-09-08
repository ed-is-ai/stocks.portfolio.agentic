---
title: 'FX evidence lookback for non-trading snapshot dates'
type: 'bugfix'
created: '2026-09-08'
status: 'done'
baseline_revision: 'add484a2'
review_loop_iteration: 0
followup_review_recommended: false
final_revision: 'cf6d036'
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** Backtest evidence preparation pins non-GBP securities to an exact FX rate on the first day of the snapshot month. When that date is a weekend or market holiday, the provider chain has no quote, incorrectly reports a transient failure, and prevents preparation from succeeding.

**Approach:** Resolve a missing exact-date FX quote using the most recent provider quote on or before the requested date, bounded by a small calendar-day lookback. Preserve transient failures for genuinely fetchable data, while classifying an exhausted no-rate gap as definitive with an operator message that does not recommend retrying.

## Boundaries & Constraints

**Always:** Keep provider ordering, exact-date persistence semantics, deterministic series revision pinning, and negative-cache behavior intact. Use the nearest earlier published quote only when the requested date has no quote.

**Block If:** The existing provider interfaces cannot expose the observed quote date needed to persist and test the fallback safely.

**Never:** Add a maintained holiday-calendar dependency, use a later quote, or weaken validation for unsupported currency pairs.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Holiday snapshot start | Requested `2025-01-01`; prior provider quote on `2024-12-31` | Preparation succeeds and pins the prior quote for the affected securities | No error |
| Weekend snapshot start | Requested date is Saturday/Sunday; prior published quote is within lookback | Preparation succeeds using the nearest earlier quote | No error |
| Genuine unavailable pair/date | No quote in the bounded lookback and providers return no rate | Preparation fails definitively | Message says choose a later start month or equivalent, not retry |
| Transient provider outage | Provider chain raises while resolving the lookback | Preparation remains transient | Message continues to support retry |

</intent-contract>

## Code Map

- `app/services/backtest/backtest_launch_service.py` -- identifies missing pinned FX evidence, resolves provider misses, and shapes operator-facing failure messages.
- `app/integrations/fx_history.py` -- chained FX provider fetch behavior and quote-date semantics.
- `tests/backtest/test_backtest_launch_service.py` -- launch-service regression coverage for exact-date misses and outcome classification.
- `tests/test_fx_history.py` -- provider-chain behavior and quote parsing coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/backtest/backtest_launch_service.py` -- resolve each missing FX date by trying bounded earlier dates, retain the actual quote date in the persisted quote, and classify exhausted gaps as definitive -- holiday and weekend snapshot starts must prepare without retries.
- [x] `app/integrations/fx_history.py` -- expose or preserve the observed date required by the resolver without changing provider order or later-date behavior -- fallback must use a real published quote.
- [x] `tests/backtest/test_backtest_launch_service.py` and `tests/test_fx_history.py` -- cover holiday/weekend fallback, genuine unavailable dates, transient failures, and message classification -- prevent regression of the preparation workflow.

**Acceptance Criteria:**
- Given a January 2025 snapshot and no `GBPUSD=X` quote on `2025-01-01` but a quote on `2024-12-31`, when evidence is prepared, then it succeeds and pins the last trading-day rate on or before `2025-01-01`.
- Given a weekend or holiday snapshot-month start with an earlier quote inside the lookback, when evidence is prepared, then all affected securities share the resolved FX series revision and preparation succeeds.
- Given a requested pair/date with no provider quote throughout the bounded lookback, when evidence is prepared, then it fails definitively and the user-facing message does not instruct the operator to retry preparation.
- Given a transient provider failure while resolving the requested date/lookback, when evidence is prepared, then it remains a transient failure and does not create a definitive negative-cache entry.

## Spec Change Log

## Review Triage Log

### 2026-09-08 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 0, medium 2, low 0)
- defer: 0
- reject: 1: (high 0, medium 1, low 0)
- addressed_findings:
  - `[medium] [patch]` Preserve `FxUnsupportedPair` through the lookback helper so unsupported currencies are not reported as transient failures.
  - `[medium] [patch]` Limit the lookback to four prior calendar days so accepted fallback quotes remain within the downstream five-day freshness policy.

## Verification

**Commands:**
- `uv run pytest -q tests/backtest/test_backtest_launch_service.py tests/test_fx_history.py` -- expected: all relevant regression and provider-chain tests pass (60 passed).
- `uv run ruff check app/services/backtest/backtest_launch_service.py app/integrations/fx_history.py tests/backtest/test_backtest_launch_service.py tests/test_fx_history.py` -- expected: no lint errors.
