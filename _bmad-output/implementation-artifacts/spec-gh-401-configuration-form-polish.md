---
title: 'Polish the Strategy Manager backtest configuration form'
type: 'feature'
created: '2026-08-30'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [multiple-goals]
github_issue: 401
baseline_revision: 'd3a9eeab'
---

<intent-contract>

## Intent

**Problem:** The backtest configuration form visually over-emphasises its fieldset containers, separates currency from capital, puts readiness/coverage messaging in a detached alert, and can hide the run action below a long form.

**Approach:** Present the existing configuration contract with lighter section chrome, a compact capital control, context-aware currency visibility, and a sticky action area. Preserve all form names, HTMX contracts, validation, and launch semantics.

## Boundaries & Constraints

**Always:** Keep `starting_capital`, `base_currency`, all existing hidden inputs, wizard markers, error accessibility attributes, HTMX targets/includes, POST validation, and the server-side launch guarantee intact. GBP remains the default. The active currency value must still be submitted when its visible selector is hidden. A disabled action must carry its coverage/readiness explanation without a separate red alert.

**Block If:** The available roster/selected-universe data cannot determine whether one currency is represented without changing a durable API or domain contract.

**Never:** Change database schema, route paths, field names, strategy parameter validation, coverage/readiness rules, or background-job behaviour. Do not make a visual-only issue silently expand to a readiness-service integration.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Single-currency selection | One selected or whole-universe market currency | Compact capital amount with its currency symbol; visible selector is hidden while its valid value remains posted | Retain GBP fallback when currency cannot be inferred client-side |
| Mixed-currency selection | Selected securities span currencies | Currency selector is visible alongside the capital control | Existing GBP/USD validation remains authoritative |
| Unavailable coverage | No usable coverage or coverage error | Sticky disabled Run Backtest action displays the reason in its helper/tooltip area | No standalone red warning alert is rendered |
| Invalid submission | Existing server-side form error | Form re-renders with values, field errors, and controls intact | No launch occurs |

</intent-contract>

## Code Map

- `app/api/templates/_strategy_configuration.html` -- configuration form shell, wizard steps, coverage disable state, and submit area.
- `app/api/templates/_strategy_configuration_fields.html` -- HTMX-swapped parameters, period, and capital controls.
- `app/api/templates/_universe_selector.html` -- selected-universe control and quote-currency data exposed to the browser.
- `app/api/static/css/theme.css` -- scoped Strategy Manager visual styles.
- `app/api/routes/strategy_manager.py` -- fixed form-field contract and configuration render/decode behaviour to preserve.
- `tests/test_strategy_manager_routes.py` -- configuration shell, no-JS, error, and field-preservation coverage.
- `tests/backtest/test_universe_selection_routes.py` -- universe-selection form integration coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_strategy_configuration.html` -- replace heavy section card treatment with semantic configuration section hooks; move the submit/review state into a sticky action region and express disabled coverage messaging there while preserving wizard and HTMX contracts.
- [x] `app/api/templates/_strategy_configuration_fields.html` and `app/api/templates/_universe_selector.html` -- render compact capital/currency controls and expose only presentation-level currency-selection state so single-currency selections infer a valid submitted currency and mixed selections reveal the selector.
- [x] `app/api/static/css/theme.css` -- add narrowly scoped responsive styles for hairline/divider-led form sections, capital input grouping, and an opaque sticky action bar without global control overrides.
- [x] `tests/test_strategy_manager_routes.py` and `tests/backtest/test_universe_selection_routes.py` -- assert the new form hooks/copy, inferred-versus-visible currency behaviour, disabled-action explanation, and unchanged successful/error form contracts.

**Acceptance Criteria:**
- Given a single-currency universe, when configuring capital, then capital and currency read as one compact control and the valid currency is submitted without showing a selector.
- Given selected securities span supported currencies, when configuring capital, then the currency selector is available and preserves the existing field name and validation values.
- Given a long configuration form, when the form scrolls, then Run Backtest remains visible in its sticky action area.
- Given coverage prevents launch, when the form renders, then Run Backtest is disabled with its reason attached in the action area and no standalone red coverage alert appears.
- Given any configuration form state, when it posts or is HTMX-swapped, then the existing values, validation errors, wizard behaviour, and one-launch guarantee remain unchanged.
- Given the form sections, when rendered at desktop or mobile widths, then lightweight headings/dividers replace the prior stack of heavy bordered cards without obscuring controls or errors.

## Design Notes

Use CSS `position: sticky`, not a fixed overlay, so the action remains part of normal no-JavaScript form flow. Client-side currency presentation may infer from the selected controls, but server-side `base_currency` validation remains the authority. The independently shippable B6/B7/B8 bundle is intentionally retained as one issue because its acceptance criteria share the same form shell.

## Verification

**Commands:**
- `uv run pytest tests/test_strategy_manager_routes.py tests/backtest/test_universe_selection_routes.py -q` -- expected: form, HTMX, submission, and universe-selection tests pass.
- `uv run ruff check app/api/routes/strategy_manager.py tests/test_strategy_manager_routes.py tests/backtest/test_universe_selection_routes.py` -- expected: no lint errors.
- `git diff --check` -- expected: no whitespace errors.

## Review Triage Log

### 2026-08-30 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3 (high 0, medium 1, low 2)
- defer: 0
- reject: 0
- addressed_findings:
  - `[medium]` `[patch]` Keep an invalid `base_currency` control visible instead of letting client-side inference conceal its field error.
  - `[low]` `[patch]` Include filtered selected-security hidden inputs in client-side currency inference.
  - `[low]` `[patch]` Render the currency prefix from the submitted USD/GBP value before JavaScript runs.

## Auto Run Result

Status: done

### Implemented change

Replaced the configuration form's stacked card treatment with scoped semantic
sections and hairline dividers. Starting capital is now a compact amount and
currency control. The existing `base_currency` select remains named and
submittable; browser-only presentation infers GBP or USD for a single-currency
selected or whole universe, while mixed or unsupported selections leave the
select visible and retain GBP as the fallback. The review and Run Backtest
control now sit in an opaque sticky action region. When coverage blocks launch,
the disabled button is described by the readiness reason in that region rather
than a separate warning alert.

### Review fixes

The final adversarial review made three localized currency-presentation fixes:
filtered selected inputs now participate in inference; the static input prefix
matches USD before JavaScript executes; and a server-side currency error keeps
the selector visible. No work was deferred and the changes do not warrant a
further independent review.

### Verification performed

- `uv run pytest tests/test_strategy_manager_routes.py tests/backtest/test_universe_selection_routes.py -q` — 181 passed after review fixes.
- `uv run ruff check app/api/routes/strategy_manager.py tests/test_strategy_manager_routes.py tests/backtest/test_universe_selection_routes.py` — clean.
- `git diff --check` — clean.
- `uv run pytest -q` — 2,393 passed.

### Deferred work

None.
