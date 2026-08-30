---
title: 'Move refresh freshness from the persistent status bar to the refresh control (gh-418)'
type: 'feature'
created: '2026-08-30'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: ['oversized']
baseline_revision: 'ecf82392'
final_revision: '2df58eaa'
---

<intent-contract>

## Intent

**Problem:** `#pipeline-status` (`index.html:575`) is fetched on every page load and
renders a `position: fixed` bottom bar in *every* state, including idle and
completed. `body` permanently reserves `calc(2.5rem + safe-area-inset)` of bottom
padding for it (`index.html:335`). So a user who is not refreshing anything still
pays persistent screen space for a bar whose only lasting content is the "Last
successful refresh" line.

**Approach:** Render the fixed bar **only while a refresh is running**. Move the
existing freshness display (`_pipeline_status.html:34-44` — fresh/stale/unknown
icon, timestamp, stale caution) into a shared macro and place it beside the
header refresh control as an accessible tooltip affordance. Drop the permanent
body offset. Nothing about refresh execution, freshness calculation, or
source-health semantics changes.

## Boundaries & Constraints

**Always:**
- The freshness macro is the single source of the fresh/stale/unknown icon,
  timestamp and copy; the header and `_pipeline_status.html` both render it.
- Existing copy is preserved verbatim: `Last successful refresh`, `Last
  successful refresh unknown`, and the stale screen-reader sentence `Analysis
  data is stale and should be used with caution.`
- The timestamp keeps the `<time class="local-time" datetime="...">` element so
  `renderLocalTimes()` (`watchlist.js:580-593`) localises it unchanged.
- The tooltip uses `data-bs-toggle="tooltip"`, reusing the already-initialised,
  HTMX-swap-safe `initTooltips()` (`watchlist.js:595-608`). No new JS library, no
  Bootstrap Popover.
- Freshness state is conveyed by icon + text + `aria-label`, never colour alone.
- Polling is untouched: `hx-trigger="every 2s"` still ships only in the running
  render, so terminal states still stop polling on their own.
- The failure/partial toast (`_pipeline_status.html:48-81`) and source-health
  disclosure keep rendering in terminal states. Hiding targets the bar element,
  not the `#pipeline-status` container the toast is nested in.
- The attention cue is one-shot (`animation-iteration-count: 1`) and nulled under
  `prefers-reduced-motion`, matching `index.html:332`.

**Block If:**
- Delivering an AC would require changing refresh/pipeline execution, the
  `calculate_freshness` contract, or source-health data semantics.

**Never:**
- No change to `freshness_service.py`, `pipeline_status.py`, `PipelineService`,
  or `build_freshness_context()`'s return shape.
- No second freshness computation and no freshness rendered on the scanner —
  `test_stock_scanner_ui.py:733` asserts the scanner must not duplicate it.
- No new polling loop in JS; server-driven `hx-trigger` stays the mechanism.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Idle first load | `state=idle` | No `.pipeline-status` bar; no body bottom offset; header shows freshness affordance | No error expected |
| Running | `state=running` | Bar renders with stages + `hx-trigger="every 2s"`; body offset applied while running | No error expected |
| Reaches terminal | `running` → `complete` | Poll swap removes the bar and the offset; header freshness updates via OOB | No error expected |
| Failed / partial | `state=failed`, `latest_attempt_error` set | No bar, but the breakdown toast still renders and shows | No error expected |
| Fresh | `freshness.state=fresh` | `clock-fill` icon; tooltip exposes localised timestamp | No error expected |
| Stale | `freshness.state=stale` | `exclamation-circle-fill` icon; stale caution text present for SR | No error expected |
| Unknown | `freshness.state=unknown`, `refreshed_at=None` | `question-circle` icon; reads `Last successful refresh unknown` | Explicitly unknown, never blank |
| Reduced motion | `prefers-reduced-motion: reduce` | Affordance present and discoverable; no animation | No error expected |

</intent-contract>

## Code Map

