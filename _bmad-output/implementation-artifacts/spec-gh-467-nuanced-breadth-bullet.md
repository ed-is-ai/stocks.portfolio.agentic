---
title: 'gh-467: Nuanced market-breadth bullet — separate level, near-term direction, long-term trend'
type: 'bugfix'
created: '2026-09-01'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
baseline_revision: 'b7fae261ebeadd4cc1a04bf558855272c0155080'
final_revision: 'd290ce0a'  # HEAD differs by this line only (amend self-reference)
context: []
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The market-narrative breadth bullet renders `MarketBreadth.trend_rising` — the slope of the 200-day *moving average of the breadth index*, an extremely lagging line — as the bare word "(rising)". On 2026-08-31, raw breadth had fallen from ~74% to 68% over the week yet the bullet (and the LLM prompt line feeding it) still said "rising", and the LLM expanded that into "breadth is broad and improving … argues against an imminent broad-market breakdown."

**Approach:** Parse a short-horizon comparison (raw % and the 8-day smoothed value ~5 trading rows back) plus the feed's 50-day breadth series from the CSV, add those as optional fields on `MarketBreadth`, and rewrite both the deterministic bullet and the LLM prompt line to state three things separately: current **level**, **near-term direction** (past ~week), and **long-term trend** (the 200-day-MA slope, explicitly labelled as long-term). Never emit a standalone "(rising)".

## Boundaries & Constraints

**Always:** keep the client fail-soft — a short feed, missing 50-day columns, or an unparseable prior row degrades the new fields to `None`/`False` and never raises or nulls the whole reading; new fields are `Optional` with safe defaults so existing construction sites and cached JSON stay valid; fold every new semantic field into `narrative_input_digest`; bump `NARRATIVE_VERSION`; leave `pct_above_200dma`, `smoothed_8ma`, `as_of`, `is_fresh`, `days_old`, `retrieval_source` unchanged; line length ≤ 88; type hints + docstrings on new/changed functions.

**Block If:** a live fetch shows the 50-day column names are not `Breadth_50_Index_Raw` / `Bearish_Signal_50` (200-day header already confirmed live this session — do not re-block on that).

