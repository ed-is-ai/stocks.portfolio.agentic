---
baseline_commit: d8f644c2a26fab8459ca332f75f08b3a2afa35ad
github_issue: 189
---

# Story 1.6: Commit Versioned Monthly Snapshot Coverage

Status: done

## Story

As a portfolio owner,
I want monthly reconstruction to become Ready only through a complete transactional proof,
so that coverage summaries and Backtest selectors never hide a failed member, missing month, or incompatible data version.

## Acceptance Criteria

1. A requested snapshot month is parsed strictly as `YYYY-MM`. Every expected roster member is assigned the last completed session of that calendar month for its closed MIC mapping (`XNAS -> XNYS`, `XNYS -> XNYS`, `XLON -> XLON`), including early closes. Malformed months, unsupported MICs, the current calendar month, and future months fail with `calendar_error`; no snapshot state is written.
2. A member may resolve as `before_first_provider_observation` only when immutable evidence proves that a successful canonical full-history request for the same security, effective alias, provider contract, currency/unit, and calendar has at least one valid later observation and the target session is earlier than the first observation. The persisted proof describes provider-observed lifetime only and never claims an IPO/listing date.
3. Missing evidence on or after the first observation, ambiguous identity or alias, a bounded request presented as full-history, partial/malformed evidence, provider/system failure, `not_tradeable`, or an unknown state remains unresolved and fails the month immediately. No source-gap inventory, guessed exclusion, or partial Ready month is persisted.
4. A month becomes Ready only when all expected members are represented by exactly one immutable `snapshot_members` row, every valid member has exactly one canonical `monthly_scan_results` row, every excluded member has none, all exact input/evidence revisions still verify, and expected/resolved/valid/excluded counts and roster/input/result/content digests balance. The complete write set commits in one `BACKTEST_DB` transaction and is invisible before commit.
5. `SnapshotProfileV1` has one canonical UTF-8 JSON representation and content-derived `profile_hash`. Its identity binds the historical-record schema, ordered detector source manifests, reconstruction-roster/source policies, calendar policy and 1970–2100 session-table digest, yfinance ingestion contract/source version, reconstructability policy, provenance vocabulary, and monthly cadence. Exact member aliases, sessions, evidence revisions, and record bytes remain month evidence; reconstructed and later eligible observed-BAU months may coexist in a compatible profile without claiming the same provenance.
6. Snapshot profiles and committed months are immutable compare-and-insert evidence. Repeating an identical profile/month commit converges to the existing row; different content for the same profile/month fails with `integrity_error` and never overwrites or partially mutates evidence.
7. One singleton `active_snapshot_profile` pointer selects current coverage and increments a monotonic activation sequence on each actual profile change. Activation is atomic, cannot point to an absent/malformed profile, and never merges old-profile months into current coverage. A no-change activation is idempotent and does not fabricate another sequence value.
8. Coverage discovery uses only committed Ready months for one profile and returns the active profile's human-readable version, earliest month, latest month, total count, provenance counts/ranges, and maximal contiguous calendar-month intervals. It never infers continuity from min/max/count, bridges a missing month, or merges profiles.
9. Readiness for a normalized inclusive interval is true only when every ordered calendar month exists under the pinned profile with `processing_complete=true` and `market_complete='unknown'`. It returns the ordered-month evidence digest used by later launch/revalidation stories. An entirely Ready duplicate initialization request is a no-op; a partial overlap identifies only missing months while preserving the full requested-interval readiness result.
10. Concurrent identical commits converge on one immutable month and one profile; concurrent conflicting commits produce one winner and one deterministic `integrity_error`. Any validation, insert, trigger, or digest failure rolls back the whole monthly transaction.

## Tasks / Subtasks

- [x] Define the canonical snapshot/profile domain contract (AC: 2, 4-9)
  - [x] Add `app/services/backtest/snapshot_profile.py` with strict, frozen Pydantic v2 models (`extra='forbid'`) for `SnapshotProfileV1`, legitimate-exclusion proof, member resolution, complete monthly commit input, coverage interval/summary, and interval-readiness output.
  - [x] Reuse `app/services/backtest/canonical_manifest.py` and the canonical model conventions established by Story 1.5. Do not add an alternate JSON/hash implementation.
  - [x] Close all vocabularies: provenance is `best_effort_reconstructed | observed_bau`; resolution is `valid_scan | legitimate_exclusion`; the sole exclusion code is `before_first_provider_observation`; Ready is exactly `processing_complete=true` plus `market_complete='unknown'`.
  - [x] Build `profile_hash`, per-month expected/input/result/content digests, and ordered-range digest from version-tagged canonical manifests. Acquisition timestamps remain audit metadata and do not make identical evidence a new content identity.
  - [x] Keep nested mappings and sequences deeply immutable so validated commit input cannot change between digest verification and persistence.

