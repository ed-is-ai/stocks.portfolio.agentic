---
title: 'Refresh button UX: consolidate actions and move freshness detail into status icon'
type: 'feature'
created: '2026-08-31'
status: 'done'
baseline_revision: '9fd10d6d73c59a2be8a51851c2931ef4cc14a2df'
final_revision: '143231360c7ac7747a4dbc4a8bd27d8d9fc458e5'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/spec-gh-418-refresh-freshness-affordance.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** The scanner header exposes two competing refresh controls and a permanently visible timestamp, making an occasional maintenance action visually dominant without explaining the different source cadences. Freshness also needs to distinguish stale usable data from a failed latest attempt.

**Approach:** Replace the controls with one non-split dropdown that offers standard, institutional, and custom refresh actions while preserving the existing backend contract. Move freshness into an accessible status treatment on that control: quiet when fresh, amber when stale, red when the latest attempt failed, with full detail available on hover, focus, and after opening on touch.

## Boundaries & Constraints

**Always:** Keep one visible scanner-header refresh toggle; clicking it only opens the menu. Preserve the existing `/refresh-data` field names, local/token authorization, missing-configuration confirmation, single-run protection, pipeline status polling, failure toast, and independent force flags. Treat latest-attempt failure as presentation state separate from last-success freshness, with failure taking visual precedence while retaining the last successful time. Keep status understandable without colour, keyboard accessible, touch discoverable, responsive at narrow widths and zoom, and sourced from one consistent presenter. Leave Portfolio `Refresh Prices` unchanged.

**Block If:** Preserving authentication, confirmation values, or single-run behavior requires changing the public refresh route or pipeline execution semantics; or the available pipeline context cannot reliably distinguish latest failure from the last successful artefact.

**Never:** Trigger a refresh from the dropdown toggle; add nested focusable controls inside the toggle; combine conflicting Bootstrap dropdown and declarative tooltip toggles on the same element; add a new pipeline freshness state for failure; change source schedules, cache policy, or upstream publication behavior; or broaden the change to Portfolio refresh UX.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Standard | User selects standard refresh | POST existing defaults: no extract and no force flags | Existing confirmation/auth/single-run flow remains authoritative |
| Institutional | User selects include-institutional | POST `extract=true`, force flags false | Existing pipeline feedback and confirmation are retained |
| Custom | User independently selects either or both sources | POST `extract=true` and the chosen existing force fields | Unselected flags remain false; guidance says forcing cannot create newer upstream data |
| Fresh | Last success is inside its freshness window and latest attempt did not fail | No visible warning icon or persistent timestamp; detail still exposes last refresh and age | Unknown/missing data must not be presented as fresh |
| Stale | Last success is outside its calculated window | Amber warning, non-colour stale label, last refresh, relative age, and freshness-window reason | Preserve the usable last-success timestamp |
| Latest failure | Latest attempt failed, whether last success is fresh or stale | Red warning takes precedence and exposes failure detail plus last success | Keep the existing failure toast and sanitization behavior |
| Unknown | No successful refresh time exists | Neutral unknown indicator and explicit accessible explanation | Do not invent a time or healthy state |

</intent-contract>

## Code Map

