---
title: GH-539 durable verified coverage for cold-process navigation
type: feature
created: 2026-09-09
status: done
final_revision: 5b583ac8da55cb512da098e05ffab7d79d5818d8
baseline_revision: d067ef73283c02fe8f0dec9ebdec01d02476cdcd
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issues: [444, 539]
---

<intent-contract>

## Intent

**Problem:** First Strategy Manager navigation in each process still spends roughly 126 seconds verifying unchanged coverage.

**Approach:** Persist verified coverage against the existing transactional revision identity and verifier version. Prepare coverage automatically during app boot while the splash screen remains visible; fresh processes reuse the verified projection. Retain an explicit maintenance command.

## Boundaries & Constraints

**Always:** Preserve runtime authority, source invalidation, fail-closed verification, bounded process cache and consistent SQLite read snapshots. Publish only against the source revision actually verified. Missing or stale projections retain full verification fallback.

**Block If:** The improvement requires trusting unverified source evidence or pretending missing coverage is ready.

**Never:** Modify live databases for benchmarks, bypass invalid runtime authority, silently return empty coverage, or claim all cold Result acceptance criteria are complete. First-ever/stale verification remains expensive; perform boot preparation in a background thread so the splash can render, and expose preparation failures without trapping the user behind the splash.

## I/O & Edge-Case Matrix

| Scenario | Input/state | Expected behavior | Failure handling |
|---|---|---|---|
| Restart | Valid persisted summary | No bulk evidence reads | Validate authority and projection integrity |
| Source change | Revision differs | Full existing verification | No stale summary returned |
| Publication race | Writer commits after verification | Skip old publication | Never relabel old summary with new revision |
| Bad projection | Malformed JSON/digest/identity | Reverify source | Never return damaged projection |
| Busy publication | Valid summary, writer unavailable | Return verified result | Skip persistence for bounded busy/locked failure |

</intent-contract>

## Code Map

- `app/repositories/backtest_repo.py`: coverage cache, revision counters, SQLite schema, full verifier.
- `app/services/backtest/snapshot_profile.py`: existing CoverageSummaryV1 serialization.
- `tests/backtest/test_snapshot_coverage_repository.py`: existing correctness/concurrency tests.
- `scripts/benchmark_strategy_navigation.py`: isolated-copy navigation benchmark.

## Tasks & Acceptance

**Execution:**
- [x] `app/api/app.py`, `app/api/routes/views.py`, `app/api/templates/index.html`, `app/api/static/css/splash.css` and focused boot tests: run coverage preparation at startup, expose pending/ready/failed state, hold splash during known pending work, preserve font/tab gates, release with visible error on failure and escape on network/script failure.
- [x] `app/repositories/backtest_repo.py`: preserve schema_version across unchanged startup by conditionally migrating replacement triggers; retain atomic migrations and external DDL invalidation.
- [x] `app/repositories/backtest_repo.py`: add idempotent summary table; versioned, checksummed bounded projection with profile/display identity validation; reuse only matching revision and authority; preserve full fallback.
- [x] `app/repositories/backtest_repo.py`: publish after closing verification read transaction using BEGIN IMMEDIATE and revision comparison; retain already-verified result on SQLITE_BUSY/LOCKED publication failure only; do not cache under a newer revision.
- [x] `tests/backtest/test_snapshot_coverage_repository.py`: restart avoids bulk reads; stale/corrupt/version-mismatch fallback; runtime authority rejects; publication race, rollback, locked persistence, verification failure; update restart expectation to reuse persisted projection.
- [x] `scripts/prepare_snapshot_coverage.py`: explicit database path and optional profile hash; initialize schema, verify/persist active or selected coverage; documented writable maintenance command, no provider/worker lifecycle.
- [x] `docs/evidence-database-operations.md` and `gh-539-durable-navigation.json` in implementation artifacts: preparation instructions, benchmark cold-process first visit on same offline source after prewarm, honest first-ever/stale limitation.
- [x] `sprint-status.yaml` and this specification: retain GH539 in progress for bounded cold Result work; local commit with review and checks.

**Acceptance Criteria:**
- Given prepared unchanged evidence, when a fresh repository serves coverage, then it performs no bulk member/month/result/roster/alias reads and returns the exact verified summary.
- Given stale, corrupt or incompatible persistent projection, when coverage is requested, then full original verification runs before any usable summary is returned.
- Given publication races or bounded write contention, when verification finishes, then source-consistent coverage remains available without publishing an incorrect revision.
- Given the approved offline database, when prewarmed and opened in a fresh process, then measured first navigation is recorded separately from preparation cost and without claiming completed Result or p95 acceptance.

## Spec Change Log

