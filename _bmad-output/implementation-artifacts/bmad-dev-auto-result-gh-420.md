---
status: review
---

# BMad Dev Auto Result

Status: review

## Summary

- Centralized display-only backtest metric and currency formatting for the Result and results-list surfaces.
- Standardized Total Return and Win Rate to one decimal place in both surfaces; gain/loss values use the existing `.pos` / `.neg` presentation classes.
- Added Result-only signed P&L, derived from the final persisted equity-curve point minus persisted starting capital. No metric calculation, JSON, query, or storage value changes.
- Kept Max Drawdown Result-only and kept Win Rate as a rate (no win/loss counts).

## Decision record

- Rounded zero values render as neutral `0.0%` / zero money with no sign or colour. This follows the requested gain/loss treatment while avoiding a misleading positive or negative state caused by rounding.

## Files changed

- `app/services/backtest/result_presenter.py`
- `app/api/routes/strategy_manager.py`
- `app/api/templates/_backtest_result.html`
- `app/api/templates/_backtest_results_list.html`
- `tests/backtest/test_result_presenter_initial_basket.py`
- `tests/test_strategy_manager_routes.py`
- `github-bmad-tracking.yaml`

## Verification

- `uv run pytest -q tests/backtest/test_result_presenter_initial_basket.py tests/test_strategy_manager_routes.py` — 150 passed, 2 warnings.
- `uv run ruff check app/services/backtest/result_presenter.py app/api/routes/strategy_manager.py tests/backtest/test_result_presenter_initial_basket.py tests/test_strategy_manager_routes.py` — passed.
- `uv run pyrefly check app/services/backtest/result_presenter.py app/api/routes/strategy_manager.py` — blocked by two existing route-wide type errors outside this change (lines 238 and 1105 in `strategy_manager.py`).
- `git diff --check` — passed.

Deferred work: run the full suite during PR validation; no code changes deferred.