- `app/api/templates/index.html` -- scanner header refresh dropdown, three action forms, source-cadence guidance, and responsive styles.
- `app/api/templates/_macros.html` -- shared freshness status/detail presentation and stable `#refresh-freshness` OOB target.
- `app/api/templates/_pipeline_status.html` -- passes latest failure context through status OOB updates.
- `app/api/stock_scanner_context.py` and `app/services/freshness_service.py` -- existing freshness/failure context and deterministic plain-language age presentation.
- `app/api/static/js/pipeline-refresh.js` -- closes/disables the single toggle for any `/refresh-data` request and restores it on terminal/error paths.
- `app/api/static/js/watchlist.js` -- existing local-time and OOB enhancement lifecycle; extend only if the chosen disclosure needs reinitialization.
- `app/api/templates/_pipeline_confirmation.html` -- existing flag-preserving continuation contract; regression target, not a redesign.
- `tests/test_views_index_freshness.py`, `tests/test_pipeline_status_route.py`, `tests/test_stock_scanner_ui.py`, `tests/test_pipeline_refresh_flags.py` -- presentation, OOB, markup, and request-flag coverage.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/index.html` -- replace the two controls and timestamp with one viewport-bounded dropdown containing standard, institutional, and custom actions plus concise quarterly 13F and weekly StockTwits guidance.
- [x] `app/api/templates/_macros.html`, `app/api/templates/_pipeline_status.html` -- present quiet/fresh, amber/stale, red/failure, and neutral/unknown states from a stable OOB-updatable element, with identical accessible detail available across pointer, keyboard, and touch paths.
- [x] `app/api/stock_scanner_context.py`, `app/services/freshness_service.py` -- provide deterministic relative-age display data without changing freshness calculation or conflating failure with artefact age.
- [x] `app/api/static/js/pipeline-refresh.js`, `app/api/static/js/watchlist.js` -- support form-originated refresh requests, dropdown closure, disabled/loading lifecycle, and OOB-updated disclosure without stale handlers.
- [x] `tests/test_views_index_freshness.py`, `tests/test_pipeline_status_route.py`, `tests/test_stock_scanner_ui.py`, `tests/test_pipeline_refresh_flags.py` -- cover the state and action matrix, confirmation flag retention, accessibility contracts, and unchanged route semantics.

**Acceptance Criteria:**
- Given the scanner header, when the user activates Refresh Data, then it opens one non-split menu and does not POST until an action inside the menu is selected.
- Given the menu, when standard, include-institutional, or any custom source combination is submitted, then the existing route receives the corresponding default, extract, and independent force values.
- Given the menu guidance, when it is read, then WhaleWisdom is described as quarterly 13F data published after quarter end, StockTwits as weekly, and force-checking as bypassing local reuse without making upstream data newer.
- Given fresh, stale, latest-failed, or unknown state, when the header or OOB status renders, then it follows the matrix above and exposes the same meaningful detail without relying on colour.
- Given keyboard, pointer, touch, a 320px viewport, or browser zoom, when the control is used, then actions, status detail, focus, and menu content remain operable and unclipped.
- Given a refresh confirmation, rejection, failure, or terminal status, when the lifecycle completes, then selected flags are retained as applicable and the single toggle returns to the correct enabled state.
- Given the Portfolio tab, when its Refresh Prices action is used, then its markup and behavior are unchanged.

## Spec Change Log

## Review Triage Log

### 2026-08-31 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 10: (high 1, medium 7, low 2)
- defer: 0
- reject: 5
- addressed_findings:
  - `[high]` `[patch]` Refresh forms targeted `#tab-content` and were caught by a global every-form handler, activating Portfolio after a refresh. Restricted that handler to the `/trades` route and added regression coverage.
  - `[medium]` `[patch]` Server-rendered relative age could remain frozen on a long-lived page. Recompute it from the absolute timestamp on hover, focus, menu open, and OOB replacement while keeping the server fallback.
  - `[medium]` `[patch]` Latest-failure presentation hid the stale caution for an already-stale last usable artifact. Retained the red failure precedence and added explicit stale-use caution plus the freshness-window time.
  - `[medium]` `[patch]` A polite live region nested in the dropdown button could be unreliable and repeatedly announced on every poll. Removed live-region semantics and kept state attached through the toggle's accessible description.
  - `[medium]` `[patch]` Freshness detail could be folded into the button name and then repeated as its description. Added an explicit concise toggle label while retaining the separate described detail.
  - `[medium]` `[patch]` Both primary action buttons exposed only “Run” in button navigation. Added distinct accessible action labels.
  - `[medium]` `[patch]` Hiding the menu during a form submission could strand focus inside hidden content. Return focus to the toggle and use focus-preserving `aria-disabled` behavior while the run is active.
  - `[low]` `[patch]` Repeated activation of the formerly focused submit control could issue another protected request. Disable every menu submit action and suppress toggle activation during the active run.
  - `[low]` `[patch]` Resize, zoom, or font loading could invalidate reserved status-detail height. Resynchronize while open on viewport resize and font readiness.
  - `[medium]` `[patch]` A long failure summary could cover or push menu actions beyond the viewport. Bound the disclosure height and make it independently scrollable.

