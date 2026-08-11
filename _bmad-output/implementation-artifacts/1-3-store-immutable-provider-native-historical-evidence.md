---
baseline_commit: 52a5c7b2e5798d329d870f2c226a54d1c5edca7e
github_issue: 186
---

# Story 1.3: Store Immutable Provider-Native Historical Evidence

Status: review

## Story

As a portfolio owner,
I want historical market observations stored as immutable source evidence,
so that repeated initialization and Backtests can reuse exactly what was fetched and detect provider revisions.

## Acceptance Criteria

1. **Dedicated historical store:** Given a resolved security and canonical request interval, when historical data is fetched, then `HistoricalPriceRepository` stores it in the separate `HISTORICAL_PRICE_CACHE`, and the overwrite-only current-price cache is never used for historical time series.
2. **Complete provider-native evidence:** Given a successful yfinance response, when its evidence manifest is created, then it retains provider-native OHLCV, Adjusted Close, splits, dividends, requested and observed symbols, quote currency/unit/scale, exchange timezone, inclusive-start/exclusive-end request bounds and full request contract, response-metadata digest, yfinance version, and acquisition timestamp, and heuristic repair or silent alternate-source filling is never applied.
3. **Canonical revision identity:** Given normalized evidence content, when its revision digest is calculated, then canonical column/session ordering, IEEE-754 finite-number encoding, JSON nulls, exchange-local session dates, normalized timezone/symbol/currency metadata, request contract/version, and normalized action rows contribute exactly as specified by AD-6, while acquisition time is retained as metadata but excluded from content identity.
4. **Idempotent immutable revisions:** Given the same security and canonical request interval is fetched repeatedly, when normalized content is unchanged, then the existing content revision is reused without duplicate observations, while each successful acquisition remains auditable; changed content creates a new immutable revision rather than overwriting evidence.
5. **Explicit interval units:** Given overlapping request intervals, when evidence is persisted, then each security/request interval remains an explicit revision unit, and overlapping content is neither merged implicitly nor selected without an exact manifest/revision reference.
6. **Reference integrity:** Given a committed snapshot month or Backtest references an evidence revision, when cache maintenance or replay occurs, then referenced observations/manifests cannot be changed or deleted, and absent or incomplete referenced evidence is reported as `integrity_error`/`evidence_missing` rather than refetched or silently replaced.

## Tasks / Subtasks

- [x] Gate 1 — Extract the reusable provider-native acquisition contract (AC: 2–3)
  - [x] Introduce typed `HistoricalEvidenceRequest`, `HistoricalEvidencePayload`, row/action, quote-unit, and revision-manifest contracts under `app/services/backtest/`; carry resolved `security_id` and alias evidence into the request without resolving identity from ticker text.
  - [x] Refactor Story 1.1's `YFinanceQualificationAdapter` to delegate the actual request, retry, metadata, normalization, and digest work to one reusable `YFinanceHistoricalEvidenceAdapter`. Preserve Story 1.1's public contracts, fixture digests, failure codes, exact retry timing, and qualification tests.
  - [x] Keep the closed yfinance request contract: `interval="1d"`, explicit inclusive `start`/exclusive `end`, `prepost=False`, `auto_adjust=False`, `back_adjust=False`, `actions=True`, `repair=False`, `keepna=True`, `rounding=False`, 15-second attempt timeout, and error surfacing. No `yf.download`, auto-adjusted frame, heuristic repair, Stooq, MCP, paid source, or fallback.
  - [x] Require a timezone-aware `DatetimeIndex`, unique sessions, exact expected exchange-session coverage, required columns, finite required OHLCV values, non-negative volume/dividend/split values, and resolved observed-symbol/currency/unit/timezone agreement. Successful empty/partial/malformed responses fail immediately under AD-21.
  - [x] Canonicalize rows by exchange-local `YYYY-MM-DD` session and fixed field order. Encode finite provider numbers from `float(value).hex()` and missing nullable values as JSON null; reject infinities/NaNs where the contract requires data. Normalize split/dividend events into stable effective-session rows.
- [x] Gate 2 — Add the dedicated immutable historical-price repository (AC: 1, 4–5)
  - [x] Add `HISTORICAL_PRICE_CACHE` to `app/core/config.py`; create `app/repositories/historical_price_repo.py` as the sole schema/API owner using `db.Connect`/`session`, SQLite foreign keys on every connection, and idempotent `ensure_schema()`.
  - [x] Persist immutable revision manifests, interval bounds/request contract, provider metadata, observations, corporate-action rows, and append-only acquisition records. Keep data revision identity distinct from acquisition metadata so an unchanged refetch reuses one observation/action set but records the successful acquisition time.
  - [x] Key one revision unit by resolved `security_id`, provider, canonical inclusive-start/exclusive-end interval, request-contract version, and content-derived `data_revision`. Do not merge rows from overlapping intervals or expose a “latest matching overlap” lookup.
  - [x] Use compare-and-insert in one `BEGIN IMMEDIATE` transaction. Identical content is an idempotent reuse; a digest collision, changed payload under an existing revision, unbalanced manifest/row counts, or partial child-row write is `integrity_error` and rolls back.
  - [x] Enforce append-only evidence in SQLite with FK/CHECK/UNIQUE constraints and update/delete triggers for revisions, observations, actions, and acquisition lineage—not frozen dataclasses alone.
