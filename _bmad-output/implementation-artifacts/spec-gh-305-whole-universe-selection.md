---
title: 'Add "whole universe" selection option to Backtest launch form, default on'
type: 'feature'
created: '2026-08-25'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
baseline_revision: '4c6efc502c6582187dabcbc02f04be561c55ad88'
final_revision: 'd343c70332a5930059a0e95db5dbd76fb67e70ca'
---

<intent-contract>

## Intent

**Problem:** The Backtest launch form's Universe selector (Story 4.5) only supports picking securities one at a time via checkboxes; a fresh form starts with zero selected, and there is no way to say "use the whole active roster" without manually checking every box.

**Approach:** Add a "Whole universe" checkbox to `_universe_selector.html`, checked by default. When checked, the server resolves the submission to every security ID currently in the active roster (re-read fresh at submit time, not frozen from page load); when unchecked, the existing manual multi-select behaves exactly as today, including retaining whatever individual selections the browser already holds.

## Boundaries & Constraints

**Always:**
- Preserve every existing Story 4.5 validation: stale-profile detection (`profile_hash`/`activation_seq` mismatch), unknown-security rejection, empty-selection rejection, and canonicalization via `canonical_run_universe`.
- When "whole universe" is checked at submit time, resolve the security list server-side from `backtest.roster_member_identities(active.profile_hash)` at that moment — never trust a client-submitted list of 700+ hidden inputs for this mode.
- Keep the toggle a plain, no-JS-required HTML control for correctness (server must not assume JS ran); JS may enhance the visual list (hide/show) but must not be required for correct submission.
- Default state on a fresh (first) render of the configuration form is "whole universe" ON. Subsequent re-renders (search-as-you-type, Strategy change) must preserve whatever the user currently has set, not silently reset to the default.

**Block If:** none — this is UI-layer only, scoped per the prior decision not to add a new `universe_mode` contract value.

**Never:**
- Do not add a new `universe_mode` value to `StrategyUniverseContractV1`/`BacktestSubmissionV1` — stays at the form/submission layer, reusing `mode: "selected-securities"` with the full roster as the resolved set.
- Do not render 700+ checkboxes as individually `checked` when whole-universe is on — that's a DOM-cost path this feature must avoid, not lean into.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Fresh form load | GET `/strategy-manager/configuration`, no prior state | "Whole universe" checkbox rendered checked; manual picker hidden/collapsed | No error |
| Submit with whole-universe checked | POST with `whole_universe=true`, no `security_ids` | Canonical universe = every current roster ID for the active profile | No error |
| Submit with whole-universe checked, empty roster | `whole_universe=true`, roster has 0 members | Rejected same as today's empty-selection case | `security_ids` field error: "Select at least one security." |
| Toggle off, no manual selection yet | `whole_universe` absent/false, no `security_ids` | Same as today's existing empty-selection behavior | `security_ids` field error: "Select at least one security." |
| Toggle off after having it on, re-render (search) | htmx partial re-render with current checkbox state included | Manual picker state reflects what the user actually has, not reset to default-on | No error |
| Stale profile at submit | `whole_universe=true` but `profile_hash`/`activation_seq` no longer match active | Same stale-profile rejection as today | `security_ids` field error: "The active profile has changed..." |

</intent-contract>

## Code Map

