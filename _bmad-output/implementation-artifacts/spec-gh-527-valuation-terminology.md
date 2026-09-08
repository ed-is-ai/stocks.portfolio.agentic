---
title: 'GH-527: consistent portfolio valuation terminology'
type: 'bugfix'
created: '2026-09-08'
status: 'done'
baseline_revision: 'a795cf2e6660a1f069d4c9dc87b95af54cf5a6e3'
final_revision: '30a9ad3519fea0f1919aa860ff4375a903cd2da5'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/app/api/templates/_portfolio.html'
  - '{project-root}/app/api/templates/_portfolio_chart.html'
  - '{project-root}/app/services/portfolio_service.py'
warnings:
  - multiple-goals
  - oversized
---

<intent-contract>

## Intent

**Problem:** On the Portfolio tab the phrase "Market Value" names the
cash-*inclusive* total in the summary card but the cash-*exclusive*
holdings total in the chart legend and the holdings-table column, so the
same words point at two different figures on one screen. "Total Cost" in
the summary card silently bundles cash too.

**Approach:** Rename the summary card that shows the cash-inclusive total
(`total_value_gbp`) from "Market Value" to "Portfolio Value" so it matches
the chart's solid series; make the "cash is included" fact visible on both
cash-inclusive cards. After this, "Market Value" means holdings-only
everywhere and "Portfolio Value" means holdings+cash everywhere. Backend
figures are already correct (`total_cost_gbp_valued`, the P&L%
denominator, is holdings-only) — this is a labelling change only.

## Boundaries & Constraints

**Always:**
- "Market Value" = holdings only (no cash), wherever it appears.
- "Portfolio Value" = holdings + cash, wherever it appears.
- Keep the existing numbers/bindings: the renamed card still binds
  `total_value_gbp`; "Total Cost" still binds `total_cost_gbp`.
- Keep the chart legend labels ("Portfolio Value" solid, "Market Value"
  dashed) as they are — they already follow the target convention.
- Accessibility: the "includes cash" hint must be reachable by screen
  readers (a `visually-hidden` span is acceptable, matching the current
  pattern).

**Block If:**
- Making the terminology consistent would require renaming a chart series
  or changing any stored/served numeric value (it should not — if it
  does, stop and surface it).

**Never:**
- Do NOT touch the reconstructed-chart "implausible swings" concern from
  issue #527 section 2. Investigation (see Design Notes) shows those
  swings are ~90% a real composition change (the portfolio held 11–16
  positions worth ~£160k in early 2026 and was liquidated to ~£64k of
  holdings + ~£95k cash by mid-2026); the issue's price-cache/unit-scale
  hypothesis is disproven. That half needs product direction and is
  carried in `deferred-work.md`, not this spec.
- No backend/service changes, no chart-data changes, no new template
  context keys unless a label genuinely needs one.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Cash-only portfolio | positions empty, cash set | "Portfolio Value" card shows the cash amount; "Market Value" wording appears only in the holdings table / chart | No error expected |
| Positions + cash | normal SIPP | "Portfolio Value" = holdings + cash; holdings-table "Mkt Value" column unchanged (per-row, no cash) | No error expected |
| No cash balance (multi-currency only) | `cash_balance is none` | Card still titled "Portfolio Value"; body renders the existing multi-currency fallback unchanged | No error expected |

</intent-contract>

## Code Map

- `app/api/templates/_portfolio.html` (~L418–450) -- the summary strip:
  the `stat-card` currently titled "Market Value" (binds
  `total_value_gbp`, carries the `visually-hidden` "Includes cash." span)
  and the "Total Cost" card (binds `total_cost_gbp`).
- `app/api/templates/_portfolio_chart.html` (L11–13, L43–53, L92–129) --
  legend + helper text already use "Portfolio Value" / "Market Value"
  with the target meaning; reference only, do not change.
- `app/services/portfolio_service.py` (L1215–1244) -- builds
  `total_value_gbp` (holdings+cash), `total_cost_gbp` (holdings cost +
  cash), `total_cost_gbp_valued` (holdings-only cost, the P&L%
  denominator — already correct). No change; confirms no backend work.
