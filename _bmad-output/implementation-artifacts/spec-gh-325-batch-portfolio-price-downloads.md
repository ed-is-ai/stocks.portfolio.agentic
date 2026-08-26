---
title: 'GitHub #325: Batch Portfolio price downloads and reuse currency metadata'
type: 'feature'
created: '2026-08-26'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
baseline_revision: 'fc64b9c6'
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** Portfolio refresh downloads each ticker separately and can drop a valid cached valuation when a provider response is partial. Currency lookups can also repeat provider calls despite persisted price metadata.

**Approach:** Download canonical provider symbols in bounded batches, extract flat and MultiIndex yfinance responses defensively, and resolve quote currencies from price and durable currency caches before live lookup. Retain cached values for failed symbols and disclose partial refreshes.

## Boundaries & Constraints

**Always:** Preserve original holding tickers in returned maps; use aliases and the safe `.L` fallback; normalise GBP, GBp, USD, and HKD correctly; persist only successful prices/currency metadata through per-ticker upserts; keep provider failures isolated to their symbols.

**Block If:** A required provider currency conversion has no existing supported GBP valuation path.

**Never:** Make a provider request per holding, overwrite the complete cache from one refresh payload, hide a partial refresh, or change portfolio/trade persistence schema.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Batched success | Aliased GBP/GBp/USD/HKD holdings | One bounded primary download produces GBP values and native display metadata | Unsupported/invalid value is a symbol failure |
| Provider shape | Single flat or batch MultiIndex `Close` response | Latest finite positive close is mapped to its original ticker | Missing/NaN ticker column is a symbol failure |
| Partial/batch failure | Missing ticker column or failed chunk | Successful symbols persist; failed symbols retain prior cached value when present | UI warning identifies failed ticker(s) |
| Currency reuse | Price/durable cache contains currency | Cached metadata is used before live lookup; only misses invoke lookup | Newly resolved misses persist |
| Concurrent refresh | Overlapping result subsets | Each successful ticker upsert survives | No wholesale cache replacement |

</intent-contract>

## Code Map

- `app/services/portfolio_service.py` -- price batching, response extraction, currency/value normalisation.
- `app/api/routes/portfolio.py` -- partial-refresh cache merge and warning context.
- `app/api/templates/_portfolio.html` -- visible non-fatal refresh warning.
- `tests/test_portfolio_service.py` -- yfinance shapes, bounded chunks, aliases, currencies, and failures.
- `tests/test_portfolio_template.py` -- partial-failure message rendering.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/portfolio_service.py` -- add bounded batch price retrieval and cache-first currency conversion, retaining the existing two-map caller contract while exposing failed symbols to the refresh route.
- [x] `app/api/routes/portfolio.py` -- merge failed holdings from the pre-refresh cache and report a partial refresh instead of discarding valued positions.
- [x] `app/api/templates/_portfolio.html` -- render partial price-refresh failures as a visible warning.
- [x] `tests/test_portfolio_service.py` -- cover flat/MultiIndex responses, chunks, errors, GBp/USD/HKD, aliases, LSE retry, and cache-first currency reuse.
- [x] `tests/test_portfolio_template.py` -- cover warning markup.
- [x] `_bmad-output/implementation-artifacts/spec-gh-325-batch-portfolio-price-downloads.md` -- record focused test, provider-call, and representative timing evidence.

**Acceptance Criteria:**
- Given a portfolio refresh for many symbols, when it calls yfinance, then it uses one request or bounded chunks plus only a bounded fallback retry batch, rather than one download per ticker.
- Given persisted price or durable currency metadata, when prices are refreshed, then that metadata is used before provider currency lookup and only misses are resolved live.
- Given GBP, GBp, USD, HKD, aliases, LSE fallback, partial frames, or chunk exceptions, when values are resolved, then successful values are correct and failures are isolated.
- Given overlapping refreshes, when each stores its successful subset, then neither discards valid cache entries written by the other.
- Given a partial refresh, when a failed symbol has a cached value, then the UI renders that cached value and clearly identifies the partial failure.

## Design Notes

Batching is limited to 50 symbols so large portfolios bound URL/provider work. A primary batch contains canonical aliases; only unaliased primary misses are retried together with `.L`. The existing SQLite `ON CONFLICT(ticker) DO UPDATE` contract is deliberately retained: only successful subset entries are written, so overlapping requests cannot erase unrelated rows.

## Verification

**Commands:**
- `uv run pytest tests/test_portfolio_service.py tests/test_portfolio_template.py -q` -- focused behaviour passes.
- `uv run ruff check app/services/portfolio_service.py app/api/routes/portfolio.py tests/test_portfolio_service.py tests/test_portfolio_template.py` -- no lint errors.

## Test & Timing Evidence

- Provider-call evidence: the 51-symbol chunk-failure test observes exactly two `yfinance.download` calls (one 50-symbol chunk and one 1-symbol chunk), versus the previous one-download-per-ticker path's 51 calls. The LSE fallback test observes one primary and one fallback request, not an individual retry loop.
- Cache reuse evidence: `tests/test_email_portfolio_parity.py` asserts one batch request for the sole cache miss (`OFFLIST`) and zero download calls on the subsequent fully cached request; the cold path uses bounded quote-unit inference rather than per-ticker provider metadata calls.
- Representative focused timing: the expanded portfolio refresh regression suite completed 225 tests in 9.84s on 2026-08-26. This is a hermetic regression timing, not a live-provider latency claim.
- Quality: focused Ruff check passed for all changed service, route, template-test, repository-test, and parity-test paths.

## Spec Change Log

- 2026-08-26: The review fixes preserve GBp quote units through price conversion,
  use bounded cache-first quote-unit inference for cold batches, keep HKD
  positions in native currency with GBP valuation projections for totals, and
  distinguish cached fallback symbols from unavailable symbols in the refresh UI.

## Review Triage Log

- patched: GBp was being normalized to GBP before pence conversion.
- patched: cold cache refreshes no longer issue one `yf.Ticker` currency request
  per holding.
- patched: HKD native display/P&L and aggregate GBP conversion now agree.
- patched: partial refresh warning separately reports cached fallbacks and
  unavailable symbols; the route-level regression also caught and fixed an
  eager fallback dictionary lookup.
- patched: provider call shape, cache subset persistence, and refresh fallback
  tests now exercise the reviewed contracts.
- patched: email/orchestrator totals now use the same HKD valuation service as
  the Portfolio UI, and the table renders HKD values with an `HK$` symbol.

Final verification: `uv run pytest tests/test_trader_agent.py
tests/test_portfolio_import.py tests/test_portfolio_service.py
tests/test_portfolio_template.py tests/test_email_portfolio_parity.py
tests/test_repositories.py tests/test_web_auth.py -q` -- 225 passed.

Post-review verification: 222 focused parity, refresh, trader, cache, and
template tests passed.