- [x] Gate 3 — Close exact-reference and integrity APIs (AC: 5–6)
  - [x] Provide repository reads only by exact revision/manifest reference and an explicit exact interval-revision lookup; return typed evidence ordered by session/action key. Never choose a revision merely because its interval overlaps.
  - [x] Add a reference/pin API suitable for snapshot and Backtest consumers, recording consumer type/id plus exact revision. Pinning verifies the complete referenced manifest, observations, actions, and digests before commit; missing evidence returns stable `evidence_missing`/`integrity_error`.
  - [x] Add integrity verification that recomputes canonical counts/digests from persisted rows and fails closed on missing, extra, altered, or FK-orphaned evidence. Replay is cache-only and must not call yfinance when an exact reference is missing.
  - [x] If maintenance deletion is introduced, it may target only demonstrably unreferenced whole revisions and must remain all-or-nothing. It is acceptable—and safer for v1—to expose no deletion API at all.
- [x] Gate 4 — Verification and regression protection (AC: 1–6)
  - [x] Add focused adapter tests for exact request kwargs, inclusive/exclusive bounds, timezone/session normalization, fixed canonical ordering, response metadata, quote units USD/GBP/GBp, actions, missing/null/non-finite/duplicate/naive/partial frames, identity mismatch, and retry taxonomy.
  - [x] Add repository tests for schema restart, foreign keys, immutability triggers, atomic rollback, unchanged refetch reuse plus acquisition audit, changed-content revision, overlapping intervals, exact lookup, pinning, missing evidence, digest/count verification, and concurrent compare-and-insert.
  - [x] Run Story 1.1 qualification and Story 1.2 identity/roster tests to prove the refactor preserves their contracts, then run the full repository suite.
  - [x] Run Ruff lint/format and Pyrefly for touched code; distinguish unrelated pre-existing findings from Story 1.3 regressions.

## Dev Notes

### Developer Context

Story 1.3 creates the immutable source-evidence substrate consumed by Stories 1.4–1.7. It stores what yfinance actually returned under a closed request contract; it does not yet derive `as_traded` or `split_continuous_as_of_D` planes, execute corporate actions, reconstruct scan records, commit monthly coverage, or run initialization jobs.

The distinction between content identity and acquisition metadata is load-bearing. The same provider content fetched tomorrow must resolve to the same `data_revision` and must not duplicate observations. The later acquisition should still be auditable without making acquisition time part of the content digest. A changed historical response for the same security/interval must create a second immutable revision, and downstream consumers must pin one explicitly.

### Technical Requirements

- Use Python 3.12+, pandas 3.0.3, yfinance 1.4.1, Pydantic 2.13.4 where useful, and stdlib `sqlite3`. Use the versions already locked in `uv.lock`; add no dependency or ORM.
- Reuse `app/services/backtest/canonical_manifest.py` for sorted UTF-8 compact JSON and SHA-256. Preserve its existing output for Story 1.1/1.2. Add a versioned evidence-specific canonical shape rather than silently changing the shared serializer.
- Reuse `FailureCode`, `ProviderFailure`, `_classify_exception` behavior, and deterministic retry/jitter from `historical_data_qualification.py`; extract public shared names if needed. Do not create a second provider outcome taxonomy.
- Resolve expected sessions before acquisition via `TradingCalendar`; the adapter validates exact returned sessions. Session identity is the exchange-local date after converting from the provider's timezone-aware index.
- Store provider-native values exactly in the canonical representation used for identity (hex strings/null), not rounded display numbers. Derived Decimal planes belong to Story 1.4.
- Quote evidence is three-part: economic currency, provider quote unit, and scale. Closed equity units are USD/1, GBP/1, and GBp/0.01 GBP. Unknown or conflicting units fail.
- Persist timestamps as UTC ISO-8601 instants. Request `start` is inclusive and `end` exclusive. Validate `start < end` and reject non-canonical intervals before provider access.
- Keep external calls injected. Unit/CI tests use provider-shaped local DataFrames/metadata and controlled clocks; no live network is required.

### Architecture Compliance and Scope Boundaries

