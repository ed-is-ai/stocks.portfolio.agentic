---
title: 'Two-step wizard for backtest configuration (B1)'
type: 'feature'
created: '2026-08-29'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
baseline_revision: '6764c821'
final_revision: 'df295eea'
---

<intent-contract>

## Intent

**Problem:** The backtest setup screen (`_strategy_configuration.html`) renders five
stacked bordered fieldsets — Strategy, Universe, Parameters, Period, Capital — all
expanded at once, with "Run Backtest" buried below a long scroll. Everything
competes for attention and newcomers cannot tell what to do first.

**Approach:** Restructure the one existing form into a client-side wizard: Step 1
(Strategy + its Parameters), Step 2 (Universe + Period + Capital), Step 3 (read-only
Review + the single Run Backtest submit). Navigation is JS/HTMX show-hide over the
same form and the same single POST to `/strategy-manager/configuration` — no new
endpoints, no change to server validation or launch semantics.

## Boundaries & Constraints

**Always:**
- One `<form>`, one `action="/strategy-manager/configuration"`, one `type="submit"`.
- Only the final explicit submit starts a backtest; page load, strategy selection,
  and step navigation never do (preserve Story 2.7 guarantee).
- Server-side validation stays authoritative. The 422 re-render keeps returning the
  full form fragment with `#configuration-errors` linked to field ids.
- Progressive enhancement: with JavaScript disabled, every step's fields remain
  visible and the form submits and validates exactly as today.
- Step fields are grouped by a `data-wizard-step` attribute baked into the template
  markup (on each top-level `<fieldset>`), so HTMX partial swaps of `#strategy-fields`
  return already-tagged fieldsets.
- After a `#strategy-fields` HTMX swap, the wizard re-scans and keeps the user on
  their current step.
- On the 422 re-render, the wizard opens the earliest step containing a field error
  (detected via `.is-invalid` / `[aria-invalid="true"]` / `#configuration-errors`
  anchors) and moves focus to the error summary.
- Keyboard operable: Back/Next are real `<button type="button">`; step indicator
  conveys "Step N of 3" to assistive tech.

**Block If:**
- A backtest can span multiple strategies (would force Step 1 multi-select). Resolved
  for this spec: the launch command, form, and route are single-strategy
  (`strategy_id`, radio, "Choose a Strategy."); #368/#369 changed capital allocation
  within one strategy's cohort, not multi-strategy backtests. If implementation
  reveals a `strategy_ids` plural contract, HALT with blocking condition
  `multi-strategy backtest contract`.

**Never:**
- No new routes or split endpoints; no server-driven step state.
- No change to `_decode_launch_form`, `BacktestLaunchCommandV1`, or launch/enqueue.
- No build step or new JS framework — vanilla JS, matching
  `_universe_selector.html` / `_comparison.html` inline-script convention.
- Not in scope: B2/B3 parameter declutter, B6-B8 visual polish, B9 recall — separate
  issues. This spec only moves existing fields into steps.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Fresh load, JS on | GET `/strategy-manager/configuration` | Only Step 1 visible; step indicator "Step 1 of 3"; Next enabled, Back hidden/disabled | n/a |
| Fresh load, JS off | same | All fieldsets visible, Run Backtest at bottom (current behaviour) | n/a |
| Advance to Step 3 | user clicks Next twice | Read-only summary lists selected strategy, each parameter name=value, universe (whole universe or N selected), period start–end, capital + currency; single Run Backtest button below | Summary reads live field values |
| Strategy changed mid-wizard | radio change triggers `#strategy-fields` swap | New Parameters/Period/Capital fieldsets carry `data-wizard-step`; wizard stays on current step; hidden steps stay hidden | n/a |
| Submit with invalid capital | POST returns 422 full fragment | Wizard opens Step 2 (first error), `#configuration-errors` focused, field marked invalid; submitted values + strategy selection preserved | Existing 422 path unchanged |
| Coverage not initialized | `coverage` empty | Step 3 Run Backtest stays `disabled` with the existing reason text shown on Step 3 | n/a |

</intent-contract>

## Code Map

- `app/api/templates/_strategy_configuration.html` -- add wizard shell: step
  indicator, Back/Next controls, `data-wizard-step` on the Strategy and Universe
  fieldsets, the Review step wrapper containing the coverage warning + Run Backtest,
  and the inline wizard `<script>`.
- `app/api/templates/_strategy_configuration_fields.html` -- add `data-wizard-step="1"`
  to the Parameters fieldset and `data-wizard-step="2"` to the Period and Capital
  fieldsets; no logic change.
