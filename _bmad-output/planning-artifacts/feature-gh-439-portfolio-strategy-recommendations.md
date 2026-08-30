---
status: confirmed
created: 2026-08-30
method: bmad-mini-plan
github_issue: 439
prerequisite_issue: null
architecture: _bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md
---

# Feature GH-439 — Portfolio-assigned strategies, recommendations, and daily alerts

## Feature contract

### Problem

The scanner currently derives one generic set of recommendations and one
consolidated daily email. Portfolios have independent holdings and cost bases,
but cannot select the Strategy that should govern that account, inspect the
resulting account-scoped actions, or receive an account-specific daily digest.

### Outcome

A portfolio can have zero or one active discovered Backtest Strategy. An
assigned Strategy evaluates the already-published current analysis artifact
against that portfolio's holdings and returns an inspectable, non-executing
Sell/Hold/Buy result. The same result powers a Portfolio Recommendations screen
and one separate daily email for that portfolio. Unassigned portfolios retain
the existing generic experience.

### Success conditions

1. Assign, replace, or clear one Strategy per portfolio without launching a
   scan, backtest, trade, or email.
2. Show Strategy-specific Sell/Hold/Buy recommendations immediately from the
   current published scan artifact.
3. Warn, but do not block, when the artifact is missing, has unknown age, or is
   more than exactly 24 hours old.
4. Send one separate daily recommendation email per assigned portfolio after a
   successful scan, while preserving the existing consolidated daily email.
5. Give UI and email the same typed recommendation result, freshness decision,
   reasons, and provenance.
6. Isolate a broken Strategy, missing evidence, or email failure to one
   portfolio; never trade automatically and never abort unrelated delivery.

## Confirmed scope

- Zero or one active Strategy assignment per portfolio.
- Assignment choices use the existing fail-soft `kind: backtest-strategy`
  discovery authority.
- V1 saves the selected Strategy's shared-validator-normalized default
  parameters. Custom per-portfolio parameter editing is deferred.
- A Portfolio Recommendations action sits beside the account controls. The
  assignment control lives in the existing Manage surface.
- The recommendations screen is portfolio-scoped, names the Strategy, and
  groups actions in risk-first order: Sell, Hold, Buy.
- Exit signals for owned securities map to Sell; owned securities without an
  exit map to Hold; entry signals for unowned scan securities map to Buy.
- A held security with missing/unresolvable evidence maps to Hold with an
  explicit `scan_evidence_missing` warning. Missing evidence can never create a
  Sell or Buy.
- Changing an assignment recalculates the screen immediately. It does not send
  an immediate email; the next successful daily scan uses the current
  assignment.
- Each assigned portfolio receives one email per published analysis `run_id`,
  sent to the existing configured recipient. Subject and content identify the
  portfolio and Strategy.
- Portfolio email structure reuses current design primitives in this order:
  market summary, portfolio summary, actions.
- No assignment means current generic behaviour and no portfolio-specific
  email.
- Recommendation evidence records the analysis run/timestamp, evaluated market
  session, Strategy ID/API/source digest, normalized parameters, portfolio
  revision, rule/reason codes, and warnings.

## Out of scope

- Multiple active Strategies, weighted blends, or voting.
- Automatic order placement, staging, or use of Strategy position sizing.
- Intraday, assignment-triggered, or per-action email.
- Replacing or redesigning the consolidated daily scanner email.
- Per-portfolio recipients.
- Custom Strategy parameter editing in the portfolio assignment UI.
- On-demand provider/network fetches to fill gaps in the current artifact.
- Showing every non-held non-signal security as Hold.

## Decisions and assumptions for review

The user authorized autonomous choices where UX or implementation detail was
uncertain. These are therefore explicit review points, not hidden assumptions:

1. **Default parameters in v1.** Assignment persists a canonical snapshot of
   the Strategy's validated defaults. This makes assignment one deliberate
   click and avoids embedding the full Backtest configuration form in Portfolio.
2. **Current code, recorded provenance.** The stable Strategy ID and normalized
   parameters are the assignment. Each evaluation records the actually loaded
   API/source digest so a code change is visible without silently pretending it
   was the prior source version.
3. **Strict 24-hour warning.** Recommendation freshness uses the analysis
   envelope's timezone-aware `generated_at` and a literal 24-hour threshold,
   not the scanner dashboard's weekend-grace policy.
4. **Safe evidence gap.** A held name absent from the artifact or unresolved by
   `app.core.ticker_identity` is Hold-with-warning. The evaluator performs no
   network fetch and does not guess an exit.
5. **Daily means once per analysis run.** An atomic dispatch receipt keyed by
   `(portfolio_id, analysis_run_id)` prevents retry duplicates while allowing a
   later published run to send the next digest.
6. **Market session versus freshness time.** Strategy evaluation is bounded to
   the newest complete session evidenced by current OHLCV. Freshness remains
   tied to artifact publication time; the two facts are displayed separately.