- `tests/test_portfolio_template.py` -- existing template assertions;
  update any that assert the literal "Market Value" card title.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/templates/_portfolio.html` -- rename the summary `stat-card`
  from "Market Value" to "Portfolio Value" (keep the `bi-graph-up` icon
  and the `total_value_gbp` binding); keep/keep-wording the
  `visually-hidden` "Includes cash." hint on it.
- [x] `app/api/templates/_portfolio.html` -- on the "Total Cost" card add
  the same `visually-hidden` "Includes cash." hint (it binds
  `total_cost_gbp`, which adds `effective_cash_balance`).
- [x] `tests/test_portfolio_template.py` -- update/extend assertions:
  "Portfolio Value" card present, "Market Value" no longer used as a
  summary-card title, holdings-table column header still "Mkt Value".

**Acceptance Criteria:**
- Given a SIPP portfolio with holdings and cash, when the portfolio
  partial renders, then exactly one element is titled "Portfolio Value"
  (the cash-inclusive summary card) and no summary card is titled
  "Market Value".
- Given the same render, when a screen reader reads the "Portfolio Value"
  and "Total Cost" cards, then each announces that cash is included.
- Given the chart renders, when its legend shows, then "Portfolio Value"
  (solid) and "Market Value" (dashed) are unchanged.
- Given the P&L% figure, when it is computed, then its denominator is
  `total_cost_gbp_valued` (holdings-only) — unchanged by this spec.

## Spec Change Log

_(none — no bad_spec loopback)_

## Review Triage Log

### 2026-09-08 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (medium 1, low 2)
- defer: 2
- reject: 6
- addressed_findings:
  - `[medium]` `[patch]` The unconditional "Includes cash." hint on both
    the Portfolio Value and Total Cost cards is false for a
    multi-currency-only portfolio (`cash_balance is none`), where
    `total_value_gbp` / `total_cost_gbp` exclude cash. Wrapped both spans
    in `{% if cash_balance is not none %}` — root-cause fix, also
    covering the pre-existing Portfolio Value span.
  - `[low]` `[patch]` `assert html.count("Includes cash.") == 2` was
    brittle (no per-card targeting). Replaced with a `_card()` helper and
    per-card assertions.
  - `[low]` `[patch]` `test_summary_cards_have_the_required_market_first_order`
    was misnamed after the rename and the multi-currency branch had no
    coverage. Renamed to `..._portfolio_value_first_order`; added
    `test_includes_cash_hint_is_suppressed_when_no_gbp_cash_balance`.
  - deferred (2): alert-agent CLI/email snapshot still uses "Market
    Value" and mislabels a cash-inclusive total as "Mkt Value"
    (`alert_agent.py`, `_snapshot.html`); `total_cost_gbp` mixes
    historical holdings cost with current cash. Both in `deferred-work.md`.
  - rejected (6): intra-page "Mkt Value" vs "Portfolio Value" difference
    (intended end state); SR-hint asymmetry vs P&L/Cash cards; label
    width; `default(0)` asymmetry (key always supplied); "Mkt Value"
    assertion framing (it guards AC1); "label-only understates it"
    meta-comment.

## Design Notes

`total_value_gbp` (holdings + cash) is the value the renamed card binds;
`total_cost_gbp_valued` (holdings-only) is the P&L% denominator and is
already correct — verified in `portfolio_service.py` L1215–1244, no
backend change needed.

Issue #527 section 2 ("implausible swings") is descoped with full
investigation evidence in
`_bmad-output/implementation-artifacts/deferred-work.md` (entry dated
2026-09-08): the swings are a real portfolio composition change
(~£160k / 11 holdings in early 2026 → ~£64k / 5 holdings + ~£95k cash by
mid-2026), the price caches are clean, and the issue's proposed guard is
the wrong fix.

## Verification

**Commands:**
- `uv run pytest tests/test_portfolio_template.py` -- expected: pass,
  including the new "Portfolio Value" assertions.
- `uv run ruff check app/api/templates` -- n/a (templates); run
  `uv run ruff format .` and `uv run ruff check .` for the test edit.

**Manual checks:**
- Render the Portfolio tab for the SIPP account: the four summary cards
  read "Portfolio Value", "Total Cost", "Unrealised P&L", "Cash"; the
  chart legend still reads "Portfolio Value" / "Market Value"; the
  holdings table column is still "Mkt Value".

## Auto Run Result

Status: done

**Implemented change:** Portfolio-tab terminology made consistent. The
summary card that shows the cash-inclusive total (`total_value_gbp`) is
renamed "Market Value" → "Portfolio Value", matching the chart's solid
series. "Market Value" now means holdings-only everywhere (holdings-table
"Mkt Value" column, chart dashed series). A screen-reader "Includes cash."
hint is shown on both cash-inclusive cards (Portfolio Value, Total Cost)
and is suppressed when no GBP cash balance is known. Backend figures
unchanged — the P&L% denominator `total_cost_gbp_valued` was already
holdings-only. Issue #527 section 2 ("implausible swings") descoped after
investigation disproved its hypothesis (see `deferred-work.md`).

**Files changed:**
- `app/api/templates/_portfolio.html` — summary card "Market Value" →
  "Portfolio Value"; both cash-inclusive cards carry a `{% if
  cash_balance is not none %}`-guarded "Includes cash." span.
- `tests/test_portfolio_template.py` — assertions updated for the rename;
  `_card()` slice helper; per-card hint assertions; new
  `test_includes_cash_hint_is_suppressed_when_no_gbp_cash_balance`.
- `_bmad-output/implementation-artifacts/deferred-work.md` — three defer
  entries (section-2 swings investigation; alert/email surface; Total
  Cost mixed-basis).

**Review findings:** 3 patches applied (1 medium: conditional
"Includes cash." hint for the multi-currency path; 2 low: test
brittleness + naming/coverage). 2 deferred. 6 rejected. 0 intent_gap,
0 bad_spec.

**Follow-up review recommended:** false — the review-driven changes were
one localized template guard plus test tidy-up, low consequence.

**Verification:**
- `uv run pytest tests/test_portfolio_template.py` → 28 passed
- `uv run pytest tests/test_portfolio_service.py tests/test_portfolio_chart_route.py` → 80 passed
- `uv run ruff format .` / `uv run ruff check .` (changed files) → clean
- `uv run pyrefly check tests/test_portfolio_template.py` → 0 errors

**Residual risk:** low. Template-only visible change; a full-app render
was not performed (no CLI harness in this run) — the manual check in
Verification remains for a human. The alert/email snapshot surface still
shows old terminology (deferred).