- [x] Strengthen the canonical calendar boundary (AC: 1, 8, 9)
  - [x] Extend `app/services/backtest/trading_calendar.py` with a strict month parser/normalizer and a public API that resolves the canonical session for every expected MIC.
  - [x] Reject malformed, current, and future month labels before roster processing. Inject/pass an aware clock or `as_of` date in tests; do not make date-boundary tests depend on wall-clock timing.
  - [x] Preserve Story 1.1's fixed XNYS/XLON 1970–2100 authority, closed MIC mapping, early/unscheduled closure behavior, and canonical digest. Do not call private `_calendar` from the new profile/commit service.
  - [x] Define calendar-month adjacency independently of exchange sessions (`2024-01` is adjacent to `2024-02`) and use it for contiguous coverage and inclusive range enumeration.

- [x] Validate the sole legitimate member exclusion (AC: 2, 3)
  - [x] Require verified immutable `StoredHistoricalEvidence`, the exact roster member, alias revision/effective-date result, target MIC/session, canonical calendar digest, request contract/version, currency/unit, evidence revision, first observed session, and acquisition timestamp.
  - [x] Treat only a successful full supported-history request as exclusion evidence. A normal warm-up/bounded reconstruction request is never sufficient. The contract must identify the full-history scope explicitly; for v1 it spans the application-supported calendar horizon from `1970-01-01` through an exclusive bound after the target/first observation.
  - [x] Require at least one canonical valid observation later than the target session and prove the target is before the first observation. Persist the closed proof as canonical JSON and digest it into the member/month manifests.
  - [x] Return stable architecture failure codes (`required_data_missing`, `identity_ambiguous`, `calendar_error`, `integrity_error`, or the existing provider adapter code). Never translate a missing/later observation to present-day `not_tradeable` and never collect a user-facing gap list.

- [x] Add immutable profile and monthly snapshot persistence (AC: 4-7, 10)
  - [x] Extend `app/repositories/backtest_repo.py` with `snapshot_profiles`, singleton `active_snapshot_profile`, `snapshot_months`, `snapshot_members`, and `monthly_scan_results`, including the composite PK/FK and CHECK constraints from AD-9/AD-14.
  - [x] Store the canonical profile/month/member manifests or exact canonical JSON/digests needed to re-verify every row. Add immutable UPDATE/DELETE triggers for profiles, months, members, and scan results; only the active pointer is mutable through a repository-owned CAS/activation method.
  - [x] Validate the whole month in memory before opening the write transaction, then recheck referenced roster/profile and immutable digest facts inside `BEGIN IMMEDIATE`. Insert profile if absent, compare-and-insert all month evidence, read/verify the winner, and commit once.
  - [x] Enforce `expected_count = valid_count + excluded_count`, member count equals expected count, result count equals valid count, security IDs are unique and exactly match the expected roster, record/profile/month/session identities agree, and every canonical record digest/input revision matches the member and month manifests.
  - [x] Keep failed/incomplete work outside committed snapshot tables. Story 1.7 owns attempt/job state and progress; this story must not introduce a second lifecycle ledger or expose partial staging as coverage.
  - [x] Make identical repeat/concurrent commits idempotent. Never use `INSERT OR REPLACE`; a key/content mismatch is `BacktestIntegrityError(code='integrity_error')` and rolls back every insert from that attempt.

- [x] Implement active-profile and coverage/readiness queries (AC: 7-9)
  - [x] Add repository methods to compare-and-insert/get profiles, atomically activate a profile, query active coverage, query explicit pinned-profile coverage, and evaluate an inclusive interval.
  - [x] Return maximal contiguous intervals in ascending order while earliest/latest/count summarize the same exact month set. Return no fabricated interval for empty coverage.
  - [x] Return provenance counts and ranges without blending `best_effort_reconstructed` and `observed_bau`; reconstructed output retains the canonical survivorship-warning facts for later UI projection.
  - [x] Compute the ordered-month digest over each month's profile, month label, expected/roster, input-revision, provenance-quality, and content digests. Do not hash only earliest/latest/count.
  - [x] Distinguish `ready`, `missing_months`, and `no_op`; duplicate-range no-op must create no job or snapshot mutation. Job creation itself remains Story 1.7 scope.