## Existing architecture and reuse points

| Concern | Existing authority | Planned use |
|---|---|---|
| Strategy discovery and metadata | `app/services/backtest/skill_discovery.py` | Populate assignment choices; record ID/API/source digest; retain fail-soft warnings. |
| Parameter validation | `validate_strategy_parameters` in `strategy_protocol.py` | Normalize defaults at assignment and revalidate at evaluation. |
| Strategy behaviour | Six `skills/rtly-backtest-*/scripts/strategy.py` runtimes implementing `StrategyProtocolV1` | Execute through a bounded current-scan view; no duplicated live rules or Strategy-ID conditionals. |
| Current scan evidence | `ANALYSIS_JSON`, `AnalysisArtifact`, `StockRecord.ohlcv_history` | One no-network input envelope and freshness authority. Normalize daily OHLCV oldest-first and run the registered detector seam needed by scan-aware Strategies. |
| Ticker identity | `app/core/ticker_identity.py` and `config/ticker_aliases.json` when present | Canonical portfolio/scan matching; unresolved identity is surfaced, never guessed. |
| Portfolio state | `PortfoliosRepository`, `TraderService`, `PortfolioService` | Resolve account metadata, holdings, values, and trade revision through services. |
| Existing recommendation | `app/core/recommendation.py` | Preserve generic scanner behaviour; do not replace it with portfolio Strategy results. |
| Existing email design | `AlertAgent`, `summary.html`, `_market_narrative.html`, `_snapshot.html`, action-card partials | Extract/reuse stateless presenters and partials; do not depend on `AlertAgent`'s mutable alert queues. |
| Daily orchestration | `app/orchestration/orchestrator.py` | After analysis publication/current digest work, isolate and attempt each assigned portfolio delivery. |

## Target design

### Persistence

Add a dedicated repository concern in `trades.db`:

- `portfolio_strategy_assignments`: `portfolio_id` primary key, `strategy_id`,
  canonical `parameters_json`, `assigned_at`, and `updated_at`. A nullable
  column on `portfolios` is deliberately avoided so Strategy-specific
  lifecycle and validation do not leak into general portfolio CRUD.
- Recommendation snapshots may be durably cached only when keyed by all inputs:
  portfolio/trade revision, assignment identity/parameters, Strategy source
  digest, and analysis `run_id`. Recalculation is equally acceptable in Story
  2 if it remains deterministic; Story 3 must make screen/email parity
  testable.
- `portfolio_recommendation_dispatches`: an atomic claim/success lifecycle with
  unique `(portfolio_id, analysis_run_id)` identity for retry safety.

All schema evolution remains additive/idempotent in `app/repositories/db.py`.
Portfolio deletion explicitly removes the new rows, matching current manual
portfolio-scoped cleanup.

### Runtime boundary

Introduce frozen provider-neutral recommendation models and a
`PortfolioRecommendationService`. Routes obtain it through
`app/api/dependencies.py`; routes never open repositories or load Skill
runtime modules.

The service builds:

1. a current-scan `MarketViewV1` implementation bounded to one evidenced
   session, with no provider/network access;
2. an immutable `PortfolioView` scoped to one portfolio; and
3. validated parameters plus the host-bound current scan universe.

The Strategy scan-result annotation should be generalized to a small structural
`StrategyScanViewV1` containing only fields Strategies are allowed to read.
Both historical records and a typed current detector result satisfy it. Do not
construct a fake `HistoricalScanRecordV1` with invented reconstruction
provenance.

Execute and validate `exit_signals` and `entry_signals`; do not call or apply
`position_size`. The host owns deterministic action mapping and conflict rules,
not individual presenters.

### Result and presentation contract

One frozen recommendation result contains:

- portfolio ID/name and portfolio trade revision;
- assignment ID/timestamps, Strategy ID/API/source digest and parameters;
- analysis `run_id`, `generated_at`, evaluated session and freshness;
- ordered Sell/Hold/Buy entries with canonical ticker/security identity,
  rule/reason code, plain-language explanation, and evidence warnings; and
- top-level unavailable/degraded state suitable for UI and email.

The Portfolio screen and email presenters consume this object. Neither
presenter invokes Strategy rules or reclassifies actions.

### UX baseline

- Keep the Account selector and Manage control intact.
- Add a visible Recommendations button beside them, with the assigned Strategy
  name or a setup cue.
- The screen header shows portfolio, Strategy, “scan generated” time and
  “market session evaluated” separately.
- A warning banner appears for missing/unknown/>24h data but does not hide
  available actions.
- Sell appears first and uses danger emphasis; Hold is neutral; Buy uses the
  established buy treatment. Each group has a count and useful empty state.
- Reasons use rule metadata/plain language; raw digests live in a compact
  provenance disclosure.

## Ordered implementation stories

### Story 1 — GH-440: Assign one active Strategy to each portfolio

**Value:** A user can deliberately select the methodology for an account while
all unassigned accounts remain unchanged.

