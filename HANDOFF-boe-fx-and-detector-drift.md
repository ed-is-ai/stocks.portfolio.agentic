# Handoff — BoE FX provider (done) + detector generations (active profile verified)

## Origin of this session

The original request was to run all 6 strategy backtests (`rtly-backtest-buy-and-hold`,
`-darvas-box`, `-minervini`, `-moving-average`, `-turtle-trend`, `-weinstein`)
over 2000-01 to 2026-08, one at a time, initially proposing
`block_buy_on_downtrend_enabled=TRUE` and `enable_position_upgrade=TRUE` where a
strategy supports it. That surfaced
two unrelated problems, in order. The first is fixed and tested. The second is
a pre-existing data-integrity issue that blocks every backtest and needs a
decision before any of the six runs can happen.

### Superseding launch decision

The legacy `block_buy_on_downtrend_enabled` flag and the AAPL benchmark are not
part of the launch contract. The intended contract now uses
`regime_filter_enabled=true`, a 200-session moving average, and SPY as a
read-only reference benchmark. SPY must be acquired and pinned before launch;
its security ID is intentionally still unset in the readiness sheet. Minervini
and Weinstein upgrade flags remain explicit and are enabled according to the
original launch request; they must remain visible in each sealed manifest.

## Current state

- Repository: `/Users/me/Git/Agents.stocks`
- Branch: `fix/portfolio-strategy-artifact-read`
- Server: the historical restart failure is recorded below. A 2026-09-23
  read-only recheck found that the active profile is uniform and its authority
  matches the current runtime fingerprints; older profile generations remain
  retained historical evidence.
- Working tree is intentionally uncommitted. Do not reset or discard it.
- `data/` is untracked (gitignored) and contains the real SQLite databases —
  left in place deliberately.

### Pre-existing local changes, not part of this session's work

These were already modified in the working tree before this session started
(visible in the original git status) and are unrelated to the BoE/FX work
below — leave them alone unless the next task specifically concerns them:

- `app/api/routes/strategy_manager.py`
- `app/api/templates/_strategy_setup.html`
- `app/services/backtest/strategy_bootstrap_service.py`
- `tests/backtest/test_strategy_setup_routes.py`

## Part 1 — BoE FX provider (DONE, tested, safe to keep)

### Problem

Backtests with `base_currency=GBP` (or `USD`, since the universe mixes GBP-
and USD-denominated securities either way) over a range starting before
2003-12 fail with `fx_missing: Required GBP/USD FX close is missing.` Yahoo's
`GBPUSD=X` series (`yfinance`) only has data from 2003-12-01 onward — verified
directly:

```python
import yfinance as yf
yf.download("GBPUSD=X", start="1999-01-01", end="2003-01-01")  # empty
yf.download("GBPUSD=X", start="2000-01-01", end="2004-01-01")  # first row: 2003-12-01
```

### Fix

Added a Bank of England-backed `FxSeriesFetcher` (BoE's daily spot series
covers back to 1975) as an **honest, separately-provenanced** provider —
never spoofing `provider="yfinance"`.

**New code** (`app/integrations/fx_history.py`):
- `fetch_boe_fx_series(pair, start, end, *, request_get=...)` — generalizes
  the existing `_fetch_bank_of_england()` single-date fetch into a ranged
  fetch, reusing `_boe_rows`/`_parse_rate`/`_BOE_URL`/`_PAIR_SERIES`. Handles
  2-digit-year century disambiguation via `_boe_date_in_window`.
- `BankOfEnglandFxSeriesFetcher` — implements the `FxSeriesFetcher` protocol
  (`historical_price_evidence.py:548`). Builds a fully honest
  `HistoricalEvidencePayload`: `provider="bank_of_england"`,
  `request_contract` records the *actual* BoE HTTP params used (not a
  fabricated yfinance-shaped contract), rows hex-encoded via `float(x).hex()`
  to match `market_planes.provider_decimal`'s decode expectation.
- `BOE_FX_SERIES_REQUEST_CONTRACT_VERSION` constant.

**Changed to accept it** (`app/services/backtest/currency.py`):
- `_fx_closes()` no longer hardcodes `provider == "yfinance"`. Accepts
  `{"yfinance", "bank_of_england"}` and dispatches request-contract
  validation per provider (`_validate_fx_request_contract`) — BoE evidence is
  checked against its own honest contract version, never against
  `validate_provider_native_request_contract` (which hardcodes yfinance's
  exact `Ticker.history()` kwargs).

