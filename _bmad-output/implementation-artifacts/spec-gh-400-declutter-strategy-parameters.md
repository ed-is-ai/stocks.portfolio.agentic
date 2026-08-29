---
title: 'Declutter the Strategy & Parameters section of the backtest form (B2+B3)'
type: 'feature'
created: '2026-08-29'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
baseline_revision: '5be91010'
final_revision: 'e72b8ebd'
---

<intent-contract>

## Intent

**Problem:** In the backtest configuration form, every strategy radio label prints
the full parameter list with defaults ("Parameters: lookback (default 20), …") — noise
at the moment of *choosing*. After selection the Parameters section renders every
parameter as an always-expanded field, though most backtests run entirely on defaults.

**Approach:** (B3) Radio labels show only display name + description + a parameter
*count*. (B2) After a strategy is selected, non-required parameters collapse behind a
`<details>` disclosure with a summary line ("6 parameters · all defaults" /
"6 parameters · 2 changed"); required parameters render outside the disclosure, always
visible. All existing field markup, names, validation, and the per-strategy HTMX swap
are unchanged — this only regroups and hides.

## Boundaries & Constraints

**Always:**
- Submitted field names (`param__<name>`), input types, `id`s, `aria-describedby`
  wiring, and error markup are byte-for-byte the same as today — only their DOM
  container changes.
- Required parameters (`parameter.required == true`) render outside the disclosure.
- Non-required parameters render inside a single `<details>` disclosure, collapsed by
  default, natively expandable with no JavaScript.
- On a 422 re-render, if any parameter *inside* the disclosure has an error, the
  `<details>` renders with the `open` attribute so the error is visible.
- The disclosure `<summary>` carries a live count line: server renders
  "N parameters"; inline JS refines it to "· all defaults" or "· M changed" by
  comparing each control's current value to a `data-param-default` stamped on it, and
  updates it as the user edits. With JS off the bare "N parameters" count stands.
- If a strategy declares zero non-required parameters, no `<details>` renders (all
  parameters, if any, are outside it); if it declares zero parameters total, the
  section still renders its heading with "No parameters." as today's behaviour allows.
- The Parameters fieldset stays `data-wizard-step="1"` and inside `#strategy-fields`;
  the inline script is local to the swapped fragment (operates only on elements within
  `#strategy-fields`, binds listeners only to those inputs — no `document`/`body`
  listeners) so repeated HTMX swaps do not leak handlers.
- The wizard's Step-3 review summary (`_strategy_configuration.html` `parameterPairs`)
  must keep reading every `param__*` control whether or not it sits inside the
  collapsed `<details>` (it uses `querySelectorAll`, which already ignores
  `<details>` state — verify, do not regress).

**Block If:**
- (none — every `StrategyParameterV1.default` is a required non-nullable field, so the
  issue's "required parameter with no default" case cannot occur; required params are
  simply surfaced outside the disclosure.)

**Never:**
- No change to `_decode_launch_form`, `_configuration_context` data shape,
  `validate_strategy_parameters`, or the launch/enqueue path.
- No new route or endpoint; no server-side "changed count" diff logic (the count
  refinement is client-side only).
