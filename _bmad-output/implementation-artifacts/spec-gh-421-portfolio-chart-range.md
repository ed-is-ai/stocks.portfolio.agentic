---
title: 'Portfolio value chart: selectable time range with server-side downsampling (gh-421)'
type: 'feature'
created: '2026-08-30'
status: 'done'
review_loop_iteration: 1
followup_review_recommended: true
context: []
warnings: ['oversized']
baseline_revision: '36d8a3bd'
final_revision: '1b33b8ef'
---

<intent-contract>

## Intent

**Problem:** `PortfolioSnapshotsRepository.history` returns `ORDER BY id DESC
LIMIT 180` — the last 180 snapshots *by count*. Snapshots are written on every
value refresh and every SIPP import, so the chart's x-axis spans an
unpredictable, uncontrollable amount of time, and the user cannot change it.

**Approach:** Add a `1M / 3M / 12M / 3Y / 5Y` range selector above the chart
(default `12M`, hard cap `5Y`). Filter snapshots by a `timestamp >= cutoff`
predicate for the chosen range, then downsample server-side to ~250 points
(last value per time bucket). A new `GET /partials/portfolio/chart` fragment
endpoint re-renders only the chart card on range change — no positions/prices/
cash rebuild. The chosen range persists per-browser in `localStorage`, mirroring
the existing `activePortfolioId` pattern, and rides the full portfolio render
via `hx-vals` so there is no load-flash. Decisions recorded on issue #421
(2026-08-30 triage comment).

## Boundaries & Constraints

**Always:**
- Range presets are exactly `1M, 3M, 12M, 3Y, 5Y`; any other/absent value
  resolves to `12M`. Cutoffs are day-counts relative to now: 30 / 91 / 365 /
  1096 / 1826.
- `repo.history(portfolio_id, limit=180, since=None)` — `since` adds
  `AND timestamp >= ?`; the existing `limit`/ordering/reverse behaviour is
  unchanged when `since` is `None`, so every current caller is unaffected.
- Downsampling is a pure, unit-tested helper: given rows and `max_points`, it
  buckets by `ceil(span_days / max_points)`-day windows and keeps the last row
  of each bucket, preserving chronological order and always keeping the final
  row. `max_points = 250`.
- Trades are filtered to `date >= cutoff` before `_trade_markers`, so a trade
  older than the window shows no marker. In-window trades whose snapshot was
  downsampled out keep snapping to the nearest retained label (existing
  `_trade_markers` behaviour, unchanged).
- The chart card renders whenever the portfolio has any snapshot history; the
  `<canvas>` + Chart.js script render only when the *selected range* has ≥ 2
  points, otherwise a short "No data in this range" message with the selector
  still usable.
- The Chart.js instance is stored on `window` and `.destroy()`-ed at the top of
  the card script before `new Chart(...)`, so repeated fragment swaps don't leak
  or collide.
- The selector is keyboard-operable and its current value is visually marked;
  freshness of state is not conveyed by colour alone.

**Block If:**
- Delivering an AC would require changing how snapshots are written, the
  snapshot schema, or trade-history semantics.

**Never:**
- No change to the legacy `portfolio_value.csv` path (`_load_portfolio_history`
  with no `portfolio_id`) — everything is portfolio-scoped post-#147.
- No new charting library; Chart.js 4.4.4 (already loaded) only.
- No server-side storage of the range preference; `localStorage` only.
- No second full-context rebuild on range change — the fragment endpoint must
  not call `default_portfolio_context` / `positions_from_input_snapshot`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Default load | no stored range | Chart shows last 12 months | — |
| Widen | click `5Y` | Fragment swaps; chart shows ≤ 5y; `localStorage` set | — |
| Persist | reload after choosing `3M` | `hx-vals` sends `range=3M`; full render is 3M | — |
| Bad range param | `?range=9Q` | Resolved to `12M` | Silently normalised |
| Long history | 5y of ~2/day snapshots | Downsampled to ≤ 250 points, last-per-bucket, endpoints kept | — |
| Empty window | `1M` on a portfolio dormant 6 months | Selector visible, "No data in this range" message | Not an error |
| Old trade | buy 2 years ago, `3M` view | No marker for it | — |
| In-window trade, point dropped | buy last week, downsampled out | Marker snaps to nearest retained label | — |
| < 2 total snapshots | brand-new portfolio | Whole chart card hidden (as today) | — |