## Design Notes

The toggle is the single keyboard/touch target; its status icon is noninteractive. A custom hover/focus disclosure may be associated with the toggle because Bootstrap's `data-bs-toggle` is already reserved for the dropdown. Opening the menu provides the same status detail for touch users. OOB updates replace only the stable inner freshness region so they do not destroy the dropdown instance or menu.

## Verification

**Commands:**
- `pytest -q tests/test_views_index_freshness.py tests/test_pipeline_status_route.py tests/test_stock_scanner_ui.py tests/test_pipeline_refresh_flags.py` -- expected: all focused behavior and regression tests pass.
- `pytest -q` -- expected: full suite passes.
- `ruff check .` -- expected: no lint errors.
- `git diff --check` -- expected: no whitespace errors.

**Manual checks (if no CLI):**
- Open the header menu with mouse, keyboard, and touch emulation; verify Escape/focus behavior, no request on toggle, status disclosure, and no horizontal overflow at 320px and increased zoom.

## Auto Run Result

**Summary.** The scanner header now has one non-split Refresh Data dropdown. It offers standard, institutional, and independently forced source refreshes through the existing authenticated route contract, explains WhaleWisdom's quarterly 13F and StockTwits' weekly cadence, and replaces the persistent timestamp with an accessible freshness treatment: quiet when fresh, amber when stale, red when the latest attempt failed, and neutral when unknown. The recorded unattended decisions were to keep unknown neutral, let failure take visual precedence while retaining stale caution and the last-success time, and use focus-preserving `aria-disabled` guarding rather than a native-disabled toggle.

**Files changed.**
- `app/api/templates/index.html` -- one responsive dropdown, three refresh actions, source guidance, status disclosure styling, distinct accessible labels, and trade-only Portfolio activation.
- `app/api/templates/_macros.html` -- shared fresh/stale/failure/unknown presentation, relative-age hook, stale failure caution, and stable OOB target.
- `app/api/templates/_pipeline_status.html` -- supplies latest-attempt failure to both OOB and running-bar freshness renders.
- `app/services/freshness_service.py` -- deterministic server-side relative-age fallback.
- `app/api/static/js/pipeline-refresh.js` -- form-origin request detection, focus-safe single-run guarding, menu closure, current age text, and responsive disclosure sizing.
- `tests/test_freshness.py` -- relative-age boundary coverage.
- `tests/test_pipeline_refresh_flags.py` -- standard/institutional/custom flag matrix and confirmation retention.
- `tests/test_pipeline_status_route.py` -- updated OOB failure/freshness contracts.
- `tests/test_stock_scanner_ui.py` -- consolidated-menu, guidance, action-label, and tab-activation regressions.
- `tests/test_views_index_freshness.py` -- quiet fresh, stale, failed-over-fresh, failed-over-stale, and unknown first-paint states.

**Review breakdown.** Two independent BMAD review passes produced 15 deduplicated findings. Ten were patched (1 high, 7 medium, 2 low), none were deferred, and five were rejected as outside the accepted contract or already protected behavior. Because the review changed cross-cutting keyboard/HTMX behavior and fixed a high-consequence tab regression, an independent follow-up review is recommended.

**Verification.**
- `uv run pytest -q tests/test_views_index_freshness.py tests/test_pipeline_status_route.py tests/test_stock_scanner_ui.py tests/test_pipeline_refresh_flags.py tests/test_freshness.py` -- 80 passed, 1 warning.
- `uv run pytest -q` -- 2594 passed, 5 warnings in 86.63s.
- `uv run ruff check` on all changed Python/test files -- passed.
- `node --check app/api/static/js/pipeline-refresh.js` -- passed.
- `git diff --check` -- passed.
- `uv run ruff check .` -- reports nine pre-existing errors exclusively in unchanged `scripts/` and `skills/` files; this branch adds none.

**Residual risks.** The repository has no automated browser coverage for Bootstrap dropdown focus, touch disclosure, or extreme zoom, so those interactions are protected by markup/static regressions and code inspection rather than an end-to-end test. The server and browser relative-age formatters intentionally mirror each other; future wording changes must keep them aligned.