- No build step / framework — vanilla inline JS matching `_universe_selector.html`.
- Not in scope: B6/B7/B8 form polish (#401), the wizard structure itself (#399).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Strategy list renders | GET configuration | Each radio label = `<strong>name</strong> — description` + "N parameters" (or "No parameters"); no per-parameter default dump | n/a |
| Strategy selected, all params optional | e.g. moving-average | Parameters heading + `<details>` (collapsed) wrapping every field; summary "2 parameters · all defaults" after JS | n/a |
| Strategy selected, all params required | e.g. turtle-trend | Every field rendered outside the disclosure; no `<details>` element | n/a |
| Strategy selected, mixed | alpha (lookback required; 4 optional) | `lookback` outside; `<details>` wraps the other 4; summary "5 parameters · all defaults" | n/a |
| User expands + edits one optional value (JS on) | changes `threshold` | Summary becomes "5 parameters · 1 changed"; edited value persists through a 422 round-trip | n/a |
| 422 with an error on an optional (disclosed) param | `param__threshold` invalid | Fields partial re-renders with `<details open>`; the field shows `.is-invalid` + linked error; wizard opens Step 1 | Existing 422 path unchanged |
| JavaScript disabled | any | `<details>` still natively expandable; summary shows bare "N parameters"; all fields submit as before | n/a |
| HTMX strategy re-swap ×N | repeated radio changes | Each swap re-inits its own local listeners; no duplicate handlers, no console errors | n/a |

</intent-contract>

## Code Map

- `app/api/templates/_strategy_configuration.html` -- radio label (~line 43-46):
  drop the `<div class="text-muted small">Parameters: …</div>` default dump; replace
  with `{{ strategy.parameters|length }} parameter(s)` (or "No parameters").
- `app/api/templates/_strategy_configuration_fields.html` -- Parameters fieldset
  (~line 9-37): split the `{% for parameter in selected.parameters %}` loop into
  required (outside) and non-required (inside a `<details class="sm-param-disclosure">`
  with `<summary>` holding `<span id="param-summary">`); stamp
  `data-param-default="…"` on each control (raw default for number/string/boolean;
  the `enum_default_tokens[name]` token for enum); add `open` to `<details>` when a
  disclosed param has an error; add an inline `<script>` computing the changed count.
- `app/api/static/css/theme.css` -- a `.sm-param-disclosure > summary` rule (cursor,
  hairline, spacing) consistent with the existing `<details id="advanced">` styling.
- `tests/test_strategy_manager_routes.py` -- update/extend the Story 2.7 +
  "B1 staged wizard" sections.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_strategy_configuration.html` -- replace the per-parameter
  default dump in each strategy radio label with a parameter count.
- [x] `app/api/templates/_strategy_configuration_fields.html` -- render required
  params outside a `<details class="sm-param-disclosure">`; render non-required params
  inside it; `<details open>` when a disclosed param has an error; `data-param-default`
  on every control; `<summary>` with `<span id="param-summary">N parameters</span>`.
- [x] `app/api/templates/_strategy_configuration_fields.html` -- inline `<script>`
  (fragment-local): for each `[data-param-default]` control read its current value
  (checked radio value for enum/boolean groups), compare to the default, write
  "N parameters · all defaults" / "N parameters · M changed" into `#param-summary`,
  and re-run on `input`/`change` of any param control.
- [x] `app/api/static/css/theme.css` -- `.sm-param-disclosure` summary styling.
- [x] `tests/test_strategy_manager_routes.py` -- add tests for: radio label has no
  "(default" dump and shows the count; disclosed vs. outside placement for a mixed
  strategy (`lookback` outside, `threshold` inside `<details>`); `<details open>` on a
  422 with a disclosed-param error; `#param-summary` present; existing param
  validation + HTMX-swap tests still green.

**Acceptance Criteria:**
- Given the strategy list, when it renders, then each radio label contains the display
  name and description only, plus a parameter count, and no per-parameter default dump.
- Given a strategy is selected, when the Parameters section renders, then non-required
  parameters are inside a collapsed `<details>` whose summary states the parameter
  count, and (with JS) whether any value differs from its default.
- Given the user expands the disclosure and edits a value, when the control changes,
  then the summary line updates to "M changed" and the edited value survives a
  validation round-trip.
- Given a strategy declares a required parameter, when the section renders, then that
  parameter's field is visible without expanding the disclosure.
- Given a 422 re-render where a disclosed parameter has an error, when the fields
  partial renders, then the `<details>` is `open` and the error is visible.
- Given existing parameter validation and the per-strategy HTMX field swap, when a
  strategy is (re)selected or a form is submitted, then they behave exactly as before.
- Given JavaScript is disabled, when the section renders, then the disclosure is still
  natively expandable and all parameter fields submit as before.

## Design Notes

`StrategyParameterV1.default` is a required field — every parameter always has a
default, so "required parameter with no default" is impossible; `required` params are
handled purely by rendering them outside the disclosure.

The changed-count is client-side only. Server-side diffing would need to reproduce
enum-token / boolean-string / numeric-coercion comparison that already lives in
`_decode_parameter`; stamping `data-param-default` and letting one small script diff
live values (the same technique the wizard's Step-3 summary uses) avoids duplicating
that logic. No-JS users get the bare count, which the AC permits.

Enum/boolean groups: the "current value" is the `value` of the `:checked` radio in the
group `name="param__<name>"`; the default is `enum_default_tokens[name]` (enum) or
`'true'/'false'` (boolean) — stamp `data-param-default` on the group's wrapper or on
each radio and read it once per group.

Keep the inline script fragment-local (no `document.body` listeners) — it re-executes
on every `#strategy-fields` swap, so any body-level binding would accumulate.

## Verification

**Commands:**
- `uv run pytest tests/test_strategy_manager_routes.py -q` -- expected: all pass.
- `uv run pytest tests/ -q` -- expected: no regressions.
- `uv run ruff check app/ tests/` -- expected: clean.
- `uv run pyrefly check app/api/routes/strategy_manager.py` -- expected: no new errors
  (2 pre-existing at ~line 1006 are unrelated).
- `git diff --check` -- expected: clean.

## Spec Change Log

_No bad_spec loopback — intent contract held through review._

## Review Triage Log

### 2026-08-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6: (medium 1, low 5)
- defer: 2
- reject: 4
- addressed_findings:
  - `[medium]` `[patch]` Required params carried no accessible "required" signal (the
    `*` is `aria-hidden`, inputs have no `aria-required`) — added a
    `<span class="visually-hidden">(required)</span>` to required-param labels.
  - `[low]` `[patch]` Disclosure summary + JS hard-coded plural "parameters" — now
    "1 parameter" / "N parameters".
  - `[low]` `[patch]` `currentValue()` returned the first radio's value when no option
    was checked (possible after a stale-value round-trip) → phantom "1 changed"; it now
    returns `null` for an unchecked radio group.
  - `[low]` `[patch]` `param_default_token` could emit the literal `"None"` for an enum
    with no matching default token → `enum_default_tokens.get(name, '')`.
  - `[low]` `[patch]` Dead `data-param-type` attribute removed.
  - `[low]` `[patch]` `#param-summary` gained `aria-live="polite"`; tests tightened to
    the exact summary string, disclosure-closes-before-Period, and no `data-param-type`.
- deferred:
  - Editing params, switching strategy, and switching back (A→B→A) silently discards
    A's edits — pre-existing `hx-include` scope; the new "all defaults" summary makes
    the loss less obvious.
  - The disclosure `<summary>` shows the *total* parameter count (per the frozen I/O
    matrix and the "summary lives in `<summary>`" invariant), but the `<details>` only
    holds the non-required subset; for real strategies (which get 3 host-injected
    `regime_filter_*` optional params) this over-states what the disclosure hides.
    Resolving it needs a product decision that conflicts with the frozen contract.
- rejected: `--accent-2` token (it is defined in `tokens.css`); exotic numeric
  default-string normalization (`20.0` vs `20` — no shipped strategy declares such a
  default); error-link jumping to a *manually* re-collapsed `<details>`; latent
  `showStep` re-hide interaction (step-1 radio is the only swap trigger).

## Auto Run Result

Status: done

### Implemented change
Decluttered the backtest-config form's Strategy & Parameters area. (B3) Each strategy
radio label now shows a parameter *count* ("5 parameters" / "No parameters") instead
of dumping every parameter name and default. (B2) After a strategy is selected,
required parameters render directly while non-required ones collapse behind a
`<details class="sm-param-disclosure">`; the disclosure `<summary>` carries a live
count line ("N parameters · all defaults" / "· M changed") maintained by a
fragment-local inline script that diffs each control against a stamped
`data-param-default`. On a 422 the disclosure re-renders `open` when one of its params
errored. Field names, types, ids, `aria-describedby` wiring, error markup, validation
and the HTMX strategy swap are unchanged.

### Files changed
- `app/api/templates/_strategy_configuration.html` — radio label: parameter count in
  place of the per-parameter default dump.
- `app/api/templates/_strategy_configuration_fields.html` — `param_field` /
  `param_default_token` macros; required-vs-optional split; `<details>` disclosure with
  count summary + fragment-local diff script; `data-param-default` stamps;
  `visually-hidden` "(required)" label text; `aria-live` on the summary.
- `app/api/static/css/theme.css` — `.sm-param-disclosure` / `> summary` styling.
- `tests/test_strategy_manager_routes.py` — 3 new tests (count-not-dump label,
  required/optional split placement + disclosure boundary, `<details open>` on a 422
  disclosed-param error).

### Review findings breakdown
6 patches applied (1 medium a11y, 5 low), 2 deferred to `deferred-work.md`, 4 rejected.
No intent_gap, no bad_spec.

### Verification performed
- `uv run pytest tests/` — 2324 passed.
- `uv run ruff check app/ tests/` — clean.
- `uv run pyrefly check app/api/routes/strategy_manager.py` — 2 errors, both
  pre-existing on baseline `5be91010` (route file untouched).
- `git diff --check` — clean.
- Read-through of the rendered fields partial and the 422 fragment.

### Residual risks
- The disclosure summary counts total parameters, not just the hidden subset (deferred
  — see Review Triage Log); for a strategy with many required params it reads high
  relative to what expanding reveals.
- The changed-count diff is a string comparison; it is correct for every parameter
  type shipped today but a future strategy declaring an unusual numeric default string
  could show a spurious "changed".
- Wizard/disclosure interaction is only fully verifiable in a browser; route tests
  assert server markup and script contents.

### Follow-up review recommendation
`false` — the fixes are localized low-severity polish plus one additive a11y label on
one template; no behavior, API, data, or security surface changed.

**Manual checks:**
- Configure a Backtest: pick a strategy — radio labels are short; Parameters shows a
  collapsed disclosure with "N parameters · all defaults"; required params sit above
  it; expanding + editing flips the summary to "· M changed"; submitting an invalid
  disclosed param re-opens the disclosure with the error.
