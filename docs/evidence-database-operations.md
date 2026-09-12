# Evidence database baseline and concurrency

This is the baseline/observability story [#535](https://github.com/ed-is-ai/Agents.stocks/issues/535)
of [epic #444](https://github.com/ed-is-ai/Agents.stocks/issues/444).
It does not migrate historical storage, enable garbage collection or establish
the epic's storage/Result-rendering performance targets. Those remain tracked
in [#536](https://github.com/ed-is-ai/Agents.stocks/issues/536) through
[#540](https://github.com/ed-is-ai/Agents.stocks/issues/540).

## Connection and transaction policy

Both evidence repositories enable WAL during schema initialization. Each fresh
repository connection uses a 5,000 ms SQLite busy timeout, FULL synchronous
durability and the default 1,000-page automatic passive checkpoint. SQLite's
busy handler retries lock acquisition within that bound; the application does
not replay arbitrary transactions or duplicate side effects. Some lock-upgrade
conflicts can fail immediately rather than waiting for the full timeout.

Existing repository transactions remain authoritative. A reader with an
explicit transaction retains its snapshot while a WAL writer commits. Competing
writers either acquire the lock or raise the original SQLite exception.
Connections close on success and failure; uncommitted writes roll back on close.
Long readers can retain WAL pages, so monitor the main file **and** `-wal` file.
The policy does not promise bounded WAL growth while a reader holds a snapshot.

Durable worker failure details retain the SQLite exception class, numerical
code, symbolic name and a fixed operation label, including wrapped causes.
They omit raw SQLite messages that can contain SQL, parameters or paths.
For example: `initialization.month: sqlite3.OperationalError; code=5; name=SQLITE_BUSY`.
Missing error attributes appear as `unknown`; existing non-SQLite diagnostic
behavior remains unchanged. If the database stays unwritable even for the
failure update, normal worker lease/recovery handling remains responsible.

When waits exhaust, investigate competing writers and long transactions before
restarting the existing job through its normal recovery path. Do not remove WAL
or shared-memory files while connections are open. During a maintenance window
with workers and application connections stopped, back up first and use a
standard SQLite checkpoint if WAL reclamation is needed; do not treat VACUUM
as a substitute for the planned storage migration.

## Consistent offline backup

Never copy just the live `.db` file: committed pages may still reside in WAL.
Use SQLite's backup API from a read-only source connection, writing a separate
destination with enough free disk for both databases plus headroom. The example
uses an explicit read transaction so frequent source commits do not repeatedly
restart the backup. Close that transaction promptly to release retained WAL.

```python
from contextlib import closing
from pathlib import Path
import sqlite3

source = Path("/absolute/source/backtest.db").resolve(strict=True)
destination = Path("/absolute/offline/backtest.db")
if destination.exists():
    raise FileExistsError(destination)
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    src.execute("BEGIN")
    src.execute("SELECT count(*) FROM sqlite_master").fetchone()
    with closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=8192)
```

Repeat for `historical_price_cache.db`. Each copy is internally consistent;
independently copied live databases are **not** a single cross-database snapshot.
This is sufficient for separate baseline inventories and Backtest component
timings. Stop all evidence writers first when a future migration/GC/replay
rehearsal needs a consistent cross-database reference graph. Keep backup metadata
(time, source, sizes and revision) alongside output. Benchmark copies must stay
offline and unmodified for repeatable measurements.

## Repeatable benchmark

From the repository root, using its installed Python environment:

```sh
python -m scripts.benchmark_evidence_databases \
  --historical-db /absolute/offline/historical_price_cache.db \
  --backtest-db /absolute/offline/backtest.db \
  --code-revision COMMIT_SHA_OR_COMMIT_SHA_DIRTY \
  --snapshot-id BACKUP_METADATA_IDENTIFIER \
  --run-id 8f1538db-3647-4730-88b4-ff3fa0412a8e \
  --repetitions 20 > baseline.json
```

The command opens both inputs read-only and never calls schema initialization
or a provider. Omit `--run-id` for inventory only; `--profile-hash` measures
coverage and roster without a Result. Supplying both must match the pinned run.
The required revision and snapshot identifier bind output to the code and
backup metadata; record any uncommitted code changes with the revision.
The JSON includes row counts, logical/file/free/WAL bytes and table/index bytes
when the SQLite build supports `dbstat`, plus Python/SQLite/platform metadata.
Inventory intentionally scans tables, so run it offline. Timings precede the
inventory scans to avoid warming the relevant pages through counting.

The first sample uses an empty repository cache. Subsequent samples reuse that
repository. Neither implies a genuinely cold operating-system page cache;
backup and earlier inspection may already have warmed it. Percentiles use
nearest-rank; use at least 20 warm repetitions for a useful p95. Component totals
cover Result lookup, coverage, roster and first-month member verification, not
HTTP, presentation or HTML rendering. Report these separately from the epic's
under-two-second warm/under-five-second cold **rendering** target. Repeated cold
rendering acceptance requires an isolated process and controlled OS cache state,
hardware and input snapshot, to be implemented in the Result optimization story.

An `integrity_failure` status retains the component, sample and domain error.
Its durations measure rejection, not successful Result reads. The tool never
disables profile authority, digest or provenance checks to obtain a timing.

## Recorded baseline, 2026-09-08

The operator selected main's database. Its symlinks resolve to the original
checkout's live data, so measurements used separate SQLite-backup copies in
`/private/tmp/Agents.stocks-gh-444-493-benchmark`. Main was merged through
`85e0d48c` before this run; Python 3.14.6 / SQLite 3.53.3 on macOS arm64.
The backup uses about 35.31 GiB; retain it for follow-up comparisons while
capacity permits. No schema initialization or write was performed on the source.

| Inventory | Observed |
|---|---:|
| Historical file | 27,366,866,944 bytes (25.49 GiB) |
| Historical revisions | 6,111 |
| Historical observations | 44,603,636 |
| Historical reference rows | 1,098,596 |
| Historical free pages | 0 bytes |
| Backtest file | 10,550,337,536 bytes (9.83 GiB) |
| Backtest snapshot months, across profiles | 1,243 |
| Backtest snapshot members | 1,098,596 |
| Reconstruction cache rows | 2,382,811 |

The 241-month profile still exists: `e0569f59267ae151fd4857e7e8a8d6afa3846eb6934a0a21f851cbac37ccd5f7`,
covering July 2006–July 2026. The selected completed run is
`8f1538db-3647-4730-88b4-ff3fa0412a8e`. Both coverage and member verification
reject this profile because its detector manifests differ from current runtime
authority. No successful Result-rendering latency can be claimed from it.

Five repeated samples were taken after first use. Successful Result lookup
measured 0.130 s first use and 0.006 s warm median; roster lookup measured 0.003 s
first use and 0.003 s warm median. Coverage **rejection** took 9.785 s first use
and 3.167 s warm median. This demonstrates that even the rejecting path still
performs substantial work; it does not establish a successful-read p95.

Raw samples, failure details, full table/index sizes and counts are in
[`gh-535-database-baseline-2026-09-08.json`](../_bmad-output/implementation-artifacts/gh-535-database-baseline-2026-09-08.json).
The Result optimization story must resolve reference-profile/runtime
compatibility without weakening integrity before measuring successful rendering.

## Strategy Manager Agent boundary (#493)

`StrategyManagerAgent.run(BacktestLaunchCommandV1)` synchronously delegates to
`BacktestLaunchService.launch` and returns the existing Backtest or Preparation
enqueue result, including its durable job handle. The primary configuration POST
uses this Agent through the existing FastAPI launch-service dependency.

Validation and the choice between preparation and backtest remain in the launch
service. FIFO dispatch, child-process startup, claim tokens, leases, cancellation,
progress and recovery remain in the current job service and worker. Bootstrap,
historical initialization and polling retain their current entry points. The
stateful simulation engine therefore stays outside the synchronous Agent
contract described by the architecture spine.
# Strategy Manager navigation increment (GH-539)

Verified coverage is also persisted for reuse after process restarts. The app
prepares active coverage during startup while the boot splash remains visible.
The shell is served while verification runs, so the user sees preparation status
instead of waiting for a blank page. An unchanged prepared database skips bulk
verification on subsequent boots. For an explicit maintenance preparation:

```sh
python -m scripts.prepare_snapshot_coverage --backtest-db /path/to/backtest.db
```

Use `--profile-hash HASH` to prepare a pinned profile instead of the active one.
This maintenance command initializes schema and writes derived summaries in the
selected database; use the normal backup procedure first. It confirms durable
publication rather than reporting success when a racing writer or lock prevented
persistence. It neither fetches provider data nor starts workers.

Each summary is tied to the source revision and an explicit verifier version.
Runtime authority is still checked on every read. Damaged, stale or absent
summaries fall back to full verification; the first-ever or post-mutation read
can therefore still be slow. A matching prepared summary avoids that work in a
new process. Changes to verification rules must bump the verifier version.
Projection checksums detect accidental corruption, not a malicious writer that
forges both the projection and its checksum. Accounting and derived projections
share the repository's trusted SQLite writer boundary.

Repeated coverage reads now use SQLite revision counters instead of scanning and
hashing every profile member and scan result. Independent triggers increment the
affected profile or shared generation inside the source write transaction. Cache
hits still check runtime authority; misses still perform full evidence verification.
Schema changes invalidate the process cache, and a database epoch distinguishes
new databases. Deliberately disabling accounting or rewriting database files is
outside this cache trust boundary.

The approved main Backtest database was copied through SQLite backup before any
benchmark schema writes. Three sequential in-process GETs per screen measured:

| Screen | Before warm median | After warm median |
| --- | ---: | ---: |
| Strategy Manager landing | 9.869 s | 0.078 s |
| Initialization | 4.607 s | 0.066 s |
| Readiness | 4.640 s | 0.031 s |
| Configuration | 5.272 s | 0.026 s |
| Backtests | 0.027 s | 0.025 s |

Every measured request returned HTTP 200. These are navigation measurements,
not successful completed Result benchmarks: pages may show readiness errors.
In the initial navigation increment, first landing remained expensive (137.879 s
before, 126.063 s after). The durable-summary increment below addresses reuse
across restarts. The completed Result benchmark below closes the remaining GH-539
performance gate.
Two warm samples establish the observed improvement, not a reliable p95.
OS caches were uncontrolled; the historical price cache was empty and isolated.
Workers and application lifespan were disabled.

Raw evidence is in `gh-539-navigation-before.json` and
`gh-539-navigation-after.json` under `_bmad-output/implementation-artifacts/`.
To repeat with a fresh disposable backup (allow space for another database copy):

```sh
python -m scripts.benchmark_strategy_navigation \
  --backtest-db /path/to/offline/backtest.db \
  --code-revision YOUR_REVISION --snapshot-id YOUR_BACKUP_ID \
  --output navigation.json --repetitions 5
```

The durable-summary follow-up verified the active 320-month profile on the same
offline database in **126.333 seconds**, then reused that verification in a fresh
preparation process in **3.725 seconds** (including schema initialization). A
fresh navigation process against that prepared database served its first landing
in **3.256 seconds** and subsequent landing requests in about **63 milliseconds**.
Startup now performs preparation behind the splash, before normal navigation.

Evidence: `gh-539-durable-prepared-navigation.json`. The separate
`gh-539-durable-navigation.json` experiment made another SQLite backup, which
changed the schema identity and correctly forced verification again (131.925 s).
Do not conflate a new backup with an unchanged prepared database. These timings
are individual observations with uncontrolled OS caches, not p95 or completed
Result acceptance. Existing readiness/activity errors are retained by the pages.

A final check after review measured first landing at 3.681 s and later visits
around 75 ms while the full regression suite was also running; see
`gh-539-durable-final-navigation.json`. All 3,077 tests passed, including actual
browser checks for slow preparation, failure, stalled polling, and font/tab gates.

Completed Result payloads are currently small: the three offline main-database
Results contain 446 equity points and 194 trade events in total; the largest has
about 69 KB of event JSON. Picker requests now verify the anchor once, and a
comparison verifies each side once before presentation. These are same-request
reuse changes only; each independent repository Result read still reconstructs
and validates its immutable digest. The measured cost was evidence coverage and
member verification, not Result payload reads. Raw counts and verifier-call
evidence: `gh-539-result-request-reuse.json`.

Result pages also persist the verified ordered member revision pairs for their
pinned start month. A fast-path read still checks the current profile authority
and transactional evidence revision; absent, stale, oversized, malformed, or
checksum-invalid projections run the existing full verifier. Startup prepares
distinct start months only for completed, non-tombstoned backtests of the active
profile, while the splash remains visible. The copied main database currently
has three completed Results, all pinned to a retired profile, so this increment
correctly selected zero startup months and does not claim an active completed
Result timing. The retained historical Results continue to use the existing
on-demand integrity path. Source counts and selection evidence:
`gh-539-member-revision-prewarm.json`.

## Completed Result rendering benchmark (GH-539)

The current-authority active profile
`8c891e8547236cfd66639ab6bd7e9bae7ad8676d2ca9984b5325b669d9efab93` has a ready
241-month interval from `2006-07` through `2026-07`. A deterministic Buy and Hold
run (`97c7accb-3b96-4e06-8c8c-b5bef3793c0e`) was completed against one valid USD
roster member on the offline copies identified by `gh539-current-2026-09-12`.
The Result route returned the same 769,235-byte body for every sample.

The repeatable benchmark command is:

```sh
python -m scripts.benchmark_result_rendering \
  --run-id 97c7accb-3b96-4e06-8c8c-b5bef3793c0e \
  --profile-hash 8c891e8547236cfd66639ab6bd7e9bae7ad8676d2ca9984b5325b669d9efab93 \
  --backtest-db /absolute/offline/backtest.db \
  --historical-db /absolute/offline/historical_price_cache.db \
  --code-revision 2f0293e53a456e37411c5a8b255a000f4b8fcfbb \
  --snapshot-id gh539-current-2026-09-12 \
  --output result-rendering.json
```

The benchmark records 20 warm requests in one process and five requests in
fresh processes; application startup is excluded from route timings and OS page
cache state is recorded as uncontrolled. Warm p95 was **0.120 seconds** and cold
p95 was **3.325 seconds**; all requests returned HTTP 200 and passed the normal
Result integrity path. Raw evidence is in
`gh-539-result-rendering.json` under `_bmad-output/implementation-artifacts/`.

## Historical evidence schema v2 (GH-536)

The repository can now persist an opt-in v2 representation of immutable
historical evidence. It stores the revision's canonical metadata separately
from zlib-compressed, content-addressed row and action chunks grouped by
calendar year. Internal chunk mappings use an integer revision key; the public
evidence identity remains the original canonical uncompressed digest.

Every v2 read checks the compression format, uncompressed length, chunk digest,
payload shape, row/action counts, and reconstructed canonical revision digest
before returning evidence. Identical annual content is shared by revisions.
The current default writer remains v1; generic revision reads can verify a
v2-only revision for migration compatibility. Activation, rollback, reference
migration, and any deletion of v1 storage belong to GH-537 and GH-538. No
production-sized storage-reduction claim has been made because the prior
offline historical backup is no longer available for a repeatable measurement.

A fresh read-only sample from the approved main historical database selected 25
revisions in lexical digest order. Their v1 canonical manifests totalled
53,110,749 bytes; v2 metadata plus compressed chunks totalled 5,561,397 bytes
(**89.53%** smaller). All 1,413 chunk references were unique in this sample,
so this is a compression and duplicate-representation result, not a measured
deduplication benefit. It does not establish the whole-cache 70% migration
target or a read-latency result. Raw sample evidence:
`gh-536-v2-encoding-sample.json`.

## Offline v2 rollout rehearsal (GH-540)

Perform this sequence only on SQLite-backup copies with application writers
stopped. Record the backup identifier, code revision, free space, command JSON,
and before/after `benchmark_evidence_databases` inventory beside the copies.

1. Run `python -m scripts.migrate_historical_evidence_v2 --historical-db COPY`
   repeatedly until `completed` is true. It verifies each canonical revision,
   checkpoints after each one, and refuses changed source revisions or inadequate
   capacity.
2. Review the reported size change and replay checks. Activation is permitted
   only with an explicit evidence reference:
   `--activate --activation-review REVIEW_ID`.
3. Reopen the copy and verify representative pinned and unpinned revisions with
   the normal repository read path. To prove rollback, run the same command with
   `--rollback` and repeat those reads.
4. Produce a retention dry run with
   `python -m scripts.retain_historical_evidence_v2 --historical-db COPY
   --grace-before UTC_TIMESTAMP`. Review candidates and exclusions. Execute only
   the reviewed plan with `--execute --review-reference REVIEW_ID`; it rechecks
   authoritative references under a write transaction and audits the result.

Do not run activation or retention execution against the production database
without a completed rehearsal, recorded capacity/restore evidence, and explicit
operator authorization. A failed or interrupted migration is resumed by rerunning
the migration command; a changed source is a stop condition, not a reason to
override verification. Retention keeps v1 rollback data and only reclaims
unreferenced v2 revisions and chunks.

The offline rehearsal recorded in `gh-540-rollout-rehearsal.json` completed on
2026-09-12 from consistent SQLite backup copies. The backup contained 6,685,394
historical pages and 2,578,764 Backtest pages. Migration checkpointed 10 rows
first and then completed all 6,142 historical revisions with 28,005,978,112
bytes available against a 6,867,070,976-byte reserve. Every v1/v2 reconstruction
matched (6,142 checked, zero mismatches). After activation, rollback loaded three
representative v1 revisions successfully; reactivation preserved three
representative referenced v2 reads. The reviewed retention plan reclaimed 1,570 v2
revisions and 20,123 chunks while excluding 4,406 authoritative references and
166 grace-period revisions. The production database was never activated during
the rehearsal.

### Authorized production cutover (GH-540)

Production activation was approved under review reference
`gh540-production-rollout-approved-2026-09-12` on 2026-09-12 using code revision
`33b04f3ee0bcb266b76e7b9f6be801201b064c57`. A fresh rollback backup was preserved
before migration (historical: 6,686,841 SQLite pages; Backtest: 2,584,780
pages). Migration completed all 6,143 revisions with 23,099,936,768 bytes
available against a 6,867,806,208-byte reserve. Full v1/v2 replay equality
checked all 6,143 revisions with zero mismatches.

The production storage state is now v2. Three representative repository reads
loaded successfully (1,759 rows each), all 6,143 v2 revision rows are present,
and the 4,406 referenced revisions remain available. A post-cutover SQLite
`quick_check` returned `ok`. The v1 data remains in place for rollback. No
production retention deletion was executed; the reviewed offline retention
execution remains recorded in `gh-540-rollout-rehearsal.json` for a separate,
auditable operation.