- Follow Services → Repositories. This story has no FastAPI route, worker, template, Strategy Skill, notification, or live-portfolio deliverable.
- `HistoricalPriceRepository` owns a separate `HISTORICAL_PRICE_CACHE`; do not add historical series to `price_cache` in `trades.db` or to `BACKTEST_DB`.
- The repository owns persistence identity and integrity predicates. Services must not assemble ad-hoc SQL, infer “latest” revisions, or merge overlapping fetches.
- `security_id` and permitted observed aliases are inputs from Story 1.2's immutable identity/alias evidence. Do not reuse portfolio aliases, strip suffixes, fuzzy-match names, or allocate identities here.
- Story 1.4 owns price/volume/action/FX interpretation and Decimal conversion. Story 1.5 owns detector reconstruction. Story 1.6 owns snapshot references/readiness. Story 1.7 owns background fetching. Implement only the exact-reference seam those stories need.
- AD-10 isolation remains binding: no imports/access to `TraderAgent`, live SIPP/ISA trades, cash, positions, or order submission.
- Preserve the unrelated in-progress issue #210 architecture edits currently present in the working tree; this story does not modify or adopt AD-23–AD-27.

### Existing Code to Preserve

- `historical_data_qualification.py` already implements the exact yfinance kwargs, strict provider-frame validation, finite-number hex encoding, metadata digest, stable content digest, failure classification, and retry policy. Extract/reuse these mechanics; do not copy them into a divergent adapter.
- `canonical_manifest.py` is shared by qualification and reconstruction roster manifests. Existing Story 1.1/1.2 digest tests are regression authority.
- `BacktestRepository` owns qualification and roster evidence in `BACKTEST_DB`. Do not move or couple those tables to the historical-price DB.
- `db.connect()` enables foreign keys per connection. New repository tests must prove this remains true for `HISTORICAL_PRICE_CACHE` connections.
- `PriceCacheRepository` is current-price presentation state with overwrite semantics and is intentionally unsuitable for this story.

### File Structure Requirements

Expected touch points:

- `app/core/config.py` — add the dedicated historical-price DB path.
- `app/services/backtest/historical_price_evidence.py` — typed request/payload/manifest and reusable yfinance adapter/canonicalizer.
- `app/services/backtest/historical_data_qualification.py` — delegate to the reusable adapter without qualification contract drift.
- `app/repositories/historical_price_repo.py` — immutable schema, compare-and-insert, exact reads, pins, integrity checks.
- `tests/backtest/test_historical_price_evidence.py` — acquisition/normalization/digest tests.
- `tests/backtest/test_historical_price_repository.py` — persistence/reference/integrity/concurrency tests.
- Existing Story 1.1/1.2 tests — mandatory regression suite.

Do not commit generated SQLite files, yfinance cache/cookie databases, live payload dumps, credentials, or local logs.

### Testing Requirements

- Canonical identity tests must prove acquisition time and input map order do not change `data_revision`; request contract, security, interval, observed symbol, currency/unit/timezone, any canonical row, and any action do.
- Persistence tests must inspect SQLite directly to prove update/delete rejection and FK enforcement, not only exercise repository methods.
- Race tests must use separate connections against one temporary DB and accept only identical-winner reuse; materially different content cannot be mistaken for the winner.
- Integrity tests must cover missing manifest, missing/extra observation/action, count mismatch, altered canonical payload, orphan reference, and an exact reference to the wrong interval/security.
- Regression commands should include focused historical evidence + qualification + roster suites, then `uv run pytest`, touched-scope `uv run ruff check`, `uv run ruff format --check`, and `uv run pyrefly check`.

### Previous Story Intelligence

- Story 1.2 established opaque `security_id`, effective-dated aliases, strict source identity, one shared canonical manifest utility, append-only SQLite triggers, `BEGIN IMMEDIATE` compare-and-insert, and per-connection FK enforcement. Story 1.3 should follow those patterns.
- Story 1.2 review caught three reusable failure modes: schema-only FK pragmas, conflict recovery comparing too little evidence, and permissive “strict” adapters that reused fallback-normalized data. This story must test each connection, compare the complete canonical content on races, and keep successful provider payload validation fail-closed.
- The full suite after Story 1.2 was 756 passing tests with one existing warning; Story 1.3 must not reduce that baseline.

### Git Intelligence Summary

- Current branch `feat/backtest-roster-foundations` contains Story 1.1/1.2 foundations at `52a5c7b` and the requested earlier SIPP import commit. Preserve both; do not reset or rewrite history.
- Recent Backtest work uses frozen typed records, injected providers/clocks, canonical JSON digests, raw SQLite repositories, immutable triggers, and deterministic provider-shaped tests.
- `AGENTS.md` is an untracked user/project instruction file. Preserve it and do not include it in story commits unless explicitly requested.