**Wired in as the default** (replacing `YFinanceFxSeriesFetcher()`):
- `app/api/dependencies.py` (`get_fx_series_fetcher`)
- `app/services/backtest/backtest_launch_service.py` (`BacktestLaunchService.__init__`)
- `app/services/backtest/worker.py` (`PreparationStageEngine` construction)

**Schema migration** (`app/repositories/historical_price_repo.py`):
- `historical_price_revisions.provider` had a hard SQL
  `CHECK(provider = 'yfinance')` — a database-level block, not just a Python
  check. Widened to `CHECK(provider IN ('yfinance', 'bank_of_england'))`.
- `HistoricalPriceRepository.ensure_schema()` now calls
  `_migrate_provider_check()`, a one-time idempotent migration for existing
  databases: detects the legacy constraint text in `sqlite_master`, builds
  the widened table under a **throwaway name**
  (`historical_price_revisions_boe_fx_replacement` —
  `_MIGRATE_REVISIONS_REPLACEMENT_TABLE`), copies rows, drops the original,
  renames the replacement into the original's place, then re-runs `_SCHEMA`
  to restore indexes/triggers and re-enable `foreign_keys`.
- **Important, hard-won detail**: do NOT rename the *original* table (e.g.
  `ALTER TABLE historical_price_revisions RENAME TO ..._old`). SQLite's
  `ALTER TABLE ... RENAME` rewrites other tables' `FOREIGN KEY REFERENCES`
  clauses to follow the new name, so `historical_price_observations` etc.
  end up pointing at the renamed-away table — then dropping it violates FK
  integrity (`sqlite3.IntegrityError: FOREIGN KEY constraint failed`, and
  even with `foreign_keys=OFF`, `_SCHEMA`'s own leading `PRAGMA foreign_keys
  = ON;` silently re-enables it if you `executescript(_SCHEMA)` in the
  middle of the migration). The fix that actually works: build the
  replacement under a **new** name, copy in, drop the **original** name,
  rename replacement **into** the original's name — so every child table's
  FK clause (which never changed) is satisfied the instant the rename
  completes, with no window where anything points at a renamed-away table.
  This exact failure mode is captured by
  `test_provider_check_migration_preserves_rows_and_widens_constraint` in
  `tests/backtest/test_historical_price_repository.py`, which deliberately
  includes a populated FK-child table (`historical_price_observations`) —
  an earlier version of this test passed with an empty child table and only
  failed for real against the live 31GB database.

### Verification already done

- 1,306 tests pass (`uv run pytest tests/backtest/ tests/test_fx_history.py
  tests/test_snapshot_price_backfill.py tests/test_snapshot_price_evidence.py`).
- `uv run pyrefly check` and `uv run ruff check` clean on every touched file.
- Migration applied to the **live** `data/historical_price_cache.db`
  (31GB) and verified: exactly one `historical_price_revisions` table, widened
  CHECK present, all 7,338 rows intact, `PRAGMA foreign_key_check` returns
  zero rows, `PRAGMA integrity_check` returns `ok`.
- A byte-verified backup of the pre-migration database exists at
  `data/historical_price_cache.pre-boe-migration-20260920-105950.db` (same
  size, `integrity_check: ok`). Keep it until this work is committed and
  confirmed stable.
- **Not yet verified**: an actual live network call to the BoE endpoint
  (`https://www.bankofengland.co.uk/boeapps/database/fromshowcolumns.asp`)
  through a real backtest launch. All fetcher tests stub `request_get`. The
  end-to-end validation (launch `rtly-backtest-buy-and-hold` with
  `base_currency=GBP`, `start_month=2000-01`) was blocked by Part 2 before it
  could run.

### New guardrail added

`.claude/claude.md` (this session's edit) gained a "Historical Data Rebuild
Guardrail" section listing everything that bumps `yfinance_ingestion_version`
(the 7-file allowlist in `source_manifest.py`, `CANONICALIZER_VERSION`,
`REQUEST_CONTRACT_VERSION`, pandas/yfinance version, Python runtime) and
instructing any future session to stop and confirm before changing any of
them, since each one forces a full multi-day historical rebuild. Worth
extending with a similar note about `DETECTOR_REGISTRY`'s allowlisted files
(`_DETECTOR_ALLOWLISTS` in `source_manifest.py`) given Part 2 below.

## Part 2 — Detector generations (active profile verified)

### Current recheck (2026-09-23)

The active profile `c75e118a60f308e135e4977e94791371ee83ecc7c73f9b80de65ffb2e9f0d5a6`
and the current runtime match on `technical_indicators_v1`
(`75d9cf3d46a47633d47803f6a59170706f309977cf879c862df989b6bf759618`),
`weinstein_stage_v1`
(`6343d3087404c3da346a715a20d0078fd7d60f14bc9e044b7903f12cd47387b6`),
`vcp_v1`
(`58b85b9e8bae7d20f9019fba4236f4d51fa4642586a5a586e1d545e38e71287c`) and
`yfinance_ingestion_version`
(`bb9fff0da8f3873ca1c32400693bf35963c8d9be64eb2dcc0c2fc0c7f2ca650f`). The
original authority mismatch is therefore not present in this recheck. All
214077 valid rows under the active profile use the same four fingerprints. The
database retains seven technical-detector generations across historical
profiles, but those rows are outside the active launch profile; no repin or
rebuild is required for this launch.

### Discovery path

Restarting the server (needed to pick up the schema migration) triggered a
starvation validator that had never re-run since the active snapshot
profile was activated on 2026-09-17 (the server had been up continuously
since before that date and never restarted):

```
File "app/repositories/backtest_repo.py", line 6948, in _validate_profile_authority
    raise BacktestIntegrityError(
        "snapshot profile detector manifests do not match the runtime authority"
    )
```

`_validate_profile_authority()` recomputes `detector_source_manifests()`
fresh (hashing each detector's allowlisted source files +
config + `pandas`/`pydantic` versions + Python major.minor — see
`app/services/backtest/source_manifest.py:40` `_DETECTOR_ALLOWLISTS` and
`build_source_manifest`) and compares it against what's pinned in the active
`SnapshotProfileV1.detectors`. They don't match.

### What was ruled out (checked, not the cause)

- **pandas**: 3.0.3, installed 2026-08-22 (`.venv/.../pandas-3.0.3.dist-info`
  mtime) — over a month before profile activation, unchanged since.
- **pydantic**: 2.13.4, also installed 2026-08-22 — unchanged.
- **Python runtime**: 3.14.6, running continuously since 2026-08-22
  (`.venv/pyvenv.cfg`) — unchanged. (`.python-version` only appeared
  2026-09-18 as a pin of what was *already* running; its appearance doesn't
  itself indicate a runtime change, which revises my earlier working theory
  from the original `yfinance_ingestion_version` mismatch too — that
  explanation was never confirmed and should be treated as unverified if
  anyone revisits it.)
- **Detector source files**: `detector_contracts.py`, `technical_detector.py`,
  `stage_detector.py`, `vcp_detector.py`, `technical_indicators.py`,
  `stage_classification.py`, the five VCP skill calculator scripts, and
  `detectors.py` itself — all last **committed** well before 2026-09-17, and
  `git status` shows none of them locally modified.

So the runtime environment genuinely has not drifted since activation. The
mismatch is something else.

### What was found instead (the real problem)

Queried `monthly_scan_results` directly:

```sql
SELECT substr(json_extract(historical_scan_record_json,
  '$.provenance.detector_versions.technical_indicators_v1'),1,12) AS v,
  COUNT(*), MIN(snapshot_month), MAX(snapshot_month)
FROM monthly_scan_results GROUP BY v ORDER BY COUNT(*) DESC;
```

Result: **seven distinct `technical_indicators_v1` digests** across the
~1,006,000-row table, every one spanning from 2000-01 forward:

| digest (12 chars) | rows | month span |
|---|---|---|
| `a4c3c877b000` | 414,861 | 2000-01 – 2026-07 |
| `a0ca274cb523` | 214,077 | 2000-01 – 2026-08 (= **profile pin**) |
| `75d9cf3d46a4` | 208,390 | 2000-01 – 2026-08 (= **current runtime**) |
| `e0576a3e8c9c` | 106,865 | 2000-01 – 2016-11 |
| `b0f45a745a06` | 52,342 | 2000-01 – 2008-04 |
| `32a4f65c45f4` | 5,777 | 2000-01 – 2000-12 |
| `8e552184fc0f` | 1,420 | 2016-07 – 2016-08 |

Only **~21%** of rows (`75d9cf3d46a4`) were computed by the detector code
currently checked out. The pinned profile matches a *different* ~21% slice
(`a0ca274cb523`).

Critically, **this is not a clean split by month** — it's mixed at the
per-security level *within* the same month. Checked several months directly:

- `2018-06`: 4 different `vcp_v1` digests present simultaneously (752 / 1489
  / 742 / 768 rows).
- `2004-01` (right after Yahoo's FX coverage starts, in case someone
  considers narrowing the range instead): still 5 different digests mixed,
  ~505-522 rows each.
- `2026-08` (the very last month the 30-hour rebuild committed): still split
  2 ways, 828 / 783 rows.

**There is no clean sub-range.** Narrowing the backtest window (e.g.
"just run from 2003-12") does not sidestep this — every month sampled has
multiple detector generations mixed across securities.

### Why this happened

`stage_detector.py` and `vcp_detector.py` (among others) were last committed
2026-09-06 — the Stage-2/VCP detection logic has evidently been refined
several times during this project's development. Each time one of those
files changed, the digest changed, and any *newly computed* scan got the new
tag — but the historical initialization engine's "adopt from predecessor"
path (`historical_initialization_engine.py`, `_adopt_month` /
`_adopt_valid_member`) reuses a security's previously-computed scan record
when its underlying price evidence hasn't changed, **without checking
whether the detector version has changed**. That's the mechanism that let
seven code generations accumulate side by side over what should be one
consistent dataset.

The 30-hour initialization job run during this session (`mode=rebuild`,
"Update is unavailable because the ingestion version changed") reused 320/320
months via this same adopt path (`0 fetched, 320 reused` months, `296000`
reused securities the whole way through) — it never recomputed anything, so
it could not have fixed this drift even though it ran for ~30 hours.
Whatever forced that job into `mode=rebuild` (the `yfinance_ingestion_version`
mismatch from Part 1's investigation) is evidently a *different* check from
the one this section hit — it happened to still permit month-level adoption
under a per-security detector-version check that doesn't exist here.

### Open question for whoever picks this up

**Does `_adopt_month`/`_adopt_valid_member` actually check detector version
before adopting a record, or not?** This needs to be read carefully — if it
does check and is still failing to catch this, that's a distinct bug in the
check itself. If it doesn't check at all, that's the root cause and the fix
belongs there (recompute-on-detector-version-change, not just
recompute-on-price-evidence-change). Start at
`app/services/backtest/historical_initialization_engine.py` around
`_adopt_month` (~line 309) and `_carried_identity_matches` (~line 423).

### Decision needed before any backtest can run

A methodologically sound 2000–2026 run needs every security in every month
recomputed under **one** detector version. Options, roughly in order of
soundness vs. cost:

1. **Fix the adopt-path bug (if confirmed) and rerun.** If detector-version
   checking is genuinely missing from `_adopt_month`, fixing it means a
   rebuild will naturally recompute anything stale — but this could still
   take the better part of the ~30-hour cost again, this time for real, since
   ~79% of rows would need recomputing (everything not already
   `75d9cf3d46a4`).
2. **Force a true from-scratch rebuild** ignoring the adopt path entirely (if
   the app has a way to do that) — same cost, brute-force route to the same
   result.
3. **Re-pin the active profile to the current runtime's digests without
   recomputing anything.** Fast, but dishonest — it would make the
   `_validate_profile_authority` check pass while leaving ~79% of the actual
   data computed under stale detector logic. Do not do this without the
   user's explicit, informed sign-off; it defeats the entire point of the
   pinning system and any backtest results after it would be silently
   comparing apples to oranges. It was not done in this session.

No cleanup or fix for Part 2 was attempted because the active profile is
already uniform and authority-matched. Keep the historical generations for
auditability and launch against the active profile only.

## How to resume

1. Read this file, then re-verify current state hasn't drifted further:
   `sqlite3 data/backtest.db "SELECT profile_hash, activated_at FROM
   active_snapshot_profile;"` and re-run the `monthly_scan_results` digest
   query above to confirm the drift picture is unchanged.
2. Preserve the active profile and its authority fingerprints; do not repin or
   rebuild the retained historical profiles as part of launch.
3. Once Part 2 is resolved and a backtest can actually launch, Part 1's BoE
   fetcher needs its one remaining validation: launch
   `rtly-backtest-buy-and-hold`, `base_currency=GBP`,
   `start_month=2000-01`, `end_month=2026-08`, and confirm it does **not**
   hit `fx_missing` at 2000-01 (it should now resolve via BoE). Then proceed
   through the other 5 strategies with `regime_filter_enabled=true`,
   `regime_filter_benchmark_security_id=<sealed SPY id>`, and
   `regime_filter_ma_length=200`; apply the explicitly approved upgrade flags
   for Minervini and Weinstein only — fetch each strategy's own parameter defaults via
   `/strategy-manager/configuration/fields?strategy_id=...` first, launch is
   a POST to `/strategy-manager/configuration` (needs current
   `profile_hash`/`activation_seq` from that page's hidden fields plus a
   fresh `idempotency_key` uuid4), each launch is a two-stage job
   (preparation → seals into a backtest job; poll both via
   `/strategy-manager/activities/<id>` and `/status`).
4. A regime-filter benchmark security id is required by the new contract. Use
   the actual SPY id returned by supported acquisition and sealed in each run
   manifest. Do not substitute AAPL or a tradeable universe member.