- [x] Prove atomicity, determinism, and isolation (AC: 1-10)
  - [x] Add focused tests for strict month parsing, ordinary/holiday/early-close/unscheduled-closure month ends, unsupported MIC, current/future rejection, and deterministic injected-clock behavior.
  - [x] Add exclusion-boundary tests: before first observation succeeds only with complete proof; equality/after-first fails; empty, bounded, partial, malformed, wrong-security, wrong-alias, wrong-calendar, wrong-currency/unit, and tampered revisions fail without writes.
  - [x] Add complete-commit, every count/digest mismatch, excluded-member/no-result, valid-member/one-result, missing/extra/duplicate member, malformed canonical record, rollback, reopen, immutable-trigger, identical retry, conflicting retry, and `ThreadPoolExecutor` race tests using independent SQLite connections.
  - [x] Add coverage tests for empty, one month, adjacent months across year-end, gaps, multiple profiles, active-profile rollover, monotonic activation, mixed provenance, exact interval readiness, partial overlap, and duplicate no-op.
  - [x] Keep import-graph tests proving snapshot/profile code cannot import live SIPP/ISA portfolio, trade, cash, `TraderAgent`, order submission, routes, notifications, or job orchestration.
  - [x] Run focused Backtest tests, then the complete repository test and quality suite.

### Review Findings

- [x] [Review][Patch] [High] Rebuild exclusion proofs from verified immutable evidence instead of accepting self-attesting caller proofs [app/repositories/backtest_repo.py:791]
- [x] [Review][Patch] [High] Cross-check every provider and request identity field between stored evidence and its canonical manifest [app/services/backtest/snapshot_profile.py:642]
- [x] [Review][Patch] [High] Validate exclusion-supporting observations for canonical shape, numeric semantics, and exchange sessions [app/services/backtest/snapshot_profile.py:711]
- [x] [Review][Patch] [High] Revalidate each effective observed symbol against immutable alias evidence for the target session [app/repositories/backtest_repo.py:832]
- [x] [Review][Patch] [High] Recompute member source cutoff, source payload digest, and provenance digest during commit validation [app/services/backtest/snapshot_profile.py:413]
- [x] [Review][Patch] [High] Reject profiles whose calendar version or digest differs from the canonical TradingCalendar authority [app/repositories/backtest_repo.py:598]
- [x] [Review][Patch] [Medium] Bind profile detector API versions to their authoritative detector source manifests [app/services/backtest/snapshot_profile.py:48]
- [x] [Review][Patch] [High] Prevent deletion and activation-sequence reset of the singleton active profile pointer [app/repositories/backtest_repo.py:318]
- [x] [Review][Patch] [High] Revalidate closed months with a repository-owned clock rather than caller-controlled validated_as_of [app/repositories/backtest_repo.py:663]
- [x] [Review][Patch] [Medium] Reject empty observed-BAU source run identifiers [app/services/backtest/snapshot_profile.py:305]
- [x] [Review][Patch] [High] Reverify stored month keys, canonical columns, members, results, counts, and digests before reporting coverage or readiness [app/repositories/backtest_repo.py:1006]
- [x] [Review][Patch] [High] Preserve stable evidence/provider failure codes instead of collapsing every verifier failure to integrity_error [app/repositories/backtest_repo.py:786]
- [x] [Review][Patch] [Medium] Preserve calendar_error for malformed, current, and future month validation [app/services/backtest/snapshot_profile.py:329]
- [x] [Review][Patch] [Medium] Restrict strict month labels to ASCII YYYY-MM digits [app/services/backtest/trading_calendar.py:17]
- [x] [Review][Patch] [Medium] Normalize non-object evidence JSON to SnapshotContractError instead of leaking AttributeError [app/services/backtest/snapshot_profile.py:642]

## Dev Notes

### Scope Boundary

This story turns already reconstructed member records into immutable, queryable monthly coverage. It owns canonical profile/month/member contracts, exclusion-proof validation, transactional snapshot commits, the active-profile pointer, contiguous coverage, interval readiness, and duplicate no-op detection.

