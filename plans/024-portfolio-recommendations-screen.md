# Plan 024: Portfolio-specific Sell/Hold/Buy recommendations screen (story #441)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat c12ef330..HEAD -- app/services/backtest/strategy_protocol.py app/services/portfolio_service.py app/core/ticker_identity.py app/schemas/analysis_artifact.py plans/023-portfolio-strategy-assignment.md`
> If anchors changed since `c12ef330`, compare the "Current state" excerpts
> against the live code before proceeding; on a mismatch, treat it as a STOP
> condition. Plan 023 must be DONE (its status row says so) before starting.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: 023 (assignment storage + service seam)
- **Category**: feature
- **Planned at**: commit `c12ef330`, 2026-08-30
- **GitHub story**: ed-is-ai/Agents.stocks#441 (parent feature #439)
- **UX reference**: `docs/mockups/portfolio-strategy-recommendations.html`
  (surfaces 2 and 5)

## Why this matters

With an assignment in place (plan 023), the owner needs to review what the
assigned Strategy would do for *this* account before deciding anything. This
plan adds a read-only, deterministic recommendations screen: Sell / Hold / Buy
groups computed from the already-published scan artifact plus the portfolio's
current holdings, via the Strategy's own runtime code — never a re-implementation
of Strategy rules in host code, never a trade, never a network fetch.

## Current state

- `StrategyProtocolV1` (`app/services/backtest/strategy_protocol.py:342`,
  `@runtime_checkable`): `entry_signals(view: MarketViewV1, parameters) ->
  list[Signal]`, `exit_signals(view, portfolio: PortfolioView, parameters) ->
  list[Signal]`, `position_size(...)`. Pure result validators exist alongside.
  Runtimes are loaded from `runtime_path`/`runtime_files` declared by
  `StrategyDescriptorV1`; `source_digest` comes from
  `app/services/backtest/source_manifest.py`.
- `MarketViewV1` / `PortfolioView` are the Strategy-facing read contracts used
  by historical backtests. There is **no current-scan adapter yet** — this
  plan builds it. The scan artifact envelope is
  `AnalysisArtifact(meta: AnalysisArtifactMeta(run_id, generated_at), records)`
  (`app/schemas/analysis_artifact.py`); records are per-ticker scan rows read
  fail-soft by `PortfolioService.load_analysis()`.
- Canonical ticker matching: `app/core/ticker_identity.py` —
  `load_aliases()`, `canonical_ticker(ticker, aliases)` (alias-chain walk to
  fixed point; `AmbiguousTickerAliasError` on cycles).
- Portfolio holdings: `TraderService.get_portfolio(prices, display_info,
  portfolio_id)` / `PortfolioInputSnapshot` via `PortfolioService`
  (`app/services/portfolio_service.py`); `default_portfolio_context` at line
  ~1103 is the context-builder precedent.
- Routes render partials via `templates.TemplateResponse(request,
  "_partial.html", context)`; auth guard `Depends(require_local_or_token)`;
  DI via `@lru_cache` providers in `app/api/dependencies.py`.
- Template parity tests exist as models: `tests/test_portfolio_template.py`,
  `tests/test_alert_ui_parity.py`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| New tests | `uv run pytest tests/test_portfolio_recommendation_service.py tests/test_recommendation_routes.py -v` | all pass |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |
| Types | `uv run pyrefly check` | no new errors |

## Scope

**In scope** (the only files you should create/modify):
- `app/schemas/portfolio_recommendation.py` — **new** frozen models
- `app/services/portfolio_recommendation_service.py` — **new**
- `app/services/backtest/scan_view.py` — **new** current-scan
  `MarketViewV1`/`PortfolioView` adapter