- `app/api/routes/strategy_manager.py` -- no code change expected; confirm the
  `/configuration/fields` partial still round-trips. Bump the
  `strategy-manager.js?v=` cache tag in `index.html` only if shared JS is touched
  (it should not be).
- `app/api/templates/index.html` -- no change unless a versioned asset tag is needed.
- `tests/test_strategy_manager_routes.py` -- extend the Story 2.7 configuration
  section with wizard markup + degradation + error-step tests.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_strategy_configuration_fields.html` -- tag Parameters
  fieldset `data-wizard-step="1"`, Period and Capital fieldsets `data-wizard-step="2"`.
- [x] `app/api/templates/_strategy_configuration.html` -- tag Strategy fieldset
  `data-wizard-step="1"`, Universe fieldset `data-wizard-step="2"`; wrap the coverage
  warning + Run Backtest button in a `data-wizard-step="3"` review region with an
  empty `#wizard-summary` container; add a `<nav>` step indicator ("Step N of 3",
  `aria-live="polite"`) and Back/Next `<button type="button">`; add the inline
  wizard script.
- [x] inline wizard script -- show one step at a time (default all-visible so no-JS
  works), Back/Next navigation, populate `#wizard-summary` from live field values on
  entering Step 3, re-scan on `htmx:afterSettle` for `#strategy-fields`, and on load
  jump to the earliest step containing `.is-invalid` / `[aria-invalid="true"]` and
  focus `#configuration-errors`.
- [x] `tests/test_strategy_manager_routes.py` -- add tests covering the I/O &
  Edge-Case Matrix rows that are server-observable (markup present, all steps
  visible in raw HTML for degradation, `data-wizard-step` on swapped fields partial,
  422 fragment still carries `#configuration-errors` and `data-wizard-step` on the
  strategy step, script contains the error-detection selectors).

**Acceptance Criteria:**
- Given the configuration screen with JS enabled, when it loads, then only Step 1's
  fields are visible with a visible step indicator and Next control.
- Given a strategy is selected, when Step 1 is shown, then that strategy's parameters
  appear within Step 1 and are absent before any selection.
- Given the user reaches the final step, when it renders, then a read-only summary of
  strategy, parameters, universe size, period and capital appears above the single
  Run Backtest button.
- Given the POST returns validation errors, when the fragment re-renders, then the
  wizard shows the step containing the first error with that error surfaced and
  focused, and submitted values survive.
- Given the page loads or a strategy is selected, when no explicit submit occurs,
  then no backtest is started.
- Given coverage is not initialized, when the final step renders, then Run Backtest
  is disabled with the reason shown.
- Given JavaScript is disabled, when the page renders, then all fields are visible
  and the form submits and validates as before.

## Design Notes

The parameter/period/capital fieldsets live inside `_strategy_configuration_fields.html`,
which HTMX swaps wholesale (`#strategy-fields`, `outerHTML`) on strategy change. Baking
`data-wizard-step` into that template — rather than assigning steps from JS by DOM
position — means the swapped-in fragment is already correctly grouped; the wizard
script only needs a `htmx:afterSettle` re-scan to re-hide the steps the user isn't on.

Inline `<script>` in the partial (not `strategy-manager.js`) matches the existing
form-scoped convention (`_universe_selector.html`) and re-executes automatically when
`#tab-content` is swapped, including the 422 re-render — so the error-step jump needs
no extra wiring.

Step grouping:
- Step 1: `fieldset` Strategy, `fieldset` Parameters
- Step 2: `fieldset` Universe, `fieldset` Period, `fieldset` Capital
- Step 3: `#wizard-summary` + coverage warning + Run Backtest

## Verification

**Commands:**
- `uv run pytest tests/test_strategy_manager_routes.py -q` -- expected: all pass,
  including new wizard tests.
- `uv run pytest -q` -- expected: no regressions.
- `uv run ruff format . && uv run ruff check .` -- expected: clean.
- `uv run pyrefly check app/api/routes/strategy_manager.py` -- expected: no new errors.
- `git diff --check` -- expected: no whitespace errors.

**Manual checks:**
- Load the Strategy Manager → Configure a Backtest tab: one step visible, Next/Back
  work, Step 3 summary matches entered values, Run Backtest submits once and redirects
  to the activity URL.
- Disable JS (or remove the script): all fieldsets visible, submit still works.

## Spec Change Log

_No bad_spec loopback — intent contract held through review._

## Review Triage Log

