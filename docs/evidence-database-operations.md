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
across restarts; bounded cold completed Result reads remain open under GH-539.
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