- `app/api/templates/_macros.html` -- **add** `freshness_affordance(freshness, variant)` macro holding the icon/timestamp/copy currently inline at `_pipeline_status.html:34-44`.
- `app/api/templates/_pipeline_status.html` -- bar chrome becomes running-only; call the macro instead of inline freshness; emit the header copy as an OOB swap. Toast block (`:48-81`) unchanged.
- `app/api/templates/index.html` -- `:469` add the freshness affordance next to `#refresh-data-button` (OOB swap target, stable id); `:313` scope `.pipeline-status-bar` fixed chrome to the running state; `:335` remove the permanent `body` bottom padding; `:328` toast offset no longer assumes a bar; add the one-shot cue keyframes + the `prefers-reduced-motion` override beside `:332`.
- `app/api/routes/views.py:36-37` -- `index` currently passes **no context**; add `**build_freshness_context()` so the header renders freshness server-side on first paint.
- `app/api/stock_scanner_context.py:50-60` -- `build_freshness_context()`, reused as-is.
- `app/api/routes/pipeline.py:35-46` -- `/pipeline-status` context, unchanged.
- `app/api/static/js/watchlist.js:580-608` -- `renderLocalTimes()` / `initTooltips()`, both already re-run on `htmx:afterSwap`; reused, not modified.
- `tests/test_pipeline_status_route.py` -- 9 existing tests; the two polling tests must keep passing.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_macros.html` -- add the `freshness_affordance` macro (icon per state, `<time class="local-time">`, unknown fallback, stale SR sentence, `aria-label`, `data-bs-toggle="tooltip"`) -- one source of truth for both call sites.
- [x] `app/api/templates/_pipeline_status.html` -- render the bar only when `status.state == 'running'`; replace inline freshness with the macro; add an OOB-swapped copy targeting the header id so a completing run updates the header without a reload -- `hx-swap-oob` prior art is `_notif_badge.html:5`.
- [x] `app/api/routes/views.py` -- pass `**build_freshness_context()` from `index` -- the header needs freshness on first paint, before any fetch resolves.
- [x] `app/api/templates/index.html` -- place the affordance beside `#refresh-data-button`; make the fixed-bar chrome and the body bottom offset apply only while running; keep the toast clear of the bar; add one-shot cue CSS plus its reduced-motion override.
- [x] `tests/test_pipeline_status_route.py` -- cover: idle/terminal renders no bar, running still renders bar + `every 2s`, terminal still renders the toast, and fresh/stale/unknown copy.
- [x] `tests/test_views_index_freshness.py` -- **new**: `GET /` renders the header affordance for fresh, stale and unknown freshness.

**Acceptance Criteria:**
- Given an idle or completed pipeline, when `/` renders, then no `.pipeline-status` bar is present and `body` carries no bar-reserving bottom offset.
- Given a running refresh, when `/pipeline-status` renders, then the bar is visible and still carries `hx-trigger="every 2s"` until terminal.
- Given a run that completes, fails, or is partial, when the terminal state renders, then the bar is gone, the breakdown toast still renders for failure/partial, and the header freshness reflects the run.
- Given a last successful refresh exists, when the user hovers or focuses the affordance, then it exposes the localised timestamp and freshness state; with no timestamp it reads `Last successful refresh unknown`.
- Given first load with motion permitted, then the affordance plays a single non-repeating cue; under `prefers-reduced-motion` no animation runs and the affordance is still reachable and labelled.
- Given any state, then no freshness markup appears on the scanner partial (`test_stock_scanner_ui.py:733` still passes).

## Design Notes

**Why the route change.** `index` passes no context today, so a header affordance
would otherwise be blank until the load-triggered `/pipeline-status` fetch
resolves — and blank if that fetch failed. Since the refresh control is now the
*primary* way to discover freshness, it is server-rendered on first paint, with
the OOB swap keeping it current after a run. One macro, two render paths.

**Why the bar, not the container, is hidden.** `#pipeline-status` holds both the
bar and the failure toast as siblings. Hiding the container would silently drop
AC3's warnings, so only `.pipeline-status` is conditional.

Header affordance shape:

```jinja
{{ macros.freshness_affordance(freshness, variant='control') }}
{# → <span id="refresh-freshness" data-bs-toggle="tooltip"
        aria-label="Analysis freshness" title="Last successful refresh …"> #}
```

## Verification

**Commands:**
- `uv run pytest tests/test_pipeline_status_route.py tests/test_views_index_freshness.py tests/test_stock_scanner_ui.py -q` -- expected: all pass.
- `uv run pytest -q` -- expected: no regressions.
- `uv run ruff format . && uv run ruff check .` -- expected: clean on changed files.
- `uv run pyrefly check` -- expected: no new errors.

**Manual checks:**
- Load `/` idle: no bottom bar, no dead space at the page foot, freshness icon beside Refresh; hover shows the localised timestamp.
- Trigger a refresh: bar appears and polls; on completion it disappears and the header timestamp updates without a reload.

## Review Triage Log