- `app/api/dependencies.py` — add `get_portfolio_recommendation_service`
- `app/api/routes/portfolios.py` — add `GET /portfolios/{id}/recommendations`
- `app/api/templates/_portfolio_recommendations.html` — **new**
- `app/api/templates/_portfolio.html` — Recommendations button (already
  mocked in plan 023's controls row; wire it here if 023 left it inert)
- `tests/test_portfolio_recommendation_service.py`,
  `tests/test_recommendation_routes.py`,
  `tests/test_scan_view_adapter.py` — **new**

**Out of scope** (do NOT touch):
- Any Strategy runtime under `skills/*`
- `StrategyProtocolV1`, `MarketViewV1`, `PortfolioView` definitions (extend
  structurally only if a contract gap blocks the adapter — see STOP)
- Email dispatch (plan 025), automatic execution, position sizing
- On-demand fetching of missing market history
- The generic scanner recommendation path (`app/core/recommendation.py`)

## Git workflow

- Branch: `feat/024-portfolio-recommendations-screen`
- Commit message: `feat(portfolio): strategy-driven sell/hold/buy recommendations screen`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Frozen recommendation models

New `app/schemas/portfolio_recommendation.py` (pydantic frozen, `extra="forbid"`,
`strict=True` — match `StrategyDescriptorV1` style):

```python
class RecommendationActionV1(BaseModel):    # Literal["sell", "hold", "buy"]
class RecommendationV1(BaseModel):          # action, ticker, canonical_ticker,
                                            # rule_code, reason, evidence_warnings
class RecommendationResultV1(BaseModel):    # portfolio_id, portfolio_revision,
                                            # analysis_run_id, generated_at,
                                            # market_session, freshness,
                                            # strategy_id, strategy_source_digest,
                                            # parameters, recommendations tuple,
                                            # evaluated_at
```

The result is the single typed contract both the screen (this plan) and the
email (plan 025) consume — screen/email parity depends on it.

**Verify**: `uv run python -c "from app.schemas.portfolio_recommendation import RecommendationResultV1; print('ok')"`

### Step 2: Current-scan market-view adapter

New `app/services/backtest/scan_view.py`: build a `MarketViewV1` and a
`PortfolioView` from (a) the current scan records and (b) the portfolio
snapshot.

- Normalize OHLCV **oldest-first** and bound it to **one evidenced market
  session** (the scan's session); expose detector output through fields
  compatible with what historical backtest views provide — inspect one
  existing historical view implementation and mirror its field names exactly.
- Do **not** fabricate historical provenance: fields the scan cannot evidence
  are absent/None, not synthesized.
- Resolve tickers through `canonical_ticker` + `load_aliases()`; unresolved
  aliases are surfaced on the view (e.g. `unresolved: tuple[str, ...]`), never
  silently dropped.

**Verify**: `uv run pytest tests/test_scan_view_adapter.py -v` (after Step 5).

### Step 3: `PortfolioRecommendationService`

New `app/services/portfolio_recommendation_service.py`:

- `recommend(portfolio_id) -> RecommendationResultV1` — pure with respect to
  inputs; **no provider/network fetch; never places or stages a trade**.
- Load assignment via `StrategyAssignmentService` (plan 023). No assignment →
  raise/return a typed `NoAssignment` state; the route renders the existing
  generic experience plus an assign link (AC #441.9).
- Assignment `unavailable` or runtime load failure → typed
  `EvaluationUnavailable` state with the reason (AC #441.10 — actionable
  state, not a 500).
- Load the published `ANALYSIS_JSON` envelope (`read_analysis_records` +
  `read_analysis_artifact_meta`); freshness from `generated_at` with the same
  exact-24h rule as plan 023.
- Load the portfolio snapshot; key any cache by (assignment identity,
  strategy source digest, parameters, analysis run_id, portfolio trade
  revision) so stale holdings can never be reused.
- Invoke the runtime: `exit_signals(view, portfolio_view, parameters)` for
  held securities; `entry_signals(view, parameters)` for unheld scanned
  securities. **No Strategy-ID-specific branches in host code.**
- Action mapping (deterministic; AC #441.5–6):
  - held + exit signal → **Sell** (Sell takes precedence on conflict)
  - held + no valid exit → **Hold**
  - held + absent/unresolvable in scan → **Hold** with
    `scan_evidence_missing` warning (fails safe; never Sell/Buy)
  - unheld + entry signal → **Buy**
  - unheld + no entry signal → **omitted** (not shown as Hold)
- Plain-language `reason` per row derived from the Signal's rule/code — map
  rule codes to wording in one place (a module-level table), not inline.

**Verify**: `uv run pytest tests/test_portfolio_recommendation_service.py -v`
(after Step 5).

### Step 4: Route + template

1. `app/api/dependencies.py`: `get_portfolio_recommendation_service`
   (`@lru_cache`, composed from the plan-023 providers).
2. `app/api/routes/portfolios.py`: `GET
   /portfolios/{portfolio_id}/recommendations` → renders
   `_portfolio_recommendations.html`. Read-only: no
   `require_local_or_token` needed beyond the existing read posture of
   `/partials/*`, but keep it consistent with sibling partial routes.
3. New `app/api/templates/_portfolio_recommendations.html` per mockup
   surface 2: header names the portfolio and assigned Strategy (+ source
   digest badge); provenance strip (run_id, generated_at, market session,
   portfolio revision, parameters); non-blocking freshness warning when
   stale/missing/unknown; stat cards with Sell/Hold/Buy/warning counts;
   groups in fixed order **Sell → Hold → Buy** with counts; per-row action
   badge, plain-language reason, rule code, evidence column; empty states per
   group; `NoAssignment` → info alert with assign link; `EvaluationUnavailable`
   → actionable alert with retry link (AC #441.8–10).
4. Wire the **Recommendations** button in `_portfolio.html` (mockup surface 1)
   to `hx-get` this route for the active account.

**Verify**: `uv run pytest tests/test_recommendation_routes.py -v` (after
Step 5).

### Step 5: Tests

`tests/test_scan_view_adapter.py`:
- OHLCV ordering is oldest-first and bounded to one session
- alias resolution maps a known alias to its canonical ticker; unknown alias
  lands in `unresolved`
- no fabricated history: fields without scan evidence are absent/None

`tests/test_portfolio_recommendation_service.py` (contract tests — the story
requires at least one **history-only** Strategy and one **scan-plus-history**
Strategy to prove the adapter is not tailored to one implementation; use the
`rtly-backtest-*` skills' descriptors with synthetic runtimes, or minimal
in-test `StrategyProtocolV1` implementations if loading real runtimes in
tests is impractical — report which you chose):
- exit signal on held → Sell; held without exit → Hold
- held ticker missing from scan → Hold + `scan_evidence_missing`; never
  Sell/Buy from missing evidence
- entry signal on unheld scanned security → Buy; unheld without entry →
  omitted from the result
- Sell precedence when a held security has both entry and exit signals
- determinism: same inputs → identical result (run twice, compare)
- freshness: stale artifact still evaluates, result carries the stale state
- no-assignment and unavailable-assignment typed states
- result carries portfolio revision, run_id, generated_at, strategy
  id/digest/parameters

`tests/test_recommendation_routes.py`:
- route renders groups in Sell → Hold → Buy order with counts
- freshness warning renders as non-blocking (present alongside results)
- no-assignment state renders the generic experience + assign link
- evaluation failure renders an actionable alert, not a 500
- no trading mutation: assert no POST/trade endpoints are invoked and the
  service exposes no mutation method (grep-level guard + route test)
- template parity: group order/badge classes consistent with
  `_portfolio.html` conventions (model on `tests/test_alert_ui_parity.py`)

### Step 6: Full suite, lint, format, types

**Verify**:
- `uv run pytest` → all pass
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformat; re-stage only
  in-scope files)
- `uv run pyrefly check` → no new errors

## Test plan

See Step 5 — adapter, service (contract-level, two Strategy shapes), and
route/UI tests, all offline. The service tests must not execute real network
or place trades; strategy runtimes used in tests are synthetic or fixture
runtimes under `tests/fixtures/`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `RecommendationResultV1` exists and is the only return type of
      `recommend()` (plus typed no-assignment/unavailable states)
- [ ] `uv run pytest tests/test_scan_view_adapter.py
      tests/test_portfolio_recommendation_service.py
      tests/test_recommendation_routes.py -v` → all pass
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `uv run pyrefly check` → no new errors
- [ ] Grep guard: no `strategy_id ==` / Strategy-ID-specific branching in
      `app/services/portfolio_recommendation_service.py` or
      `app/services/backtest/scan_view.py`
- [ ] Grep guard: no `requests`/`yfinance`/provider imports in the
      recommendation service or adapter
- [ ] `git status` shows only in-scope files modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Plan 023 is not DONE, or `StrategyAssignmentService` lacks the seam this
  plan needs (e.g. no `enrich`/freshness) — finish/extend 023 first.
- The historical `MarketViewV1`/`PortfolioView` contracts cannot be satisfied
  from current-scan data without fabricating provenance — report the specific
  fields; a structural contract extension is a design decision, not an
  improvisation.
- A discovered strategy runtime cannot be loaded/imported safely in-process
  (the discovery import-graph allows it, but report any runtime failure) —
  do not add Strategy-ID-specific workarounds.
- The scan artifact's detector output lacks fields the adapter must expose —
  report the gap rather than inventing values.

## Maintenance notes

- `RecommendationResultV1` is the screen/email parity contract; plan 025 must
  consume it, never recalculate action rules.
- The rule-code → wording table is the single place reason text lives; email
  templates reuse it.
- Cache key includes portfolio trade revision so a same-day trade invalidates
  the recommendation snapshot.