### Latest Technical Information

- yfinance's `Ticker.history` contract exposes the parameters already bound by AD-6, including `start`, `end`, `interval`, `prepost`, `actions`, `auto_adjust`, `back_adjust`, `repair`, `keepna`, `rounding`, `timeout`, and `raise_errors`. Keep explicit values because library defaults are not evidence contracts. [Source: https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.history.html]
- The repository lock, not an unconstrained newest release, is authoritative: yfinance 1.4.1 and pandas 3.0.3 are the tested runtime versions for this story. [Source: `uv.lock`]
- Yahoo/yfinance remains an unofficial free source without an SLA. Immutable revisions, bounded retries, and visible failures remain required even after Story 1.1 qualification. [Source: https://github.com/ranaroussi/yfinance]

### Project Structure Notes

- The Architecture Spine seeds `HistoricalPriceRepository` under `app/repositories/` and historical evidence services under `app/services/backtest/`; use those locations so later stories build on one substrate.
- The separate historical-price DB follows the repository's one-database-per-concern convention. Its path belongs only in `app/core/config.py`.
- Normal users do not browse raw revisions in this story. Later UI surfaces concise activity failures and provenance labels, not internal evidence tables.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Epic 1, Story 1.3]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-5, AD-6, AD-10, AD-14, AD-18, AD-20–AD-22, Structural Seed]
- [Source: `_bmad-output/planning-artifacts/prds/prd-Agents.stocks-2026-08-09/prd.md` — Historical Price Data glossary, FR-4–FR-7]
- [Source: `_bmad-output/planning-artifacts/sprint-change-proposal-2026-08-10.md` — FR-6 market-data contract and Story 1.3 sequencing]
- [Source: `_bmad-output/implementation-artifacts/1-2-capture-stable-security-identities-and-the-reconstruction-roster.md` — previous-story implementation/review intelligence]
- [Source: `app/services/backtest/historical_data_qualification.py` — qualified provider request, normalization, digests, retries, failures]
- [Source: `app/services/backtest/canonical_manifest.py` — shared canonical JSON/SHA-256]
- [Source: `app/repositories/backtest_repo.py` — immutable SQLite and compare-and-insert patterns]

## Dev Agent Record

### Agent Model Used

OpenAI Codex (GPT-5)

### Debug Log References

- 2026-08-11: RED — focused tests failed at collection because the historical evidence service and repository did not exist.
- 2026-08-11: GREEN — Story 1.1–1.3 focused tests passed after implementing the reusable adapter, immutable repository, concurrency, and rollback contracts.
- 2026-08-11: Full regression exposed Story 1.2's rolling exchange-calendar window; bounded calendar construction to 1970–2100 and added a regression fixture.
- 2026-08-11: Final verification passed 771 tests with one existing warning; touched-scope Ruff, Ruff format, and Pyrefly passed.

### Implementation Plan

- Extract the qualified yfinance request and normalization contract into a reusable identity-bound evidence adapter.
- Persist canonical revisions, observations, actions, acquisitions, and exact consumer pins in an append-only dedicated SQLite repository.
- Verify exact-reference integrity from persisted rows and preserve qualification and roster behavior through focused and full regression suites.

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Implemented identity-bound provider-native yfinance requests with exact session, metadata, currency/unit, action, retry, and canonical digest contracts.
- Implemented separate immutable historical-price persistence with atomic compare-and-insert, acquisition auditing, exact reads, consumer pins, SQL-enforced immutability, and stable missing/integrity errors.
- Preserved Story 1.1 qualification digests while delegating its provider acquisition to the reusable adapter.
- Corrected the Epic 1 calendar authority from a rolling today-relative schedule to the architecture-required fixed 1970–2100 table.
- Verification: 771 full-suite tests pass (one existing warning); focused Ruff lint/format and Pyrefly report no errors.

### File List

- `app/core/config.py`
- `app/repositories/historical_price_repo.py`
- `app/services/backtest/historical_data_qualification.py`
- `app/services/backtest/historical_price_evidence.py`
- `app/services/backtest/trading_calendar.py`
- `tests/backtest/test_historical_price_evidence.py`
- `tests/backtest/test_historical_price_repository.py`
- `tests/backtest/test_trading_calendar.py`
- `_bmad-output/implementation-artifacts/1-3-store-immutable-provider-native-historical-evidence.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml`

### Change Log

- 2026-08-11: Created comprehensive Story 1.3 implementation context; marked ready-for-dev.
- 2026-08-11: Fixed the rolling calendar authority prerequisite, passed 771 tests and all touched-scope quality checks, and moved Story 1.3 to review.