It does **not** fetch a roster, orchestrate reconstruction, run a durable job, expose an HTTP route/UI, project notifications, promote BAU scanner runs, or launch a Backtest. Story 1.7 composes these APIs into initialization jobs; Story 1.9 renders coverage; Story 1.10 supplies observed-BAU commits through the same predicate. Do not implement forward-looking snapshots: only fully closed historical calendar months are reconstructed.

### Canonical Profile and Month Identity

`SnapshotProfileV1` must be a policy identity, not a bag of mutable month inputs. Its canonical manifest includes at least:

- schema/version for the profile manifest and `historical_scan_record.v1`;
- the fixed ordered detector IDs, API versions, and AD-5 source-manifest digests;
- `ReconstructionRosterPolicyV1` and the compatible roster lineage/digest contract;
- identity/alias policy versions, source policy, and `reconstructability.v1`;
- calendar policy version, `exchange-calendars-v1`, and the exact 1970–2100 session-table digest;
- `YFinanceDailyProviderNativeV1`, historical evidence canonicalizer/source-manifest identity, and price-plane policy version where required by the produced records;
- the closed provenance vocabulary and cadence `per-exchange month_end`.

The profile hash excludes acquisition timestamps and per-month evidence. The committed month binds the exact roster/member set, effective aliases, per-member MIC session, record bytes/digest, detector input revision, provider evidence revision/request, exclusion proof, and provenance. This separation is what permits compatible reconstructed and observed-BAU months to share policy identity without pretending their source evidence is identical.

Use lowercase SHA-256 over canonical UTF-8 JSON with sorted keys, normalized strings, finite values, explicit nulls, and no trailing newline. Reuse the existing canonicalizers; do not rely on dataclass/dict insertion order or SQLite row order.

### Transactional Month Invariants

Prepare a complete immutable `MonthlySnapshotCommitV1` before persistence. Within one `BACKTEST_DB` write transaction:

1. Verify the profile exists or compare-and-insert its canonical manifest.
2. Verify the roster/profile relationship and all expected security IDs.
3. Insert exactly one member row per expected security in stable `security_id` order.
4. Insert exactly one canonical scan row for each `valid_scan` member and none for exclusions.
5. Insert the month manifest only when every count and digest recomputed from stored rows matches the supplied manifest.
6. Re-read and verify the committed winner before returning.

The month manifest is the Ready marker, so insert it last (or use deferred composite foreign keys if member rows reference it). No reader may infer Ready from detector-cache fragments or orphan rows. Prefer immediate constraints and an insert order that is valid at every statement; if deferred FKs are used, name them explicitly and test commit-time failure. SQLite requires `PRAGMA foreign_keys=ON` on every connection, which the existing repository session path already tests.

`BEGIN IMMEDIATE` acquires the SQLite write transaction before the compare-and-insert sequence; it may report `SQLITE_BUSY` when another writer owns the database. Keep the transaction bounded and let the existing connection/busy policy handle contention—do not add an unbounded retry loop. The repository pattern and official SQLite transaction semantics are the implementation authority.

### Legitimate Exclusion Proof

`before_first_provider_observation` is a narrow evidence result, not a business judgement. Its canonical proof must include:

- `security_id`, observed/requested symbol, alias result and revision;
- target snapshot month, MIC, canonical target session, calendar version/digest;
- provider and request-contract version, explicit full-history bounds/scope, evidence revision/manifest digest;
- first observed valid session, target session, currency, quote unit, and acquisition timestamp;
- the assertion `target_session < first_observed_session` and wording/fact that this is provider-observed lifetime, not verified listing history.

The proof must be rebuilt and verified from immutable evidence, not accepted as caller-supplied JSON. Any ambiguity or failed proof aborts the month. Do not persist a scan record for an excluded member.

### Coverage Contract

Coverage is always scoped to one explicit profile. `coverage()` with no explicit profile reads the singleton active pointer once and queries that hash; explicit pinned-profile reads are needed by jobs and later replay. A profile switch must not let old months pad the active result.

For sorted month labels, split intervals whenever the next label is not the next calendar month. Example: `2024-01, 2024-02, 2024-04` yields `[2024-01..2024-02]` and `[2024-04..2024-04]`. Earliest/latest/count remain summary fields and are never an eligibility predicate.

Interval readiness enumerates every inclusive month and requires an exact committed month manifest for the pinned profile. Return the missing month labels for internal orchestration, but do not turn member/provider failures into the prohibited user-facing source-gap inventory. If no months are missing, return `no_op=true` and perform no writes. If some are missing, Story 1.7 may process only those months while the whole requested range remains not Ready until all are committed.