### 2026-08-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 8: (high 0, medium 5, low 3)
- defer: 1
- reject: 2
- addressed_findings:
  - `[medium]` `[patch]` `htmx:afterSettle` was bound on `document.body` on every
    partial swap — added a `document.body.dataset.smWizardBound` guard so it binds once.
  - `[medium]` `[patch]` Keyboard/SR focus was lost to `<body>` when Next/Back hid the
    focused button — `showStep` now moves focus to the step indicator (`tabindex="-1"`).
  - `[medium]` `[patch]` Universe (`security_ids`) errors sent the wizard to step 1
    because the alert sat outside any step and was not `.is-invalid` — moved the alert
    inside the step-2 Universe fieldset; `earliestErrorStep` now scans `.sm-alert-danger`,
    iterates by ascending step number, and drops the dead `#configuration-errors` check.
  - `[medium]` `[patch]` Coverage-not-initialized warning was hidden until step 3 —
    moved it out of the step-3 wrapper so it stays visible on every step.
  - `[medium]` `[patch]` Review summary showed raw wire tokens (enum index, `true`/
    `false`) and dropped params with no checked option — summary now uses the control's
    `<label>` text, renders unset params as `(unset)`, and distinguishes `(unavailable)`
    from `(none)` for period/capital.
  - `[low]` `[patch]` `aria-live` re-announced an identical "Step 1 of 3" on load —
    indicator text updates only when it changes.
  - `[low]` `[patch]` New wizard elements had no styling — added `.sm-wizard-indicator`
    / `.sm-wizard-controls` / `.sm-wizard-summary` rules to `theme.css`; Next primary,
    Back secondary.
  - `[low]` `[patch]` Weak/misleading tests — fixed "two-step" → 3-step wording,
    strengthened the no-JS test to assert no step opening tag carries `hidden`, added a
    test that a universe error renders inside the step-2 fieldset.
- deferred: `_universe_selector.html`'s inline script re-binds/re-queries on every swap
  with no idempotency guard (pre-existing, same class as the fixed wizard leak).
- rejected: per-step "Next" validation gating (spec keeps server validation
  authoritative); non-contiguous step DOM order (deliberate — `hidden` handles visual
  grouping, documented in Design Notes).

## Auto Run Result

Status: done

### Implemented change
The single long backtest-configuration form is now a client-side 3-step wizard over the
same one form and one POST: Step 1 Strategy + its Parameters, Step 2 Universe + Period +
Capital, Step 3 a read-only review summary above the single Run Backtest submit. Steps
are grouped by a `data-wizard-step` attribute baked into the template markup; an inline
vanilla-JS script shows one step at a time, provides Back/Next + a "Step N of 3"
indicator, builds the review summary from live field values, re-applies the active step
after the `#strategy-fields` HTMX swap, and on the 422 re-render opens the earliest step
containing an error and focuses the error summary. With JavaScript off every step stays
visible and the form submits exactly as before.

### Files changed
- `app/api/templates/_strategy_configuration.html` — wizard shell, step indicator,
  Back/Next, step-2/3 wrappers, inline wizard script; universe error alert moved inside
  the Universe fieldset; coverage warning kept visible on all steps.
- `app/api/templates/_strategy_configuration_fields.html` — `data-wizard-step` on the
  Parameters (1) and Period/Capital (2) fieldsets; no logic change.
- `app/api/static/css/theme.css` — `.sm-wizard-indicator` / `.sm-wizard-controls` /
  `.sm-wizard-summary` styles.
- `tests/test_strategy_manager_routes.py` — new "B1 staged wizard" section (7 tests).

### Review findings breakdown
8 patches applied (5 medium, 3 low — see Review Triage Log), 1 deferred to
`deferred-work.md`, 2 rejected. No intent_gap, no bad_spec.

### Verification performed
- `uv run pytest tests/` — 2321 passed.
- `uv run ruff check app/ tests/` — clean (`ruff format` does not touch `.html`/`.css`;
  the changed `.py` file is already formatted).
- `uv run pyrefly check app/api/routes/strategy_manager.py` — 2 errors, both pre-existing
  on baseline `6764c821` (line ~1006, unrelated to this change).
- `git diff --check` — clean.
- Manual DOM/markup inspection of the rendered partial and the 422 fragment.

### Residual risks
- Brief flash of step-2 fieldsets when the strategy radio changes: the swapped
  `#strategy-fields` fragment carries no `hidden` (required for no-JS), so step-2
  fieldsets are visible for one frame until `htmx:afterSettle` re-applies the step.
- Wizard interaction (focus moves, summary contents, step visibility) is only
  exercisable in a real browser; route tests assert server markup and script contents.

### Follow-up review recommendation
`true` — the wizard script was substantially reworked during review (focus management,
error-step routing, label-aware summary); an independent pass on the final JS is
worthwhile.
