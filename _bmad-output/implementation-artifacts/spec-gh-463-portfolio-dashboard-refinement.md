---
title: 'Compact chart-first portfolio dashboard and strategy-gated recommendations'
type: 'feature'
created: '2026-09-01'
status: 'review'
baseline_revision: 'c77d0bea'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/spec-gh-455-total-portfolio-value-chart.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** The Portfolio chart is oversized, sits below the headline values, and foregrounds several component lines while the headline Market Value excludes cash. Strategy assignment also consumes space with a redundant “No Strategy” badge and allows Recommendations before the prerequisite assignment exists.

**Approach:** Lead with a compact total-value chart, move the summary beneath it, make the Market Value headline include cash, and reduce the strategy area to a contextual select/change/repair control with Recommendations enabled only for an available assignment.

## Boundaries & Constraints

**Always:** Preserve the #455 same-snapshot total projection, chart ranges, fragment swaps, accessible chart name, supporting datasets and trade markers. Keep component series available through the legend even when visually subordinate by default. Reuse the existing strategy assignment modal and full-partial rerender so selecting a strategy immediately enables Recommendations. Treat unavailable assignments as selected-but-unusable and provide a repair path. Keep disabled state explicit in native markup and assistive copy.

**Block If:** Combined headline value cannot use the existing service-owned `total_value_gbp`, or enabling Recommendations requires new client state instead of the authoritative rerendered assignment view.

**Never:** Change snapshot persistence, recommendation evaluation, strategy assignment storage, chart arithmetic, refresh behavior, or Portfolio trade actions.

</intent-contract>

## Code Map

- `app/api/templates/_portfolio.html` -- dashboard order, combined headline value, assignment control and recommendation gate.
- `app/api/templates/_portfolio_chart.html` -- compact chart presentation and subordinate component defaults.
- `app/api/templates/index.html` -- scoped chart height and responsive styling.
- `tests/test_portfolio_template.py` -- hierarchy, value semantics and chart presentation contracts.
- `tests/test_strategy_assignment_routes.py` -- unassigned, assigned and unavailable partial-render states.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_portfolio.html` -- move chart above summary, render Market Value from combined total, collapse empty strategy state into Select strategy, place assigned label after Change/Repair, and gate Recommendations.
- [x] `app/api/templates/_portfolio_chart.html`, `app/api/templates/index.html` -- reduce chart height, keep Portfolio Value dominant, and default supporting series to hidden-but-toggleable with a compact bottom legend.
- [x] `tests/test_portfolio_template.py`, `tests/test_strategy_assignment_routes.py` -- cover hierarchy, combined value, compact chart, and all assignment-dependent action states.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml`, `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml` -- record #455/#463 review visibility with all follow-up scope consolidated on #463.

**Acceptance Criteria:**
- Given retained chart history, when Portfolio renders, then the compact Portfolio Value chart appears before the headline cards and component series remain available from its legend.
- Given holdings and cash, when the summary renders, then Market Value equals their existing service-owned combined GBP total and explicitly includes cash.
- Given no assignment, when the controls render, then Select strategy replaces the No Strategy badge and Recommendations is natively disabled with explanatory text.
- Given an available assignment, when the partial rerenders, then Change strategy appears before its strategy label and Recommendations is enabled.
- Given an unavailable assignment, when the partial renders, then Repair strategy and the unavailable label remain visible while Recommendations stays disabled.

## Verification

**Commands:**
- `uv run pytest -q tests/test_portfolio_template.py tests/test_portfolio_chart_route.py tests/test_portfolio_service.py tests/test_strategy_assignment_routes.py` -- focused behavior passes.
- `uv run ruff check` on changed Python tests -- passes.
- `git diff --check` -- passes.

**Manual checks:**
- Capture the Portfolio tab at desktop and narrow viewport; verify chart order/height, strategy states, and enabled/disabled recommendation affordance.