</intent-contract>

## Code Map

- `app/services/series_downsample.py` -- **new**: `downsample_last_per_bucket(rows, max_points)` pure helper.
- `app/repositories/portfolio_snapshots_repo.py:58` -- `history` gains `since: str | None = None`.
- `app/agents/trader/trader_agent.py:2241` -- `snapshot_history` gains `since: str | None = None`, passes through.
- `app/services/portfolio_service.py` -- `_load_portfolio_history(portfolio_id, range_key="12M")` translates preset→cutoff, passes `since`, downsamples; `portfolio_input_snapshot` / `portfolio_partial_context` / `default_portfolio_context` thread `range_key="12M"`; new `chart_fragment_context(portfolio_id, range_key)` builds only the 9 `chart_*` keys + `chart_range` + `chart_has_history` (no positions).
- `app/api/params.py` -- **new** `chart_range(value: str | None) -> str` whitelist helper (default `"12M"`).
- `app/api/routes/views.py:62` -- `/partials/portfolio` handler accepts `range: str | None = None`, passes `chart_range(range)` into `default_portfolio_context`; **new** `GET /partials/portfolio/chart` → `chart_fragment_context` → renders `_portfolio_chart.html`.
- `app/api/templates/_portfolio_chart.html` -- **new**: the chart card (extracted from `_portfolio.html:354-480`) + the range `<select>`/button group + the teardown-guarded Chart.js script. Stable `id="portfolio-chart-card"`.
- `app/api/templates/_portfolio.html` -- replace L354-480 with `{% include "_portfolio_chart.html" %}`; pass `portfolio_id`, `chart_range`.
- `app/api/templates/index.html` -- add `activeChartRange()` / `setChartRange()` beside `activePortfolio()` (~L760); add `range: activeChartRange()` to the portfolio tab button's `hx-vals` (L549) and to the account `<select>`'s request (`_portfolio.html:167`).
- Tests: `tests/test_series_downsample.py` (new), `tests/test_portfolio_chart_route.py` (new), plus additions to `tests/test_portfolio_service.py` and `tests/test_portfolio_snapshots_repo.py` (or `test_multi_portfolio.py`).

## Tasks & Acceptance

**Execution:**
- [x] `app/services/series_downsample.py` -- pure `downsample_last_per_bucket(rows, max_points)` -- bucket by `max(1, ceil(span_days/max_points))` days over the row timestamps, keep the last row per bucket, always keep the last row, preserve order; `<= max_points` rows out; empty/1-row inputs returned unchanged.
- [x] `app/repositories/portfolio_snapshots_repo.py` -- add `since` param to `history`; append `AND timestamp >= ?` only when provided; keep `ORDER BY id DESC LIMIT ?` then reverse.
- [x] `app/agents/trader/trader_agent.py` -- `snapshot_history` forwards `since`.
- [x] `app/services/portfolio_service.py` -- range→cutoff map; `_load_portfolio_history` accepts `range_key`, computes `since` ISO cutoff, calls `snapshot_history(portfolio_id, since=cutoff)`, downsamples rows to 250 before shaping; thread `range_key="12M"` through `portfolio_input_snapshot`, `portfolio_partial_context`, `default_portfolio_context`; filter `snapshot.trades` to `>= cutoff` before `_trade_markers`; add `chart_range` + `chart_has_history` to the context; add `chart_fragment_context(portfolio_id, range_key)`.
- [x] `app/api/params.py` -- `chart_range` whitelist helper.
- [x] `app/api/routes/views.py` -- `/partials/portfolio` reads `range`; new `GET /partials/portfolio/chart` returning `_portfolio_chart.html`.
- [x] `app/api/templates/_portfolio_chart.html` -- new partial: `#portfolio-chart-card`, range selector (each option `hx-get="/partials/portfolio/chart"` with `portfolio_id`+`range`, `hx-target="#portfolio-chart-card"`, `hx-swap="outerHTML"`, `onclick="setChartRange('<preset>')"`, current marked `aria-current`), canvas+script when `chart_points >= 2` else the empty-range message, Chart.js script destroying `window.__portfolioChart` first.
- [x] `app/api/templates/_portfolio.html` -- swap L354-480 for the include.
- [x] `app/api/templates/index.html` -- `activeChartRange()`/`setChartRange()`; `hx-vals` range on the portfolio tab button and account select.
- [x] `tests/test_series_downsample.py` -- cover the I/O matrix rows for the helper (empty, 1 row, already-small, long span capped, endpoints retained, order preserved).
- [x] `tests/test_portfolio_chart_route.py` -- `GET /partials/portfolio/chart` renders the card for each preset; bad `range` → 12M; empty-window message; markers absent for out-of-window trades.
- [x] `tests/test_portfolio_service.py` / snapshot-repo test -- `_load_portfolio_history` range filtering + downsampling; `history(since=...)` predicate; existing callers unaffected when `since` omitted.