**Never:** rename or drop `trend_rising` (output relabelling only); add a network call or second feed; persist anything beyond the existing narrative JSON; touch scanner UI templates; change the 7-day freshness rule or the daily UTC cache contract (#343).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Falling week, long-term MA still up | latest raw 0.68, row ~5 back 0.74, `Breadth_200MA_Trend`=1, `Breadth_50_Index_Raw`=0.50 | `near_term_pct_delta` ≈ -6.0, `trend_rising` True, `pct_50dma` 50.0; bullet says level 68%, "narrowing"/down over the past week, long-term trend still up; NO bare "(rising)" and no unqualified "improving" | n/a |
| Rising week | latest raw 0.72, row ~5 back 0.66 | `near_term_pct_delta` ≈ +6.0; bullet says level + "broadening" over the past week | n/a |
| Flat week | delta within ±0.5 pt | near-term direction "little changed"; no directional verb | n/a |
| Short feed | < 6 data rows | near-term / 50-day delta fields `None`; bullet omits the near-term clause, still prints level + long-term trend | no raise |
| Missing 50-day columns | header has only the 200-day columns (as in existing tests) | `pct_50dma` / `near_term_50dma_pct_delta` / `near_term_bearish_signal` = `None`/`False`; bullet omits the 50-day clause | no raise |
| Unparseable prior row | row ~5 back has a bad number | near-term delta fields `None`; latest-row fields unaffected | swallow, continue |
| `breadth is None` | feed failed upstream | `_breadth_bullet` returns `None` (unchanged) | n/a |

</intent-contract>

## Code Map

- `app/schemas/market_breadth.py` -- `MarketBreadth` model; add optional near-term + 50-day fields with safe defaults.
- `app/integrations/market_breadth.py` -- `_parse_latest` reads only the newest row today; extend to sort rows, pick a `_LOOKBACK_ROWS`-back comparison row, and read the 50-day columns; keep every failure fail-soft.
- `app/agents/scanner/market_narrative.py` -- `_breadth_bullet` (rewrite to level / near-term / long-term prose); `narrative_input_digest` breadth dict (add new fields); `NARRATIVE_VERSION` bump.
- `app/integrations/anthropic_client.py` -- `_build_user_prompt` breadth line (state level + near-term + long-term separately, label `trend_rising` as the long-term 200-day-average trend); `_SYSTEM_PROMPT` one-clause nudge to read near-term figures for improving/deteriorating.
- `tests/test_market_breadth.py` -- new parse cases (falling week, short feed, missing 50-day columns, bad prior row).
- `tests/test_market_narrative.py` -- regression: falling week + `trend_rising=True` must not yield "rising"/unqualified "improving"; update existing bullet-text assertions.
- `tests/test_anthropic_client.py` -- prompt asserts the relabelled/expanded breadth line.
- `tests/test_narrative_cache.py` -- digest changes when a new breadth field changes.

## Tasks & Acceptance

**Execution:**
- [x] `app/schemas/market_breadth.py` -- add `near_term_pct_delta: float | None = None`, `pct_50dma: float | None = None`, `near_term_50dma_pct_delta: float | None = None`, `near_term_bearish_signal: bool = False`; docstring each; keep field order stable.
- [x] `app/integrations/market_breadth.py` -- add `_LOOKBACK_ROWS = 5`; in `_parse_latest` sort rows by `Date`, take last as `latest` and index `-1-_LOOKBACK_ROWS` (if present) as `prior`; compute `near_term_pct_delta` = `pct(latest.raw) - pct(prior.raw)`; read `Breadth_50_Index_Raw` → `pct_50dma`, compute `near_term_50dma_pct_delta`, read `Bearish_Signal_50` → `near_term_bearish_signal`; wrap prior-row / 50-day reads so any `KeyError`/`ValueError`/`TypeError` leaves that field `None`/`False`.
- [x] `app/agents/scanner/market_narrative.py` -- rewrite `_breadth_bullet` to compose: `S&P 500 breadth: {level}% of members above their 200DMA` + a near-term clause from `near_term_pct_delta` ("broadening"/"narrowing"/"little changed" over the past ~week, with the 50-day level + direction when present) + a long-term clause ("long-term (200-day-average) breadth trend {rising|falling|flat}") + existing divergence + provenance; add the new fields to the `breadth` dict in `narrative_input_digest` (quantise deltas to 1 dp, `pct_50dma` to 0); bump `NARRATIVE_VERSION` to `"2"`.
- [x] `app/integrations/anthropic_client.py` -- rewrite the breadth line in `_build_user_prompt` to give the model level, near-term (past-week) direction from the delta fields, and the long-term 200-day-average trend as three labelled facts; add one clause to `_SYSTEM_PROMPT` telling the model to judge "improving vs deteriorating" from the near-term figures, not the long-term trend.
- [x] `tests/test_market_breadth.py` -- add cases per the I/O matrix (falling week with `Breadth_200MA_Trend=1`; < 6 rows; header without 50-day columns; bad prior-row number).
- [x] `tests/test_market_narrative.py` -- add the falling-week regression; update the existing `test_breadth_and_congress_and_myb_bullets` / provenance assertions to the new wording.
- [x] `tests/test_anthropic_client.py` -- update `test_prompt_includes_breadth_and_congress` for the new labelled line.
- [x] `tests/test_narrative_cache.py` -- assert a changed `near_term_pct_delta` produces a different `narrative_input_digest`.

**Acceptance Criteria:**
- Given a feed where raw breadth falls week-on-week while `Breadth_200MA_Trend` is positive, when the deterministic narrative is built, then the breadth bullet states the current level, describes near-term breadth as narrowing/declining, labels the rising figure explicitly as the long-term 200-day-average trend, and contains no standalone "(rising)" or unqualified "improving".
- Given the same feed, when the LLM user prompt is built, then it presents level, near-term direction, and long-term trend as separate labelled facts.
- Given a feed with fewer than 6 rows or without the 50-day columns, when parsed, then `fetch_market_breadth` still returns a valid `MarketBreadth` with the new fields `None`/`False` and the bullet omits the missing clauses.
- Given any new breadth field changes value, when `narrative_input_digest` runs, then the digest differs (cache correctly invalidated).
- Given `pyrefly check`, `ruff format --check`, `ruff check`, and the full affected test modules, when run, then all pass.

## Spec Change Log

## Review Triage Log

### 2026-09-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 11: (high 0, medium 3, low 8)
- defer: 2
- reject: 4
- addressed_findings:
  - `[medium]` `[patch]` 50-day breadth info was dropped whenever the 200-day near-term delta was "little changed" or unavailable (short feed) — `_near_term_breadth_clause` and the anthropic prompt block restructured so the 200-day and 50-day parts render independently.
  - `[medium]` `[patch]` `near_term_bearish_signal` (feed `Bearish_Signal_50`) was parsed + digested but never rendered, while `_SYSTEM_PROMPT` told the LLM to judge deterioration from near-term data — now surfaced as "50-day breadth-divergence flag set" in both the bullet and the prompt.
  - `[medium]` `[patch]` `~{abs:.0f} pts` produced ungrammatical/contradictory text ("narrowing … down ~0 pts") — switched to `.1f`, matching digest precision.
  - `[low]` `[patch]` long-term boolean trend routed through `_direction_word` with sentinel floats, silently coupling it to `NEAR_TERM_FLAT_BAND` — replaced with a direct 3-way `trend_rising` check.
  - `[low]` `[patch]` `_near_term_breadth_clause` dispatched on the display string `"little changed"` — now branches on the numeric classification.
  - `[low]` `[patch]` flat band duplicated (`_NEAR_TERM_FLAT_BAND` vs a hard-coded `0.5` in `anthropic_client`) — hoisted to one `NEAR_TERM_FLAT_BAND` in `app/schemas/market_breadth.py`, imported by both.
  - `[low]` `[patch]` "over the past week" asserted regardless of the real date gap — added a 21-day calendar-span guard nulling the near-term deltas on a feed outage / negative gap.
  - `[low]` `[patch]` duplicate latest-`Date` rows: `max()`→`sort()[-1]` silently changed the tie-break — documented (restated row wins) and hardened the sort key against `Date=None`.
  - `[low]` `[patch]` short-feed vs `_SYSTEM_PROMPT` contradiction — softened the clause from "NOT from the long-term trend" to "primarily … rather than".
  - `[low]` `[patch]` LLM prompt could render "(-0.0 pts)" — normalise `-0.0` before `:+.1f` formatting.
  - `[low]` `[patch]` feed literal `"nan"`/`"inf"` on the new delta paths would produce `NaN` fields and abort `narrative_input_digest` every run — added `math.isfinite` guards nulling the affected fields.
  - deferred: pre-existing `_to_pct` accepts `"nan"`/`"inf"` for the primary `pct_above_200dma` field; pre-existing `_parse_latest` is not self-contained fail-safe (relies on the caller's `try/except`).

## Design Notes

Semantics mismatch: `trend_rising` answers "is the 200-day MA of breadth sloping up" (turns over months late) but is rendered as "is breadth improving now". Fix adds the missing short-horizon signal rather than reinterpreting the existing one.

Target bullet (2026-08-31 data):
> S&P 500 breadth: 68% of members above their 200DMA — narrowing over the past week (down ~6 pts; 50-day breadth 50% and falling). Long-term (200-day-average) breadth trend still rising. Fetched from source.

`_LOOKBACK_ROWS = 5` ≈ one trading week (feed publishes on trading days only). "little changed" band ±0.5 pt so noise doesn't flip the verb.

## Verification

**Commands:**
- `cd .worktrees/gh-467-nuanced-breadth-bullet && uv run pytest tests/test_market_breadth.py tests/test_market_narrative.py tests/test_anthropic_client.py tests/test_narrative_cache.py` -- expected: all pass
- `uv run pyrefly check` -- expected: no new errors
- `uv run ruff format --check . && uv run ruff check .` -- expected: clean

## Auto Run Result

**Status:** done

**Change:** The market-narrative breadth bullet no longer renders the lagging 200-day-MA-of-breadth slope (`trend_rising`) as a bare "(rising)". `_parse_latest` now also reads a ~5-trading-row-back comparison and the feed's 50-day breadth series; `MarketBreadth` carries four new optional fields (`near_term_pct_delta`, `pct_50dma`, `near_term_50dma_pct_delta`, `near_term_bearish_signal`); `_breadth_bullet` and the LLM prompt line state current **level**, **near-term (past-week) direction**, and **long-term (200-day-average) trend** as three separate labelled facts, and surface the feed's 50-day divergence flag. `NARRATIVE_VERSION` bumped to `"2"` (invalidates cached narratives); the new fields are folded into `narrative_input_digest`.

**Files changed:**
- `app/schemas/market_breadth.py` -- 4 new optional `MarketBreadth` fields; shared `NEAR_TERM_FLAT_BAND` constant.
- `app/integrations/market_breadth.py` -- `_parse_latest`: prior-row + 50-day parsing, fail-soft (`Date=None` sort guard, 21-day span guard, `math.isfinite` guards, tie-break comment).
- `app/agents/scanner/market_narrative.py` -- `_breadth_bullet` rewritten (level / independent near-term 200-day & 50-day parts / labelled long-term trend / both divergence flags); `narrative_input_digest` + `NARRATIVE_VERSION` bump.
- `app/integrations/anthropic_client.py` -- breadth prompt line rewritten to three labelled facts; softened `_SYSTEM_PROMPT` clause.
- `skills/market-narrative/references/system_prompt.md` -- mirrors `_SYSTEM_PROMPT` verbatim (drift-guard).
- `tests/test_market_breadth.py`, `tests/test_market_narrative.py`, `tests/test_anthropic_client.py`, `tests/test_narrative_cache.py` -- new + updated cases.

**Review:** 2 adversarial passes → 11 patches applied (3 medium, 8 low), 2 deferred (both pre-existing), 4 rejected. No intent gaps, no spec repair loopbacks.

**Verification:**
- `uv run pytest tests/test_market_breadth.py tests/test_market_narrative.py tests/test_anthropic_client.py tests/test_narrative_cache.py tests/test_alert_digest_held.py tests/test_stock_scanner_ui.py` → 132 passed.
- `uv run pyrefly check` (4 changed source files) → 0 errors.
- `uv run ruff format --check` / `uv run ruff check` on all 8 touched files → clean.

**Example output (2026-08-31 falling week):**
> S&P 500 breadth: 68% of members above their 200DMA — narrowing over the past week (down ~6.0 pts); 50-day breadth 50% and falling. Long-term (200-day-average) breadth trend rising. Fetched from source.

**Residual risks:**
- Pre-existing `"nan"`/`"inf"` handling on the primary `pct_above_200dma` field (deferred) still aborts `narrative_input_digest` every run if the feed ever emits it.
- `_LOOKBACK_ROWS = 5` assumes trading-day cadence; the 21-day span guard covers outages but a 6–13-day gap still reads as "past week" (mitigated by the "~" hedge).
- Two independent surfaces (deterministic bullet + LLM prompt) now share `NEAR_TERM_FLAT_BAND` but still assemble wording separately.

**Follow-up review recommended:** true — the review pass reshaped the core prose-assembly logic and added several fail-soft guards; an independent look at the final `_near_term_breadth_clause` / prompt wording and the new edge-case tests is worthwhile.
