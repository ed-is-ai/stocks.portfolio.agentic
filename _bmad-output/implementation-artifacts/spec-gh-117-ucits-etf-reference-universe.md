---
title: 'Tradable UK UCITS ETF reference universe for asset-allocation backtests'
type: 'feature'
created: '2026-10-18'
status: 'done'
final_revision: '07fdcb669b7abc45c036e3863900721f495984fc'
baseline_revision: '85f6403bbe55157b40615aed33057050f1aeddb9'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/.claude/CLAUDE.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The backtest universe holds only scanner stocks. SPY is registered only as a non-tradable regime benchmark. So no asset-allocation strategy (Easy Rider, Momentum Rider) has pinned equity, bond, commodity, gold or cash ETF evidence to build on (#117).

**Approach:**
- Add a versioned, content-hashed reference list, `config/etf_universe.ucits.v1.json`. Every symbol in it was checked against yfinance for listing, GBP line and inception.
- Add an acquisition service that fetches XLON session prices for each entry from its verified inception, and reports failures per symbol. Store the evidence under `etf:<SYMBOL>` ids, separately from the scanner roster.
- Add an optional BTC-GBP sleeve. It is aligned to LSE session closes with no look-ahead.
- Strategies opt in with an `etf_universe_version` parameter. Launch then seals the list digest and each symbol's evidence revision into the run manifest parameters, following the sector pin pattern.

## Boundaries & Constraints

**Always:**
- Leave the 7 guarded files, `CANONICALIZER_VERSION`, `REQUEST_CONTRACT_VERSION` and the pandas/yfinance/python pins untouched. `yfinance_ingestion_version` stays `77e1d3f58e12efa86f1f633f9d2a163564d4cbf53fb3b5b1a65dd5cea328ec1e`.
- Missing sessions are reported as incomplete months. They are never filled forward.
- Tests use temp DBs only.

**Block If:** the work would require editing a guarded file or migrating the existing reference or security identity tables.

**Never:**
- Add the ETFs to `security_identities` or to snapshot profiles. Scanner runs and profiles must stay unchanged.
- Use US-listed ETFs.
- Use intraday BTC data. yfinance limits hourly history to 730 days.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Acquire happy | Provider returns every XLON session from inception | One outcome per symbol, carrying a revision and no incomplete months | None |
| Provider failure | One symbol raises | That symbol's outcome carries the error; the other symbols still acquire | Per-symbol, no abort |
| Late inception | First provider row is after the pinned inception | Outcome error `inception mismatch` | Per-symbol |
| Gap | A month is missing one XLON session | `incomplete_months` lists that month | Launch rejects a run covering it |
| BTC alignment | UTC daily bars | The close for session d is the bar dated d-1. That bar closes at 00:00 UTC on d, which is before 16:30 London on d. A missing bar d-1 leaves session d absent | Reported as incomplete |
| Opt-in launch | Strategy declares `etf_universe_version` | Parameters gain `etf_universe_digest` and `etf_evidence_revisions` | Any symbol not acquired, or incomplete in a run month, becomes a form error |
| Stray pin | Pin keys present without `etf_universe_version` | Manifest validation raises `ValueError` | None |

</intent-contract>

## Code Map

- `app/services/backtest/sector_reference.py` -- the pattern to follow: load, digest, build pin, validate pin
- `app/services/backtest/benchmark_evidence.py` -- the pattern to follow for acquisition (adapter, commit, verify)
- `app/services/backtest/backtest_launch_service.py` -- `_bind_sector_pin` (~632) and host param stripping (~426)
- `app/services/backtest/run_input_manifest.py` -- `_v2_consistency` (~448); host param pop (~773)
- `app/services/backtest/worker.py` -- strips host params from generic parameters (~406)
- `app/repositories/historical_price_repo.py` -- `commit`, `verify`, `covering_revision`
- `app/services/backtest/trading_calendar.py` -- `sessions_in_range("XLON", ...)`

## Tasks & Acceptance

**Execution:**
- [x] `config/etf_universe.ucits.v1.json` -- add `{version, entries:[{symbol, role, asset_class, currency, quote_unit, exchange_timezone, inception, optional}]}` with the verified table (see Design Notes) -- this is the pinned list
- [x] `app/services/backtest/etf_universe.py`
  - Add `load_etf_universe` (SHA-256 of the file bytes) and `incomplete_months`.
  - Add `align_btc_to_sessions` and `EtfUniverseEvidenceService.acquire`, which returns per-symbol outcomes.
  - Add `resolve_etf_universe_pin` (offline, via `covering_revision`, with a completeness check for the run months) and `validate_etf_universe_pin`.
  - Export the constant `ETF_UNIVERSE_HOST_PARAMS`.
  - Rationale: one authority for the ETF reference.
- [x] `app/services/backtest/backtest_launch_service.py`
  - Strip `ETF_UNIVERSE_HOST_PARAMS` from submitted parameters.
  - Add `_bind_etf_universe_pin` beside `_bind_sector_pin`.
  - Rationale: the opt-in seals into the manifest.
- [x] `app/services/backtest/run_input_manifest.py` -- pop the ETF host params before strict validation, and call `validate_etf_universe_pin` in `_v2_consistency` -- the manifest rejects malformed pins
- [x] `app/services/backtest/worker.py` -- strip the ETF host params from the generic parameters -- same as the sector pin
- [x] `app/cli/acquire_etf_universe.py` -- run acquisition against `HISTORICAL_PRICE_CACHE` and print the per-symbol outcomes -- the operator entry point
- [x] `tests/backtest/test_etf_universe.py`
  - Cover the I/O matrix: load/digest change, acquire happy path, per-symbol failure, inception mismatch, gap month, BTC alignment, pin resolution, launch binding and validation.
  - Add a guard that `yfinance_ingestion_version` is unchanged. The test asserts that the ETF modules stay outside the ingestion allowlist. The digest itself was checked by command before and after the change, and both gave `77e1d3f5…`. A hardcoded-digest test would fail on legitimate changes the user has approved.

**Acceptance Criteria:**
- Given the pinned list, when evidence is acquired, then each symbol has sessions from its verified inception, and failures are reported per symbol.
- Given a run on the ETF universe, then the manifest parameters seal the list digest, and changing the file changes the digest.
- Given existing scanner runs, then their manifests and profiles are unchanged, and `yfinance_ingestion_version` is unchanged.

## Spec Change Log

## Review Triage Log

### 2026-10-18 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6 (high 0, medium 1, low 5)
- defer: 2 (high 0, medium 1, low 1)
- reject: 14 (high 0, medium 0, low 14)
- addressed_findings:
  - `[medium]` `[patch]` Launch crashed on integrity or missing-evidence errors from `verify`. Pin resolution now reports them per symbol as form errors (test added).
  - `[low]` `[patch]` Empty provider rows raised an opaque IndexError. They now report `no rows returned`.
  - `[low]` `[patch]` An `end` on or before inception now reports `end precedes inception` (test added).
  - `[low]` `[patch]` `inception mismatch` now names the first session the provider returned.
  - `[low]` `[patch]` `validate_etf_universe_pin` now rejects BTC-GBP evidence unless `bitcoin_sleeve_enabled` is true (test added).
  - `[low]` `[patch]` The CLI exits 1 when any symbol fails. The duplicated "unusable/unreadable" error prefix was removed.

## Design Notes

These are the verified listings. Evidence comes from yfinance `history(period="max")` on 2026-10-18, compared against the XLON calendar in `TradingCalendar`. Inception is the first session in the data.

| Role | Symbol | Unit | Inception | Gaps (missing XLON sessions) | Replaces |
|---|---|---|---|---|---|
| US equity | VUSA.L | GBP | 2012-05-22 | 3 | -- |
| Developed ex-US | XMWX.L | GBP | 2024-08-27 | 1 | to-research (only GBP world-ex-USA line found) |
| Developed Europe ex-UK (long-history proxy) | VERX.L | GBP | 2014-09-30 | 2 | -- |
| Japan (long-history proxy) | VJPN.L | GBP | 2013-05-21 | 2 | -- |
| Emerging markets | VFEM.L | GBP | 2012-05-22 | 3 | -- |
| US long Treasuries | IBTL.L | GBp | 2015-01-21 | 1 | IDTL.L (the USD line of the same fund) |
| Global government bonds | SGLO.L | GBP | 2009-03-06 | 2 | IGLO.L (the USD line of the same fund) |
| Commodities | CMFP.L | GBp | 2010-03-15 | 2 | CMOD.L (USD; its GBp line CMOP.L only starts in 2017) |
| Gold | SGLN.L | GBp | 2011-04-08 | 1 | -- |
| Aggregate bonds (GBP hedged) | AGBP.L | GBP | 2017-11-21 | 0 | VAGP.L (2019-06-18) |
| Cash proxy | ERNS.L | GBP | 2013-10-16 | 0 | -- |
| Bitcoin (optional) | BTC-GBP | GBP (UTC) | 2014-09-17 | 0 after alignment | -- |

Notes on the table:
- These candidates were rejected: EXUS.L (USD), IWXU.L, XDWX.L and WXUS.L (not found), and XWXU.L (delisted in 2015).
- Every chosen line is GBP-denominated, so GBP valuation needs no FX. USD-base runs keep the existing BoE path.
- The engine does not yet trade pinned ETFs. A run must still carry a prepared universe. Trading the pinned evidence needs a new manifest version and an engine price path, which belongs to the Easy Rider strategy work.

## Verification

**Commands:**
- `uv run pytest tests/backtest -q` -- expected: pass
- `uv run pyrefly check` -- expected: no new errors
- `uv run ruff check <changed files>` and `uv run ruff format <changed files>` -- expected: clean

## Auto Run Result

Status: done.

**Summary.** This change adds a pinned, content-hashed UK UCITS ETF reference universe (11 ETFs plus an optional BTC-GBP sleeve). It includes:
- per-symbol acquisition from each ETF's verified inception, stored as `etf:<SYMBOL>` evidence and kept out of the scanner roster;
- incomplete-month reporting with no forward fill;
- BTC-GBP aligned to LSE closes using the prior UTC bar, so there is no look-ahead;
- a launch-time opt-in (`etf_universe_version`) that seals the list digest and per-symbol revisions into the run manifest parameters.

**Files changed:**
- `config/etf_universe.ucits.v1.json`: the pinned list, verified against yfinance.
- `app/services/backtest/etf_universe.py`: load/digest, acquisition, completeness, BTC alignment, pin resolve/validate.
- `app/services/backtest/backtest_launch_service.py`: strips the ETF host params and adds `_bind_etf_universe_pin`.
- `app/services/backtest/run_input_manifest.py`: pops the ETF host params; `_v2_consistency` validates the ETF pin.
- `app/services/backtest/worker.py`: strips the ETF host params from the generic parameters.
- `app/cli/acquire_etf_universe.py`: operator CLI that prints per-symbol outcomes; exits 1 on any failure.
- `tests/backtest/test_etf_universe.py`: 30 tests.

**Review.** 6 findings patched, 2 deferred to `deferred-work.md`, 14 rejected:
- Deferred: engine trading of the pinned ETFs; and the newest-covering-revision limitation.
- Rejected: design choices that are documented, such as the raw-bytes digest and the sealed pin as authority. Also rejected: style or speculative items.

**Verification.**
- `uv run pytest tests/backtest -q`: 1639 passed.
- `uv run pyrefly check`: 106 errors, unchanged from baseline.
- `ruff check` and `ruff format` on the changed files: clean.
- Digests, compared between baseline `85f6403` and this change:
  - `yfinance_ingestion_version`: `77e1d3f58e12efa86f1f633f9d2a163564d4cbf53fb3b5b1a65dd5cea328ec1e` both before and after.
  - `execution_contract_digest`: `ec9d0c708c6eab62c75d5fb45b05835725c8e261ff324330d96cd4775d334369` both before and after.

**Residual risks:**
- yfinance LSE data has a few missing sessions (for example 2026-03 on most lines). Runs covering those months are rejected at launch until the data is re-acquired.
- XMWX.L (the true developed ex-US line) only starts in 2024-08. Before that date, VERX.L and VJPN.L are the long-history proxies.