- `app/api/templates/_universe_selector.html` -- add the "Whole universe" checkbox control and the show/hide behavior for the manual picker; checkbox must submit a plain `"true"`/absent value, not rely on JS for correctness.
- `app/api/routes/strategy_manager.py` (`universe_selector` GET route, ~L358) -- accept and thread through a `whole_universe` param so htmx re-renders (search) preserve current state instead of resetting to default.
- `app/api/routes/strategy_manager.py` (`_configuration_context`, ~L485) -- default `whole_universe` to `True` only on a genuinely fresh render (no prior value supplied).
- `app/api/routes/strategy_manager.py` (`submit_strategy_configuration`, ~L751-877) -- when `form.get("whole_universe")` is truthy, resolve `raw_security_ids` from `backtest.roster_member_identities(active.profile_hash)` instead of the submitted checkbox list, before the existing stale-profile/unknown-security/canonicalization logic runs.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_universe_selector.html` -- add "Whole universe" checkbox (checked per context default) that hides the search/list/chip UI when checked; include it in the search input's `hx-include` so re-renders don't lose its state -- lets users skip the manual picker entirely for the common "run against everything" case.
- [x] `app/api/routes/strategy_manager.py` -- `universe_selector` route: add `whole_universe: str | None = None` param, thread into the template context so partial re-renders reflect the caller's actual current state -- keeps search-as-you-type from resetting the toggle.
- [x] `app/api/routes/strategy_manager.py` -- `_configuration_context`: default `whole_universe` to `True` when not explicitly passed in `extra` -- gives new/fresh launches whole-universe by default per the issue's requirement.
- [x] `app/api/routes/strategy_manager.py` -- `submit_strategy_configuration`: when whole-universe is set, resolve `raw_security_ids` fresh from the roster before the existing validation/canonicalization block runs, and skip the "unknown securities" check in that branch (the server-resolved set is definitionally in-roster) -- keeps this mode from depending on client-submitted IDs.
- [x] `tests/backtest/test_universe_selection_routes.py` -- add cases for: fresh-load default-on, whole-universe submit resolves to full roster, whole-universe submit with empty roster still errors, toggle-off falls back to manual `security_ids`, stale-profile rejection still applies when whole-universe is checked.

**Acceptance Criteria:**
- Given a fresh Backtest configuration form load, when the Universe section renders, then "Whole universe" is checked and the manual security list is not required to interact with.
- Given "Whole universe" is checked at submit, when the form is submitted, then the launched Run's universe is the full current active-profile roster, canonicalized the same way manual selections are.
- Given "Whole universe" is unchecked, when the form is submitted, then behavior is unchanged from the existing Story 4.5 manual multi-select flow.
- Given the active profile changes between form load and submit, when "Whole universe" is checked and submitted, then the existing stale-profile rejection still fires rather than silently launching against the new roster.

## Spec Change Log

## Review Triage Log

### 2026-08-25 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5 (medium 0, low 5)
- defer: 1 (low 1)
- reject: 9
- addressed_findings:
  - `[low]` `[patch]` `bool(whole_universe)` treated any non-empty string (e.g. a literal `"false"`) as truthy; added `_is_truthy_flag()` helper and used it at both the GET partial route and the POST submit handler, plus a regression test for the literal-`"false"` case.
  - `[low]` `[patch]` Whole-universe submit against an empty active roster fell through to the generic "Select at least one security" message with no actionable context; added a specific "The active roster has no securities to select." message and updated its test.
  - `[low]` `[patch]` `submit_strategy_configuration`'s whole-universe branch re-fetched `roster_member_identities()` even though the same tuples were already resolved into `context["securities"]` moments earlier; reused that instead of a second DB round trip.
  - `[low]` `[patch]` The new "Whole universe" checkbox toggled an entire subsection (search box, listbox, chips) with no ARIA relationship to it; added `aria-controls="universe-manual-picker"` and `aria-expanded` reflecting the picker's visibility.
  - `[low]` `[patch]` A docstring/comment claimed the search-as-you-type GET route "passes `whole_universe` explicitly" through `_configuration_context`, but that route never calls `_configuration_context` at all (it builds its context inline); corrected the wording on both affected comments.

An HTML checkbox only appears in form data when checked, so "not present" and "explicitly unchecked" look identical over the wire. Do not rely on `whole_universe` presence/absence alone to distinguish "user unchecked it" from "route rendered before the field existed" -- render the field's own current boolean state back into the template on every response (GET and POST-error re-renders alike) so it round-trips correctly through htmx partial swaps.

## Verification

**Commands:**
- `uv run pytest tests/backtest/test_universe_selection_routes.py -q` -- expected: all pass, including new whole-universe cases.
- `uv run pytest tests/backtest -q` -- expected: no regressions elsewhere in Strategy Manager/backtest launch flow.
- `uv run ruff check app/api/routes/strategy_manager.py app/api/templates/_universe_selector.html` -- expected: clean.
- `uv run pyrefly check app/api/routes/strategy_manager.py` -- expected: 0 errors.

**Manual checks (if no CLI):**
- Load the Backtest configuration form fresh in a browser: "Whole universe" should be checked and the manual picker collapsed/hidden.
- Uncheck it: the existing search + checkbox list should appear and work exactly as before.
