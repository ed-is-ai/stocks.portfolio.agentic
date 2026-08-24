---
status: done
---

# BMad Dev Auto Result

Status: done

## Summary

- Added nullable average win/loss percentage fields to the request-scoped Realised P&L summary.
- Calculated simple per-round-trip averages from the existing resolved GBP win/loss buckets: exact zero is a win and FX-unavailable placeholders are excluded.
- Added the average percentage stat card between Win / Loss and Unmatched Sells, with positive/negative styles and em dashes for missing buckets.

## Files changed

- `app/schemas/realised_pnl.py` — backward-compatible nullable average fields.
- `app/services/realised_pnl_service.py` — shared GBP bucket lists and simple percentage averages.
- `app/api/templates/_realised_pnl.html` — ordered average percentage stat card and missing-bucket display.
- `tests/test_realised_pnl_service.py` — no-result, wins-only, losses-only, mixed, break-even, and FX-exclusion coverage.
- `tests/test_realised_pnl_route.py` — rendered values, styles, missing state, and card-order coverage.
- `_bmad-output/implementation-artifacts/spec-gh-301-realised-pnl-average-win-loss.md` and `github-bmad-tracking.yaml` — implementation contract, review record, and issue status.

## Review

- Applied 2 low-severity review fixes: card-scoped missing-state assertions and field-adjacent nullable semantics.
- Deferred: none. Rejected: none.
- Follow-up review recommended: false.

## Verification

- `uv run pytest -q tests/test_realised_pnl_service.py tests/test_realised_pnl_route.py` — 78 passed, 1 warning.
- `uv run ruff check app tests` — passed.
- `uv run pyrefly check app/schemas/realised_pnl.py app/services/realised_pnl_service.py` — 0 errors.
- `uv run pytest -q` — 1,948 passed, 5 warnings.
- `git diff --check` — passed.

Residual risk: percentage presentation is intentionally rounded to one decimal in the UI while the summary retains the arithmetic mean as a float.