### 2026-08-30 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6: (high 1, medium 3, low 2)
- defer: 3: (medium 2, low 1)
- reject: 10
- addressed_findings:
  - `[high]` `[patch]` htmx out-of-band swaps fire `htmx:oobBeforeSwap`/`htmx:oobAfterSwap`, never `htmx:afterSwap`, and `initTooltips(root)` matched descendants only — so the header affordance lost its Bootstrap tooltip and its localised timestamp the moment the load-triggered `/pipeline-status` fetch landed, on every page load. Added a `selfAndDescendants()` helper so both enhancers include the swapped root itself, bound `renderLocalTimes`/`initTooltips` to `htmx:oobAfterSwap`, and disposed the outgoing tooltip on `htmx:oobBeforeSwap` so pollers don't strand poppers.
  - `[medium]` `[patch]` The "one-shot" cue was on `.refresh-freshness`, which the OOB swap replaces every two seconds during a run — the navbar flickered for the whole refresh. Moved the animation onto a `refresh-freshness-cue` class the server render emits and the OOB render omits; the reduced-motion override follows it.
  - `[medium]` `[patch]` The affordance moved onto the near-black navbar but kept `--muted` (#5b6470) and `--amber` (#a3560a), ~3:1 against `--navbar-grad` (#111418) at ~12px — both fail WCAG AA, the stale warning worst of all. Switched to light-on-dark ink (`rgba(255,255,255,0.72)`, stale `#fbbf24`) and dropped the no-op `.freshness-unknown` rule.
  - `[medium]` `[patch]` The affordance was a bare `<span>` with no `tabindex`, so AC4's "hovers **or focuses**" was unreachable by keyboard, and `aria-label` on an implicit `role=generic` element may be dropped or may suppress the descendant copy. Added `role="note" tabindex="0"` plus a `:focus-visible` outline.
  - `[low]` `[patch]` `.pipeline-status-bar:has(.pipeline-status) .pipeline-breakdown-toast-container` can never match — the bar renders only while running and the toast only in terminal states. Removed the dead rule and documented why the toast sits on the page floor.
  - `[low]` `[patch]` `.refresh-freshness` had no `white-space: nowrap` while `renderLocalTimes()` swaps the compact UTC string for a longer `toLocaleString()` + zone name, risking navbar wrap. Added `nowrap`.

## Auto Run Result

**Summary.** The fixed bottom pipeline bar was rendered in every state and `body`
permanently reserved `calc(2.5rem + safe-area-inset)` for it, so an idle user paid
persistent screen space for a bar whose only lasting content was one freshness line.
The bar is now running-only, and the fresh/stale/unknown freshness display moved into
a shared Jinja macro that renders beside the header Refresh control as a keyboard- and
screen-reader-reachable tooltip affordance, server-rendered on first paint and kept
current by an out-of-band swap from the existing `/pipeline-status` poll. Refresh
execution, `calculate_freshness`, and source-health semantics are untouched.

**Files changed**
- `app/api/templates/_macros.html` -- new `freshness_affordance` / `freshness_detail`
  macros: one source of truth for the icon, copy and `<time class="local-time">`
  across the bar and header variants.
- `app/api/templates/_pipeline_status.html` -- bar gated on `status.state == 'running'`;
  emits the header affordance out-of-band; toast block untouched.
- `app/api/templates/index.html` -- affordance placed beside `#refresh-data-button`;
  fixed-bar chrome and the body bottom offset scoped via `:has()` to a bar actually
  being present; light-on-dark affordance styling and the one-shot cue.
- `app/api/routes/views.py` -- `index` now passes `**build_freshness_context()` so the
  header is correct on first paint rather than after (or despite) a fetch.
- `app/api/static/js/watchlist.js` -- `renderLocalTimes()`/`initTooltips()` now include
  the passed root itself, and both run on `htmx:oobAfterSwap`, with tooltip disposal on
  `htmx:oobBeforeSwap`.
- `tests/test_pipeline_status_route.py` -- amended two state assertions for the
  running-only bar; five new tests for bar/no-bar, OOB header, toast survival, and
  fresh/stale copy.
- `tests/test_views_index_freshness.py` -- **new**: `GET /` renders the affordance for
  fresh, stale and unknown, exposes `role="note" tabindex="0"` and the first-paint cue,
  and reserves no permanent body offset.

**Review breakdown.** Two adversarial passes (blind + edge-case). 6 patches applied
(1 high, 3 medium, 2 low): the OOB swap bypassing both JS enhancers, the cue restarting
on every poll, WCAG AA contrast failure on the dark navbar, keyboard/ARIA reachability,
a dead `:has()` toast rule, and navbar overflow. 3 findings deferred to
`deferred-work.md`. 10 rejected as noise or as intended consequences of the accepted
acceptance criteria.

**Verification.**
- `uv run pytest tests/test_pipeline_status_route.py tests/test_views_index_freshness.py tests/test_stock_scanner_ui.py -q` -- `56 passed, 1 warning`.
- `uv run pytest -q` -- `2408 passed, 5 warnings in 71.31s`.
- `uv run ruff format` + `uv run ruff check` on the changed Python files -- `3 files left unchanged`, `All checks passed!`.
- `uv run pyrefly check` -- 123 errors, byte-identical to the `ecf82392` baseline set; no new errors.

**Residual risks.**
- The OOB tooltip/local-time repair is exercised only in a real browser; the test suite
  asserts server markup and cannot observe htmx event wiring. `selfAndDescendants()`
  changes behaviour for every existing `initTooltips`/`renderLocalTimes` call site --
  strictly additive (the root is now also considered), but broad.
- `:has()` gates the bar chrome and the body offset. Fine on Chrome 105+/Safari 15.4+/
  Firefox 121+; on anything older the running bar renders inline rather than fixed.
- Terminal runs now confirm themselves only through the header timestamp changing; see
  the deferred entry on lost terminal feedback.
- The body offset now appears and disappears with a run, trading permanent dead space
  for a 2.5rem shift twice per refresh. Accepted as inherent to AC1.
