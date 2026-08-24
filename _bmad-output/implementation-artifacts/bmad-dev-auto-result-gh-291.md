---
status: done
---

# BMad Dev Auto Result

Status: done

## Summary

- Added service-computed win/loss counts to the Realised P&L summary. Resolved break-even round-trips count as wins; FX-unavailable rows count as neither.
- Kept ticker subtotals visible and moved their round-trip rows into collapsed native disclosures with accessible labels and non-conflicting headers.
- Made FX-only ticker subtotals explicitly unavailable rather than presenting them as break-even.

## Files changed

- `app/schemas/realised_pnl.py` — backward-compatible win/loss count fields.
- `app/services/realised_pnl_service.py` — FX-aware count calculation.
- `app/api/templates/_realised_pnl.html` and `app/api/templates/index.html` — summary balance, collapsed detail presentation, and nested-table styling.
- `tests/test_realised_pnl_service.py` and `tests/test_realised_pnl_route.py` — calculation and rendered-fragment regressions.
- `spec-gh-291-realised-pnl-collapse-summary.md` — implementation contract and review record.

## Review

- Applied 10 localized review fixes: accessibility labels, nested-header behavior, count grammar/state styling, FX-only subtotal clarity, disclosure presentation, and stronger rendering coverage.
- Deferred: none. Rejected: none.
- Follow-up review recommended: true, because review-driven changes span display behavior, accessibility, and test coverage.

## Verification

- `uv run pytest -q tests/test_realised_pnl_service.py tests/test_realised_pnl_route.py` — 75 passed, 1 warning.
- `uv run ruff check app tests` — passed.
- `uv run pyrefly check app/schemas/realised_pnl.py app/services/realised_pnl_service.py` — 0 errors.
- `uv run pytest -q` — full 1,933-test suite passed.
- `git diff --check` — passed.

Residual risk: native disclosure behavior is browser-provided; keyboard and screen-reader semantics use standard HTML elements and labels.
