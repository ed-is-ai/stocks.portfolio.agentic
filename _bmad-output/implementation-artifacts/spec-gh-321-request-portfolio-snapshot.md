---
title: 'Reuse one request-level Portfolio data snapshot'
type: 'performance'
created: '2026-08-26'
status: 'done'
baseline_revision: 'fc64b9c6a4f8f31a668fe763bcbe0cdef80ef480'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** A Portfolio render reloads analysis and trade-related data through separate context paths, duplicating JSON parses, database reads, aliases, and derived work.

**Approach:** Create one short-lived input snapshot per rendered Portfolio context and thread it through positions, chart markers, opening-lot indicators, cash, reconciliation, and analysis enrichment.

## Boundaries & Constraints

**Always:** Keep the snapshot request-scoped, preserve account selection/totals/chart/cash/reconciliation/exit output, and retain refresh/import/trade-response paths.

**Block If:** Existing context builders cannot accept explicit inputs without breaking required response paths.

**Never:** Share a snapshot across requests or cache mutable portfolio state beyond one render.

</intent-contract>

## Code Map

- `app/services/portfolio_service.py` -- Portfolio context composition and repeated loaders.
- `tests/test_portfolio_service.py` -- service-level context equivalence and call-count tests.
- `tests/test_portfolio_import_queue_browser.py` -- request-path regression coverage where relevant.

## Tasks & Acceptance

**Execution:**
- [x] `app/services/portfolio_service.py` -- introduce a request-scoped input snapshot and pass it to context sub-builders.
- [x] `app/services/portfolio_service.py` -- reuse one analysis/trade read per selected portfolio without global caching.
- [x] `tests/test_portfolio_service.py` -- assert bounded loader calls and equivalent rendered context for normal and response paths.

**Acceptance Criteria:**
- Given one Portfolio render, when context is built, then analysis loads at most once and practical selected-portfolio trade data loads once.
- Given an import/refresh/trade response path, when it rebuilds Portfolio context, then output remains equivalent and fresh per request.
- Given concurrent or later requests, when mutable state changes, then no earlier request snapshot is reused.
- Given the focused and full suites, when checks run, then regression call-count evidence is recorded.

## Spec Change Log

- 2026-08-26: Extended the snapshot boundary through TraderService so
  positions, chart markers, and opening-lot indicators replay one selected
  portfolio trade read with the repository's deterministic ordering.
- 2026-08-26: Made refresh and successful import responses pass one explicit
  snapshot to both position calculation and partial rendering; refresh reloads
  chart history only after persisting its new value point.

## Review Triage Log

- patched: unscoped refreshes now retain aggregate chart markers.
- patched: default and response paths no longer hide a second trade-table read
  behind position computation; snapshot replay is tested against database
  replay, including same-day tie ordering.
- patched: supplied cash (including explicit `None`) is retained in the
  snapshot, avoiding a second mutable-ledger read.
- patched: refresh reloads chart history after it records the refreshed value
  snapshot, preserving its immediate chart output.

## Verification

- `uv run pytest tests/test_portfolio_service.py` -- expected: passes.
- `uv run pytest` -- expected: passes.

Completed 2026-08-26:

- `uv run pytest tests/test_portfolio_service.py tests/test_trader_agent.py tests/test_portfolio_import.py -q` -- 162 passed.
- `uv run ruff check app/services/portfolio_service.py app/services/trader_service.py app/agents/trader/trader_agent.py app/api/routes/portfolio.py tests/test_portfolio_service.py tests/test_trader_agent.py tests/test_portfolio_import.py` -- passed.
- `uv run ruff format --check` on the changed Python files -- passed.
- `git diff --check` -- passed.
- `uv run pytest -q` -- 2021 passed.

The focused suite records one analysis load and one selected-portfolio trade
history read per default render, equivalent direct/response context output,
same-day replay equivalence, and a fresh later-request read after mutable
ledger state changes. The full suite passed.
