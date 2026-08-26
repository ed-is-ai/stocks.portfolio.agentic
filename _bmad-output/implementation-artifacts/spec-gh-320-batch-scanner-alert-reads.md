---
title: 'GitHub #320: Batch Stock Scanner alert-state reads'
type: 'refactor'
created: '2026-08-26'
status: 'done'
baseline_revision: 'fc64b9c6a4f8f31a668fe763bcbe0cdef80ef480'
final_revision: 'fc64b9c6a4f8f31a668fe763bcbe0cdef80ef480'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** Building Stock Scanner context currently makes `has_watching()` and
`last_alerted_at()` calls for every displayed record. Each call opens its own
SQLite session, so alert-state reads grow as two queries/sessions per tile.

**Approach:** Add a single request-level `AlertsRepository` read that supplies
both facts for all requested tickers, then use that immutable lookup while
building every `AlertUiState`.

## Boundaries & Constraints

**Always:** Preserve the existing alert-policy semantics: no alert history is
not suppressed; historical alerts still provide the newest timestamp for
cooldown calculation; active `watching` alerts suppress. Keep `AlertUiState`
database-free, retain the existing single-ticker APIs for AlertAgent callers,
and return a deterministic lookup for duplicate ticker inputs.

**Block If:** The repository schema or the established UI alert-policy contract
cannot represent both the latest timestamp and active-watching state in one
batch result without changing their meaning.

**Never:** Do not alter alert thresholds, cooldown duration/calculation,
template behavior, alert-agent write behavior, or make a database call from
inside the scanner record loop.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| No history | Requested ticker has no `alerts` row | Lookup has no entry; UI gets `False` / `None`, preserving eligibility | No error expected |
| Historical only | Requested ticker has terminal alert rows | Lookup reports newest `alerted_at` and `has_watching=False`; existing cooldown rules apply | No error expected |
| Active watch | Requested ticker has a `watching` row and history | Lookup reports `has_watching=True` plus newest timestamp | No error expected |
| Duplicates | Request contains the same ticker more than once | The batch request deduplicates by ticker and produces the same state for every record with that ticker | No error expected |
| Empty scanner | Request has no ticker | No alert database read is required; lookup is empty | No error expected |

</intent-contract>

## Code Map

- `app/repositories/alerts_repo.py` -- owns SQLite alert reads and will expose the batch state API.
- `app/api/stock_scanner_context.py` -- constructs scanner `AlertUiState` values and must consume one request-level lookup.
- `tests/test_repositories.py` -- repository behavior and combined state coverage.
- `tests/test_stock_scanner_ui.py` -- multi-record scanner context API-count regression coverage.

## Tasks & Acceptance

**Execution:**

- [x] `app/repositories/alerts_repo.py` -- added a typed batch alert-state read that deduplicates ticker inputs and returns each requested ticker's active-watching flag and latest alert timestamp using one SQLite query for non-empty input -- eliminated per-ticker scanner reads while preserving the repository as the database boundary.
- [x] `app/api/stock_scanner_context.py` -- obtains batch state once before the records loop and builds each `AlertUiState` from the lookup -- the loop makes no alert repository calls.
- [x] `tests/test_repositories.py` -- covers no-history, historical-only, active-watching, duplicate, and empty-input batch results, plus one-`SELECT` query count -- locks down legacy alert semantics and deterministic lookup behavior.
- [x] `tests/test_stock_scanner_ui.py` -- builds a multi-ticker (including duplicate) context with a mocked repository and asserts exactly one batch API call and zero legacy per-ticker calls -- prevents an N+1 regression.
- [x] `_bmad-output/implementation-artifacts/spec-gh-320-batch-scanner-alert-reads.md` -- records the verified representative query/API count and completed validation commands -- retains performance evidence with the implementation.

**Acceptance Criteria:**

- Given a scanner context with one or many distinct records, when it is built, then alert database reads are bounded at one non-empty batch query/API call rather than two calls per record.
- Given scanner records, when alert UI state is built in the context loop, then neither `has_watching()` nor `last_alerted_at()` is invoked from that loop.
- Given a ticker with no rows, terminal history, or an active watching row, when its batch state is used to build the UI state, then the existing no-alert, cooldown, and watching-suppression behavior is unchanged.
- Given duplicate ticker records, when the batch state is requested, then the repository queries each distinct ticker once and every duplicate record receives the same deterministic state.
- Given a multi-ticker scanner context test, when it completes, then it proves one batch API call and no legacy per-ticker API calls.

## Design Notes

The batch result should be a ticker-keyed value object or mapping carrying the
same two primitives accepted by `build_alert_ui_state`; policy remains in
`app.core.alerting`. A grouped SQL read can derive the newest timestamp and
whether any row is actively watching together, avoiding separate scans.

Verified representative query count: a multi-ticker request with a duplicate
performs one SQLite `SELECT`; a scanner context with three displayed records
(two `AAPL.L` rows and one `TSLA` row) performs one batch API call and zero
legacy per-ticker API calls. Previously, those three tiles made six alert
repository calls/sessions (2 × 3).

## Review Triage Log

### 2026-08-26 — Review pass

- intent_gap: 0
- bad_spec: 0
- patch: 3 (low 1, medium 2)
- defer: 0
- reject: 0
- addressed_findings:
  - `[low]` `[patch]` Added an instrumented repository regression assertion for one SQLite `SELECT`, so the recorded performance result is directly verified.
  - `[medium]` `[patch]` Replaced variable-per-ticker SQL with one JSON-array bind via SQLite `json_each`, retaining one query for arbitrarily large scanner artifacts.
  - `[medium]` `[patch]` Added exact newest-timestamp and serialized-input deduplication assertions.

## Verification

**Commands:**

- `uv run pytest tests/test_repositories.py tests/test_stock_scanner_ui.py -q` -- expected: alert batch semantics and scanner regression tests pass.
- `uv run ruff check app/repositories/alerts_repo.py app/api/stock_scanner_context.py tests/test_repositories.py tests/test_stock_scanner_ui.py` -- expected: no lint violations.
- `uv run ruff format --check app/repositories/alerts_repo.py app/api/stock_scanner_context.py tests/test_repositories.py tests/test_stock_scanner_ui.py` -- expected: files are formatted.
- `git diff --check` -- expected: no whitespace errors.

**Result:** 2026-08-26 focused tests passed (`81 passed, 1 warning`); Ruff
lint and format checks, and `git diff --check`, passed.

## Auto Run Result

Implemented a request-level alert-state lookup for Stock Scanner context. It
deduplicates input tickers, combines active-watching and latest-timestamp facts
in one grouped SQLite read, and leaves cooldown policy in `app.core.alerting`.

Changed files:

- `app/repositories/alerts_repo.py` -- added `AlertReadState` and batched state lookup.
- `app/api/stock_scanner_context.py` -- reads alert state once before the record loop.
- `tests/test_repositories.py` -- verifies state semantics and one-`SELECT` batch behavior.
- `tests/test_stock_scanner_ui.py` -- verifies one batch API call and no legacy per-ticker calls.
- `spec-gh-320-batch-scanner-alert-reads.md` -- intent contract, task completion, and query-count evidence.

Review findings: three patches applied; nothing deferred or rejected. No
follow-up review is recommended. The JSON-array transport keeps the batch query
within SQLite's bind-parameter limit for large scanner artifacts.

Verification: `uv run pytest tests/test_repositories.py tests/test_stock_scanner_ui.py -q`
(`81 passed, 1 warning`); targeted Ruff lint and formatting checks; `git diff
--check`. Not committed, per request.
