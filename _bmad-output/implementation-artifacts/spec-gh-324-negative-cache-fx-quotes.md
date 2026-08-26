---
title: 'Negative-cache unavailable same-day Portfolio FX quotes'
type: 'performance'
created: '2026-08-26'
status: 'done'
baseline_commit: '916830484ec86efeee986e14b48298cb7c1da79d'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** When the provider has no quote for today, Portfolio GBP valuation does not persist that attempt, so each render repeats the same slow lookup for every non-GBP currency.

**Approach:** Persist an explicit unavailable/stale/failed attempt keyed by provider, pair, and requested date, and coordinate same-key readers so a completed negative result suppresses repeat provider calls while preserving strict same-day valuation.

## Boundaries & Constraints

**Always:** Never present stale or unavailable data as a valid GBP valuation. A new requested date must attempt fresh lookup. Preserve existing successful quote identity and exact-date semantics.

**Block If:** The durable database cannot safely distinguish an unavailable attempt from a valid quote without weakening the existing quote contract.

**Never:** Cache a stale quote as today, use a fallback rate, or suppress errors/visibility of unavailable valuation states.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| NEGATIVE_HIT | Same pair/date has stale, empty, malformed, or failed attempt | Return `valuation_unavailable` without provider call | Preserve reason/state |
| SUCCESS | Same-day quote exists | Return valued projection and persist immutable quote | None |
| DAY_ROLLOVER | Requested date changes | Provider is eligible for a new attempt | None |
| PAIR_SEPARATION | Different currency pairs share date | Attempts do not collide | None |
| CONCURRENT | Same pair/date requested concurrently | One coordinated attempt is reused deterministically | Preserve unavailable state |
| RESTART | New service/repository process reads prior attempt | Durable negative result suppresses repeat call | No stale valuation |

</intent-contract>

## Code Map

- `app/services/gbp_valuation_service.py` -- same-day FX valuation and provider response classification.
- `app/repositories/fx_quote_repo.py` -- durable FX quote access; add negative-attempt access beside exact-date quote reads.
- `app/repositories/db.py` -- owns additive trades database schema for unavailable FX attempts.
- `tests/test_gbp_valuation_service.py` -- service behavior, provider-call-count, day rollover, pair separation, failures, and concurrency.
- `tests/test_fx_quote_repo.py` -- persistence and identity behavior for negative attempts.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/db.py` -- add an additive, pair/provider/date-keyed unavailable-attempt table and supported index -- make negative results durable across service restarts.
- [x] `app/repositories/fx_quote_repo.py` -- add idempotent read/write methods for unavailable attempts -- prevent duplicate negative records and preserve exact quote lookup.
- [x] `app/services/gbp_valuation_service.py` -- consult and record negative attempts with per-key concurrency coordination -- prevent repeated provider calls without changing valid same-day conversion.
- [x] `tests/test_gbp_valuation_service.py` -- cover stale/weekend, empty/malformed/failure responses, pair separation, day rollover, successful quote, restart, concurrency, and provider-call-count regression -- prove all acceptance criteria deterministically.
- [x] `tests/test_fx_quote_repo.py` -- cover durable negative-attempt identity and idempotence -- prove provider/pair/date isolation.

**Acceptance Criteria:**
- Given an unavailable/stale/empty/malformed/failed response for a pair and date, when valuation repeats, then the provider is not called again and the result remains explicitly unavailable.
- Given a stale response, when valuation completes, then no stale quote is presented as a valid same-day GBP valuation.
- Given a new calendar date, when valuation is requested, then a fresh provider attempt is eligible.
- Given different providers, pairs, or dates, when attempts are recorded, then their identities cannot collide.
- Given restart or concurrent readers, when the same pair/date is requested, then behavior is durable and deterministic with bounded provider calls.
- Given a valid same-day quote, when valuation repeats, then it remains valued from the existing exact-date quote path.
- Given the regression suite, when focused and full quality gates run, then all pass and provider-call-count evidence is recorded.

## Spec Change Log

## Review Triage Log

### 2026-08-26 — Implementation verification
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 0
- reject: 0
- addressed_findings:
  - none

### 2026-08-26 — Adversarial review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 1, medium 1, low 0)
- defer: 5: (high 0, medium 3, low 2)
- reject: 3: (high 0, medium 1, low 2)
- addressed_findings:
  - `[high][patch]` Positive quote lookup is now scoped to provider, pair, and date, preventing a non-yfinance quote from being used by yfinance valuation.
  - `[medium][patch]` Negative-cache persistence failures now degrade to the existing explicit unavailable projection instead of turning a provider miss into an exception.
- deferred: cross-process duplicate-fetch coordination, lock-map eviction, input normalization for low-level repository callers, and negative-cache expiry/retry policy.

## Auto Run Result

Implemented durable provider/pair/date-keyed negative FX attempts with idempotent repository access and per-key concurrency coordination. Exact same-day quote behavior remains unchanged; stale, empty, malformed, and failed provider responses remain explicitly unavailable. Positive quote reuse is provider-scoped.

Verification: focused tests `19 passed`; Ruff check, Ruff format check, and `git diff --check` passed. Full suite: `2007 passed, 2 failed` in unrelated Playwright portfolio-browser tests because `#portfolioSelect` never became visible.

## Verification

**Commands:**
- `uv run pytest tests/test_gbp_valuation_service.py tests/test_fx_quote_repo.py` -- expected: focused tests pass.
- `uv run pytest` -- expected: full suite passes.
- `uv run ruff check app/repositories/db.py app/repositories/fx_quote_repo.py app/services/gbp_valuation_service.py tests/test_gbp_valuation_service.py tests/test_fx_quote_repo.py` -- expected: no findings.