**Scope:** Add assignment schema/repository/service/dependency, discoverable
Strategy choices, default-parameter snapshot validation, assign/replace/clear
routes, Manage UI, exact-24-hour non-blocking warning, unavailable-Strategy
repair state, deletion cleanup, and tests.

**Primary likely files:** `app/repositories/db.py`, a new assignment repository,
`app/schemas/`, `app/services/`, `app/api/dependencies.py`,
`app/api/routes/portfolios.py`, `app/api/templates/_portfolio.html`, and focused
repository/route/service tests.

**Not in this story:** evaluation, recommendations screen, or email.

### Story 2 — GH-441: Generate and show portfolio-specific Sell/Hold/Buy recommendations

**Value:** The selected Strategy produces an immediate, explainable account
action list without executing anything.

**Scope:** Add frozen result/action/freshness models, no-network current-scan
market view, structural scan-result boundary, runtime loading/validation,
portfolio action mapping, alias/evidence-gap policy, deterministic input key,
recommendation service/provider/routes, account-control entry point, grouped
screen, plain-language reasons/provenance, and tests. Prove generality with at
least one history-only Strategy and one scan-plus-history Strategy.

**Primary likely files:** `app/services/backtest/strategy_protocol.py`, new
current-scan adapter and recommendation modules, `app/core/ticker_identity.py`
reuse, `app/api/dependencies.py`, Portfolio routes/templates, and contract,
service, route, template, determinism, and mutation-safety tests.

**Dependency:** GH-440.

### Story 3 — GH-442: Send one daily Strategy recommendation email per assigned portfolio

**Value:** Every assigned account receives its own daily, context-rich action
digest without losing the current scanner digest.

**Scope:** Add dispatch receipt schema/repository, stateless recommendation
email presenter/templates, market/portfolio/action composition, per-portfolio
orchestration after a successful published scan, idempotent retry, failure
isolation, stale warning/provenance, notification/log evidence, and parity tests.

**Primary likely files:** `app/repositories/db.py`, a dispatch repository,
recommendation email service/presenter, `app/agents/alert/templates/` reusable
partials/new top-level template, `app/orchestration/orchestrator.py`, dependency
wiring, and multi-portfolio/email/idempotency/regression tests.

**Dependency:** GH-441.

## Acceptance traceability

| Confirmed requirement | Story |
|---|---|
| Exactly one active Strategy | GH-440 |
| Immediate assignment and stale warning | GH-440, GH-441 |
| Portfolio Recommendations screen | GH-441 |
| Sell/Hold/Buy | GH-441 |
| Current scan data; no scan rerun | GH-441 |
| One separate daily email per assigned portfolio | GH-442 |
| Market summary → portfolio summary → actions | GH-442 |
| No assignment preserves generic behaviour | GH-440, GH-441, GH-442 |
| Changing Strategy recalculates but never trades | GH-441 |
| Screen/email parity and provenance | GH-441, GH-442 |

## Quality gates

Each story must run its focused tests plus the repository's configured full
test and quality checks. Before marking the feature complete, verify:

- schema migration from an existing multi-portfolio database;
- assignment and recommendation account isolation;
- exact 24-hour boundary with timezone-aware clocks;
- deterministic results for identical input keys;
- Strategy runtime import/safety boundaries still pass;
- at least two structurally different existing Strategy Skills work;
- no recommendation path calls trade mutation or provider/network APIs;
- current scanner recommendation UI and consolidated email regression tests
  remain unchanged;
- UI/email action counts, ordering, reasons, freshness and provenance match;
- repeated orchestration of one analysis `run_id` sends no duplicate portfolio
  email; and
- one Strategy/SMTP failure does not block another portfolio or the existing
  digest.

## Risks and mitigations

- **Backtest/live semantic drift:** reuse runtime code and detector contracts;
  avoid copying rules. Record source digest on every result.
- **Insufficient current evidence:** fail safe to Hold/omit, surface warnings,
  and perform no hidden fetch.
- **Ticker alias mismatch:** use the canonical identity authority and include
  unresolved values in diagnostics. Never one-hop map or guess.
- **Duplicate emails on retry:** atomically claim a portfolio/analysis-run
  dispatch before SMTP and record outcome explicitly.
- **Mutable `AlertAgent` coupling:** present the immutable recommendation result
  through stateless templates/services.
- **Large Buy universe:** show only entry signals; never render all non-signals.

## GitHub tracking

- Project: [Link strategy manager to portfolio manager](https://github.com/users/ed-is-ai/projects/3) — verified items #439–#442
- Feature: [#439](https://github.com/ed-is-ai/Agents.stocks/issues/439)
- Story 1: [#440](https://github.com/ed-is-ai/Agents.stocks/issues/440)
- Story 2: [#441](https://github.com/ed-is-ai/Agents.stocks/issues/441)
- Story 3: [#442](https://github.com/ed-is-ai/Agents.stocks/issues/442)