**Acceptance Criteria:**
- Given a portfolio with > 12 months of snapshots, when the portfolio tab loads with no stored range, then the chart shows only the last 12 months.
- Given the chart, when the user selects a wider range up to 5Y, then only the chart card re-renders (no tab flash), the window widens, and the choice is written to `localStorage`.
- Given a stored range, when the portfolio tab is re-opened or the account is switched, then the full render already reflects that range without a follow-up request.
- Given any range, when the series exceeds 250 points, then it is downsampled to ≤ 250 last-per-bucket points with the first and last retained.
- Given a trade dated before the selected window, when the chart renders, then it has no marker; an in-window trade whose point was dropped still shows a marker on the nearest retained label.
- Given a selected range with fewer than 2 in-window snapshots, then the selector still renders and a "No data in this range" message replaces the canvas.
- Given `repo.history` is called without `since` (every existing caller), then its result is byte-identical to today.

## Design Notes

**Why `hx-vals` for the full render but a fragment endpoint for clicks.** The tab
button and account `<select>` already send `portfolio_id` via `hx-vals`/`name`;
adding `range: activeChartRange()` there makes the first paint correct with zero
extra requests. Range-button *clicks* must not rebuild positions/prices/cash, so
they hit the lean fragment endpoint and swap `#portfolio-chart-card` only.

**Downsample math.** `span_days = (last_ts - first_ts).days`;
`bucket = max(1, ceil(span_days / 250))`; walk rows, emit a row when the next
row crosses into a new `bucket`-day window or is the last row. Deterministic,
no external dep, and "day buckets for short ranges, week+ for long" falls out
(5Y ≈ 1826 d → 8-day buckets → ~228 pts; 1M → 1-day buckets).

**Canvas reuse.** htmx runs `<script>` in swapped content. The card script does
`window.__portfolioChart?.destroy(); window.__portfolioChart = new Chart(...)`
so an `outerHTML` swap of the card can't orphan a live instance or trip
"Canvas is already in use".

## Verification

**Commands:**
- `uv run pytest tests/test_series_downsample.py tests/test_portfolio_chart_route.py tests/test_portfolio_service.py tests/test_portfolio_template.py tests/test_multi_portfolio.py -q` -- expected: pass.
- `uv run pytest -q` -- expected: no regressions.
- `uv run ruff format . && uv run ruff check .` -- clean on changed files.
- `uv run pyrefly check` -- no new errors vs `36d8a3bd`.

**Manual checks:**
- Portfolio tab: selector shows `12M` active, chart ~1 year. Click `5Y` → only the card updates. Reload tab → still `5Y`. Switch account → `5Y` retained. Pick `1M` on a dormant portfolio → "No data in this range", selector still usable.

## Spec Change Log

### 2026-08-30 — Review pass 1 clarifications (no re-derive)

- **`since` lifts the count cap.** The original tasks said `_load_portfolio_history`
  "calls `snapshot_history(portfolio_id, since=cutoff)`" and separately
  "downsamples rows to 250", but left `history`'s `LIMIT 180` in force — so a
  wide window with >180 in-range snapshots silently returned only the newest 180
  (dropping the oldest years) and the 250-point downsampler was unreachable.
  Clarification: when `since` is provided the query is a *time* window, not a
  *count* window — `history` applies no `LIMIT` in that case (a high safety
  ceiling of 20000 rows guards against pathological data). This is consistent
  with the intent contract, which already scoped the "unchanged behaviour"
  guarantee to `since is None`.
- **Downsampler must yield ≥ 2 points when given ≥ 2 rows.** A window whose rows
  all share one calendar day (`span_days == 0`) collapsed to a single point via
  the two endpoint-forcing writes hitting the same index. Guard added: if fewer
  than 2 rows survive bucketing, return `[rows[0], rows[-1]]`.