### Existing Code to Extend and Preserve

- `app/repositories/backtest_repo.py` currently owns qualification, immutable identity/alias/roster evidence, and the Story 1.5 detector cache. Extend `ensure_schema()` and repository methods without weakening existing append-only triggers, exact cache key verification, `BEGIN IMMEDIATE` compare-and-insert behavior, or foreign-key enforcement.
- `app/services/backtest/trading_calendar.py` already owns the closed MIC mapping, early/unscheduled closure fixtures, and canonical 1970–2100 XNYS/XLON digest. Add public month/range APIs while preserving the digest bytes and all Story 1.1 tests.
- `app/services/backtest/historical_scan_record.py` is the sole scan-record serialization authority. Parse and reserialize every valid record through it; never duplicate its field schema or accept permissive `StockScan`/dict payloads.
- `app/services/backtest/historical_scan_reconstruction.py` returns complete `ReconstructionResultV1` records/fragments and verifies exact roster/evidence/calendar/detector identity. Consume its canonical record and input revision; do not call detectors again in the commit layer.
- `app/repositories/historical_price_repo.py` verifies immutable `StoredHistoricalEvidence`. Exclusion validation must use verified evidence; do not read yfinance directly or use the overwrite-only live price cache.
- `app/services/backtest/reconstruction_roster.py` and `security_identity.py` own immutable roster, opaque security IDs, effective aliases, and normalization. Do not derive identity from symbol text or capture a replacement roster in this story.

### Failure Mapping

Use stable failures already established by the architecture:

| Condition | Code |
|---|---|
| malformed/current/future month, unsupported MIC, calendar inconsistency | `calendar_error` |
| missing observation/evidence or insufficient full-history proof | `required_data_missing` |
| multiple/no deterministic alias candidates where identity is claimed | `identity_ambiguous` |
| canonical bytes, digest, count, profile, FK, immutable-key, or stored-row mismatch | `integrity_error` |
| provider adapter failure while acquiring proof | preserve its closed AD-21 provider code |

Repository methods must not leak raw `sqlite3.IntegrityError` for expected content conflicts; wrap them as `BacktestIntegrityError` with `code='integrity_error'` while preserving the original exception as the cause. Programmer/configuration errors may still fail loudly during development.

### Testing and Quality Guardrails

- Use fixed canonical fixtures and byte/digest golden assertions; avoid snapshots that bless arbitrary output changes.
- Race tests must use separate SQLite connections and verify both row counts and canonical winner bytes after threads complete.
- Test transaction rollback by failing after some candidate member/result inserts and proving that no profile-month/member/result rows escaped.
- Test process reopen and direct tamper attempts through SQLite to prove repository reads revalidate stored bytes/digests.
- Preserve all Story 1.1–1.5 Backtest tests. No network is needed for focused Story 1.6 tests; use immutable fixture evidence and monkeypatch network entry points to fail if called.
- Run Ruff, formatting, Pyrefly, focused tests, and the complete suite. Report unrelated pre-existing quality findings separately; do not edit parallel-agent files outside this story.

### Previous Story Intelligence

Story 1.5 established strict canonical Pydantic models, detector source/input manifests, complete-record reconstruction, and immutable detector-cache compare-and-insert. Its adversarial review found that caller-supplied provenance was insufficient: authoritative roster membership, detector source digests, provider evidence, exact calendar sessions, deep immutability, and cross-field semantics all had to be revalidated. Story 1.6 must apply the same rule at the monthly boundary: never trust a supplied count/digest/profile/proof when it can be recomputed from canonical evidence.

The current branch includes Story 1.4 and Story 1.5 because Story 1.5 depends on the deterministic market planes. Story 1.6 should build on commit `d8f644c` and remain limited to snapshot/profile coverage files so parallel SIPP work is untouched.

### Git Intelligence Summary

- `d8f644c feat(backtest): reconstruct canonical historical scans` added the Story 1.5 contracts, reconstruction service, detector cache, and 119 focused/875 full-suite passing baseline after review.
- `4752418 feat(backtest): add deterministic market planes` established immutable evidence-to-plane interpretation used by reconstruction.
- `43bea29 feat(backtest): store immutable historical price evidence (#211)` is the mainline evidence repository prerequisite.
- Follow the existing repository convention: module-level idempotent SQL schema, frozen write-set dataclasses, raw `sqlite3` through `Connect`/`session`, explicit `BEGIN IMMEDIATE`, canonical compare-and-insert, and focused repository concurrency tests.