2026-09-09: Explicit user steering requests expensive work during app boot while splash is visible. Updated intent to authorize background startup preparation and splash/status flow; this user instruction supersedes the previous no-background-UI boundary. Preserve durable verification and normal navigation semantics.

2026-09-09: Startup investigation found unconditional trigger recreation invalidating schema identity on every restart. Added idempotent trigger migration task; preserve actual trigger upgrades, existing atomicity and external DDL invalidation.

## Review Triage Log

## Design Notes

The durable projection is a repository-maintained derived cache, under the same trusted SQLite writer boundary as revision counters. Detect accidental projection corruption with a digest and model/identity validation; deliberately forging projection plus digest or disabling accounting is outside that boundary. Version the verifier explicitly and invalidate projections when verification semantics change. Keep preparation out of per-month commit loops to avoid quadratic work.

## Verification

Focused coverage tests, full pytest suite, Ruff, Pyrefly with project interpreter, git diff check; isolated real-data preparation and fresh-process navigation benchmark. Existing 33 alert-agent typing errors are baseline.


### 2026-09-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 9 (high 1, medium 3, low 5)
- defer: 0
- reject: 0
- addressed_findings:
  - [high] [patch] Await startup preparation asynchronously before Strategy Manager routes can contend on the repository lock; real-route/real-lock test proves status stays responsive.
  - [medium] [patch] Join preparation on cancelled lifespan teardown before shutting down workers; cancellation regression test passes.
  - [medium] [patch] Restrict skipped trigger drops to matching replacements, preserving standalone removal migrations.
  - [low] [patch] Browser test exercises stalled status request and AbortController escape.
  - [low] [patch] Browser tests prove both font and first-tab gates remain effective after preparation succeeds.
  - [low] [patch] Explicitly test runtime authority rejection after reopening a persisted summary.
  - [low] [patch] Test failed replacement rollback preserves the old trigger.
  - [low] [patch] Correct documentation to distinguish implemented durable summaries from remaining cold Result work.
  - [medium] [patch] Record prepared fresh-process navigation separately from preparation and a backup-induced invalidation experiment.

### 2026-09-09 — Independent follow-up
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 0
- reject: 0
- addressed_findings:
  - none
- Reviewer found no new actionable regressions in the patched production paths.

## Auto Run Result

Durable verified coverage removes whole-profile verification on unchanged process restarts. App startup prepares active coverage in an owned background thread while the existing splash stays visible. Strategy Manager requests wait asynchronously for startup; status polling and the shell remain responsive. Failure releases the splash with a visible message, and details are logged server-side. Unchanged trigger definitions no longer cause schema invalidation.

The approved offline 320-month profile took 126.333 seconds for initial verification; a fresh preparation process reused it in 3.725 seconds. A fresh navigation process against the prepared database took 3.256 seconds for first landing and roughly 63 ms for later visits. A separate new-backup experiment invalidated schema identity and reverified in 131.925 seconds, as documented. No live database was used for benchmark writes.

Changed files: repository and coverage tests; app lifecycle, startup-status route, splash HTML/CSS and lifecycle/browser tests; preparation CLI; operational docs; this spec, sprint tracking and two benchmark JSON artifacts.

Verification: 68 focused tests pass, including six real Chromium splash scenarios. Ruff passes. Pyrefly retains only the 33 existing alert-agent errors. Final full-suite result recorded below. Review also exposed and fixed read-only benchmark compatibility: query-only diagnostics verify without attempting projection publication, while explicit preparation still requires persistence.

Residual scope: initial or invalidated evidence still needs full verification, now performed at boot when active. Post-boot evidence mutations and unprepared pinned profiles can still require verification on demand. Bounded completed Result/member reads and full GH539 p95 acceptance remained open at the time of this implementation. Persisted projections and revision accounting share the trusted SQLite writer boundary; checksums do not authenticate malicious coordinated rewrites. No push or deployment.

### Closure addendum — 2026-09-12

The previously open GH-539 completed-Result/member-read gate is satisfied by
`gh-539-result-rendering.json`: the current-authority 241-month Result route
returned HTTP 200 for all warm and cold samples, with p95 of 0.120 seconds warm
and 3.325 seconds cold. Application startup is excluded from the cold route
timing and OS page-cache state is recorded as uncontrolled. The durable coverage
and boot preparation implementation therefore has its remaining GH-539
acceptance evidence.

Final verification: **3,077 passed**, 27 warnings, 121.62 seconds. Final fresh-process check after review: first landing 3.681 seconds; later landing ~75 ms (full regression suite running concurrently). Retained in `gh-539-durable-final-navigation.json`. Worktree diff whitespace check passes.