- **Fragment endpoint keeps the card shell.** `GET /partials/portfolio/chart`
  with `hx-swap="outerHTML"` must always return `#portfolio-chart-card` (with the
  range selector), even for the empty-portfolio / no-history / legacy-CSV cases —
  otherwise the swap deletes the selector and the user cannot recover without a
  full tab reload. The legacy-CSV (`portfolio_id is None`) branch uses the same
  `>= 2 points` history check the inline render uses, not a hardcoded `False`.

## Review Triage Log

### 2026-08-30 — Review pass 1
- intent_gap: 0
- bad_spec: 0
- patch: 11: (high 1, medium 3, low 7)
- defer: 2
- reject: 3
- addressed_findings:
  - `[high]` `[patch]` `_load_portfolio_history` kept `history`'s `LIMIT 180`, so 3Y/5Y windows returned only the newest 180 snapshots and the 250-point downsampler was dead code — `since` now suppresses the `LIMIT` (safety ceiling 20000); spec clarified.
  - `[medium]` `[patch]` `downsample_last_per_bucket` collapsed a single-calendar-day window (`span_days == 0`, `len > max_points`) to one point via `kept[0]=rows[0]` then `kept[-1]=rows[-1]` on the same index — guard returns `[rows[0], rows[-1]]` when < 2 survive.
  - `[medium]` `[patch]` `chart_fragment_context` returned an empty body for no-portfolio / empty-list / legacy-CSV, so the `outerHTML` swap deleted `#portfolio-chart-card` and its selector — fragment now always renders the card shell; the `portfolio_id is None` branch uses the same `>= 2 points` check as the inline render.
  - `[medium]` `[patch]` `activeChartRange()` / `setChartRange()` had no `try/catch` around `localStorage`; a throw inside `hx-vals='js:{…}'` aborts the whole vals object (which also carries `portfolio_id`) — both wrapped, defaulting to `12M` / no-op.
  - `[low]` `[patch]` ISO-string cutoff comparison was unsound when a stored timestamp had zero microseconds (`+` `0x2B` < `.` `0x2E`) — cutoff truncated to a `YYYY-MM-DD` date prefix, which the range presets are defined in anyway.
  - `[low]` `[patch]` Downsampler bucket count could be `max_points + 1` for `span_days == max_points`; docstring promised `<= max_points` — clamp the tail.
  - `[low]` `[patch]` "No data in this range" branch rendered no `<script>`, so a range switch onto it orphaned the prior Chart instance and its resize listeners — the `{% else %}` branch now emits a one-line teardown script.
  - `[low]` `[patch]` route params named `range` shadowed the builtin — renamed to `range_key` / `chart_range_param`.
  - `[low]` `[patch]` toggle buttons used `aria-current="true"`; correct attribute for a `btn-group` toggle is `aria-pressed` — switched.
  - `[low]` `[patch]` `CHART_RANGE_DAYS` (30/91/365/1096/1826) looked arbitrary — added a comment explaining the leap-day allowance on the multi-year presets.
  - `[low]` `[patch]` `test_empty_window_shows_no_data_message` used a boundary date (`day-30` vs a 30-day cutoff) that could flake on setup latency — fixture moved to a clearly out-of-window date.
  - deferred: the range selector still renders on the legacy single-portfolio CSV view where `range_key` is ignored (vestigial path, unreachable from the live UI post-#147); `chart_has_history` issues an extra `snapshot_history(limit=2)` query per render rather than deriving from already-loaded data.
  - rejected: `aria` nit already covered; "range-to-days looks arbitrary" downgraded to the comment patch above; localStorage flaw "also in pre-existing `activePortfolio`" — out of scope for this issue.

## Auto Run Result

Status: done

### Summary

The portfolio value chart had no time control — `PortfolioSnapshotsRepository.history`
returned the newest 180 snapshots *by count*, so the x-axis spanned an arbitrary,
uncontrollable amount of time. This adds a `1M / 3M / 12M / 3Y / 5Y` range selector
(default `12M`, hard 5Y cap), a `timestamp >= cutoff` time-window query, server-side
downsampling to ≤ 250 last-per-bucket points, a dedicated
`GET /partials/portfolio/chart` fragment endpoint that re-renders only the chart
card on range change, and per-browser `localStorage` persistence that rides the
full portfolio render via `hx-vals` (no load-flash).

### Files changed

- `app/services/series_downsample.py` (new) — pure `downsample_last_per_bucket(rows, max_points)`: `ceil(span_days / max_points)`-day buckets, last row per bucket, both real endpoints retained, `< 2` survivors → `[first, last]`, `> max_points` → tail-clamped.
- `app/repositories/portfolio_snapshots_repo.py` — `history()` gains `since`; when set, the window is by time (safety ceiling 20000) not the 180 count cap; `since is None` is byte-identical to before.
- `app/agents/trader/trader_agent.py`, `app/services/trader_service.py` — `snapshot_history` forwards `since`.
- `app/services/portfolio_service.py` — `CHART_RANGE_DAYS` (30/91/365/1096/1826), date-prefix `chart_cutoff_iso`, `_load_portfolio_history(portfolio_id, range_key)` (window + downsample), `range_key` threaded through `portfolio_input_snapshot` / `portfolio_partial_context` / `default_portfolio_context`, marker trades pre-filtered to `>= cutoff`, `chart_range` + `chart_has_history` context keys, lean `chart_fragment_context`.
- `app/api/params.py` — `chart_range()` whitelist helper.
- `app/api/routes/views.py` — `/partials/portfolio` reads `range` (aliased param, no builtin shadow); new `GET /partials/portfolio/chart`.
- `app/api/templates/_portfolio_chart.html` (new) — `#portfolio-chart-card` shell + `aria-pressed` range btn-group, always rendered; canvas + teardown-guarded Chart.js script when ≥ 2 in-window points, else a "No data in this range" message that also tears down any live chart.
- `app/api/templates/_portfolio.html` — chart block replaced with a guarded include.
- `app/api/templates/index.html` — `activeChartRange()` / `setChartRange()` (try/catch around `localStorage`); `range` added to the portfolio tab button's `hx-vals`.
- Tests: `tests/test_series_downsample.py` (new), `tests/test_portfolio_chart_route.py` (new), additions to `tests/test_portfolio_service.py`, `tests/test_multi_portfolio.py`, `tests/test_api_params.py`.

### Review

Two adversarial reviewers (Blind Hunter + Edge Case Hunter), one pass. Both
independently found the load-bearing defect: `_load_portfolio_history` left
`history()`'s `LIMIT 180` in force under `since`, so wide windows returned only
the newest 180 rows and the 250-point downsampler was unreachable.

- **Patches applied: 11** (1 high, 3 medium, 7 low) — see Review Triage Log. The
  high + one medium reshaped the core data path; three specify empty-state /
  teardown parity; the rest are correctness nits (ISO-compare, bucket clamp,
  builtin shadow, `aria-pressed`, flaky-test fixture, comments).
- **Deferred: 2** — the selector renders (inert) on the vestigial legacy-CSV path;
  `chart_has_history` costs an extra `snapshot_history(limit=2)` query. Both in
  `deferred-work.md`.
- **Rejected: 3** — duplicates / out-of-scope pre-existing `activePortfolio` gap.

### Verification

- `uv run pytest -q` → **2438 passed**, 5 warnings.
- Targeted suite (`test_series_downsample`, `test_portfolio_chart_route`, `test_portfolio_service`, `test_portfolio_template`, `test_multi_portfolio`, `test_api_params`) → all pass.
- `uv run ruff format` / `ruff check app tests` → clean (only the new test file self-reformatted).
- `uv run pyrefly check` → 123 errors, **identical to the `36d8a3bd` baseline, 0 new**.

### Residual risks

- `followup_review_recommended: true` — the re-derived data path (LIMIT lift +
  downsampler interaction + `chart_has_history`) was fixed but not independently
  re-reviewed. Worth a second pass focused on `portfolio_snapshots_repo.history`
  and `_load_portfolio_history`.
- The Chart.js teardown and htmx `<script>`-in-fragment execution are only
  assertable at the markup level; the "swap onto No-data then back" cycle was not
  exercised in a real browser.
- Full-tab re-renders triggered by trade/import/refresh routes still render the
  chart at the 12M default (they call `portfolio_partial_context` without
  `range_key`); the stored range reasserts on the next tab or account switch.