### Current Technical Notes

- Use repository-locked Python/Pydantic/SQLite/exchange-calendars versions; this story requires no dependency upgrade.
- Official SQLite semantics confirm that `BEGIN IMMEDIATE` starts a write transaction immediately and may fail with `SQLITE_BUSY` if another writer is active. Foreign keys are connection-scoped and immediate by default; composite child/parent keys must have matching cardinality.
- `exchange_calendars==4.13.2` remains the architecture-locked authority. Do not replace it with current wall-clock heuristics, yfinance trading-day inference, or another calendar package.

### Project Structure Notes

Expected files:

- UPDATE `app/repositories/backtest_repo.py`
- UPDATE `app/services/backtest/trading_calendar.py`
- NEW `app/services/backtest/snapshot_profile.py`
- NEW `tests/backtest/test_snapshot_profile.py`
- NEW `tests/backtest/test_snapshot_coverage_repository.py`
- UPDATE existing Story 1.1 calendar/repository tests only where public API coverage is needed

Do not add routes/templates, job tables/services/workers, notification changes, BAU Scanner mutation, live portfolio imports, or Strategy execution in this story.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Epic 1, Story 1.6]
- [Source: `_bmad-output/planning-artifacts/prds/prd-Agents.stocks-2026-08-09/prd.md` — FR-4 through FR-7, FR-12, reproducibility and safety]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-5, AD-9, AD-13, AD-14, AD-18, AD-19, AD-21, AD-22]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/EXPERIENCE.md` — Coverage summary, Historical Coverage Rules, initialization no-op/failure]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/DESIGN.md` — Coverage summary and historical-only presentation]
- [Source: `_bmad-output/implementation-artifacts/1-5-reconstruct-and-cache-canonical-historical-scan-records.md` — canonical record/cache contracts and review learnings]
- [Source: `app/repositories/backtest_repo.py` — current schema and compare-and-insert conventions]
- [Source: `app/services/backtest/trading_calendar.py` — canonical MIC/session authority]
- [Source: `app/services/backtest/historical_scan_record.py` — canonical monthly result payload]
- [Source: `app/services/backtest/historical_scan_reconstruction.py` — reconstructed record/input identity]
- [SQLite Transactions](https://sqlite.org/lang_transaction.html)
- [SQLite Foreign Keys](https://sqlite.org/foreignkeys.html)

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- RED: Story 1.6 tests initially failed because the snapshot profile contract and repository APIs did not exist.
- GREEN: focused snapshot profile, calendar, persistence, coverage, exclusion, and concurrency tests passed.
- Regression: complete repository suite passed with 911 tests; touched-file Ruff, format, and Pyrefly checks passed.
- Review remediation: all 15 accepted adversarial findings were patched and covered by focused regression tests; 205 Backtest tests passed.

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Added strict canonical snapshot/profile, member-resolution, exclusion-proof, monthly commit, coverage, and readiness models with content-derived identities.
- Added closed-month calendar APIs and deterministic calendar-month interval handling.
- Added immutable transactional snapshot persistence, active-profile activation, exact coverage discovery, and interval-readiness evaluation.
- Added evidence re-verification, complete-write-set validation, idempotent retries, deterministic conflict behavior, and atomic rollback protection.
- Added focused boundary, persistence, provenance, import-isolation, and concurrent-commit tests.
- Kept durable initialization jobs, progress, routes, notifications, and UI out of scope for Story 1.7 and later stories.
- Completed adversarial review hardening for authoritative evidence, aliases, calendar/profile identity, immutable reads, stable failure codes, and active-profile monotonicity.

### File List

- `_bmad-output/implementation-artifacts/1-6-commit-versioned-monthly-snapshot-coverage.md`
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `app/repositories/backtest_repo.py`
- `app/services/backtest/snapshot_profile.py`
- `app/services/backtest/trading_calendar.py`
- `tests/backtest/test_snapshot_coverage_repository.py`
- `tests/backtest/test_snapshot_profile.py`
- `tests/backtest/test_trading_calendar.py`

### Change Log

- 2026-08-11: Implemented Story 1.6 canonical monthly snapshot coverage and moved the story to review.
- 2026-08-12: Applied all 15 code-review patches, passed 911 repository tests, and completed Story 1.6.
