---
baseline_commit: 4752418cdf8f940dcc4605d6e1709b401e8aeaba
github_issue: 188
---

# Story 1.5: Reconstruct and Cache Canonical Historical Scan Records

Status: done

## Story

As a portfolio owner,
I want supported scanner detectors replayed against bounded historical evidence,
so that a past monthly snapshot is reproducible without invoking network-bound skill CLIs or copying present-day enrichment backwards.

## Acceptance Criteria

1. Each supported detector is imported and called in-process against already-fetched, bounded historical evidence. Reconstruction performs no network access and invokes no `skills/*/scripts/*.py` CLI subprocess.
2. Weinstein Stage classification is implemented in dependency-free `app/core/stage_classification.py` and consumed by both Analyst and historical reconstruction. Neither Strategy nor reconstruction code imports `app/agents/`.
3. Every detector declares its required lookback in completed trading sessions. Reconstruction requests the maximum declared warm-up before the requested period; warm-up rows are calculation-only and can never become candidate or simulation dates.
4. `HistoricalScanRecordV1` is the sole typed serialization authority for the fields, types, units, nullability, enums, and canonical UTF-8 JSON of a reconstructed scan. Current-only and institutional fields are null only where `ReconstructabilityPolicyV1` explicitly permits it; live values are never copied backwards.
5. The exact cache key `(security_id, date, detector, detector_version, input_revision)` returns the immutable existing detector result. A detector-version or input-revision change is a cache miss. Conflicting bytes for an existing key raise a stable integrity failure rather than overwriting data.
6. Missing, ambiguous, insufficient, or out-of-bounds detector evidence yields a stable typed failure and no fabricated record. Any attempt by a detector to access evidence after its as-of date raises an out-of-bounds error.
7. Reconstruction emits one complete record for every valid roster member for which all required detector fragments can be produced, including securities for which `valid_vcp` is false. It does not reuse the live scanner's candidate filter.
8. Repeated and concurrent reconstruction with identical evidence, source, policy, and detector configuration produces byte-identical canonical records and one immutable cache row per detector key.

## Tasks / Subtasks

- [x] Establish the closed historical scan contract and reconstructability policy (AC: 4, 6, 7)
  - [x] Add `app/services/backtest/historical_scan_record.py` containing frozen, strict Pydantic v2 models with `extra='forbid'` and non-finite numbers rejected.
  - [x] Define `HistoricalScanRecordV1`, `ReconstructabilityPolicyV1`, detector-fragment envelopes, closed enums, canonical JSON serialization, digesting, and strict round-trip parsing in one module.
  - [x] Represent Decimal prices and ratios as canonical decimal strings in JSON; dates use ISO `YYYY-MM-DD`; timestamps use UTC ISO-8601; percentages are percentage points, not fractions.
  - [x] Classify every persisted field as reconstructed-required or policy-nullable. Fundamentals, institutional activity, Congress/Senate activity, current watch-list flags, and inferred relative strength are null in V1 because no point-in-time evidence contract exists for them.
  - [x] Include durable provenance: source `yfinance`, `universe_basis='captured_configured_roster'`, `roster_captured_at`, `point_in_time_universe=false`, `survivorship_bias='known'`, roster/evidence/calendar/alias revisions, detector versions, and `input_revision`.

- [x] Extract shared pure scanner calculations without changing live behaviour (AC: 1, 2, 3)
  - [x] Add dependency-free `app/core/stage_classification.py`; move the exact Analyst Stage and SMA-slope rules there and make `AnalystAgent` delegate to it.
  - [x] Add `app/core/technical_indicators.py`; extract the scanner's pure OHLCV technical calculation and preserve `ScannerAgent.compute_technicals` as a compatibility delegate.
  - [x] Add characterization/parity tests before extraction and retain existing Analyst/Scanner outputs, ordering, rounding, and null behaviour.
  - [x] Define a detector protocol with stable detector ID, algorithm/API version, canonical source-manifest `detector_version`, declared lookback sessions, and a pure `run` operation. `detector_version` is exclusively the SHA-256 source-manifest digest required by AD-5, not a human semantic version.
  - [x] Register V1 detectors in fixed order: `technical_indicators_v1`, `weinstein_stage_v1`, and `vcp_v1`. Invoke only pure calculator modules under `skills/vcp-screener/scripts/calculators/`; never import the network-bound screener entry point.
  - [x] Centralize conversion from oldest-first market planes to each detector's required order. Validate finite/range-safe values before any explicit Decimal-to-float adapter and reject required null OHLCV rather than coercing it.
  - [x] Keep separate compatibility and reconstruction adapters around shared pure calculations. The live compatibility delegate retains existing integer-volume/rounding outputs; reconstruction retains eight-place split-continuous Decimal volume. Update the pure VCP volume calculator to accept finite Decimal-compatible volume without `int()` truncation while preserving identical results for integral live inputs.
  - [x] Declare `required_history_sessions` individually and inclusively: `technical_indicators_v1=252`, `weinstein_stage_v1=252`, and `vcp_v1=252`. Each count includes `as_of_session_date` (251 preceding completed sessions plus the target); fewer than 252 valid bounded rows is `required_data_missing`. Expose the registry maximum without adding another 252 sessions.
  - [x] Do not infer historical relative-strength rank from current data. Pass no rank to VCP V1 and persist relative-strength fields as policy-approved null; adding benchmark evidence later requires a new detector version and input revision.

- [x] Build deterministic, bounded reconstruction (AC: 1, 3, 6, 7)
  - [x] Add `app/services/backtest/historical_scan_reconstruction.py` with an explicit input object containing roster identity, snapshot month, exchange-local as-of session date, exact `StoredHistoricalEvidence`, and revision metadata.
  - [x] Construct `HistoricalMarketPlanes.from_evidence(...)` internally and consume only its `split_continuous_as_of(date)` view; do not accept separately supplied planes, read raw provider rows, or use adjusted-close data directly.
  - [x] Treat cache `date` as the security's exchange-local `as_of_session_date`; keep `snapshot_month` as a separate record field.
  - [x] Exclude warm-up rows from output eligibility and assert all detector-visible rows are `<= as_of_session_date`.
  - [x] Produce every detector fragment, assemble them in the fixed registry order, validate the complete `HistoricalScanRecordV1`, and only then return a record.
  - [x] Apply the error mapping in Dev Notes. Preserve Story 1.4 `MarketDataPolicyError` codes unchanged and use architecture vocabulary for reconstruction/cache failures; immutable content conflicts are `integrity_error`.
  - [x] Ensure reconstruction has no imports from `app/agents`, portfolio/trading/order modules, or live price-cache modules.

- [x] Fingerprint source and inputs canonically (AC: 5, 8)
  - [x] Add `app/services/backtest/source_manifest.py` as the shared detector/strategy source-manifest implementation; reuse existing canonical manifest primitives where appropriate.
  - [x] Hash a sorted UTF-8 JSON manifest using POSIX paths and normalized line endings. Use the exact per-detector runtime allowlists in Dev Notes; do not recursively traverse imports. Exclude tests, caches, bytecode, logs, and generated artifacts.
  - [x] Define `ReconstructionInputManifestV1`; include security ID, snapshot month, exchange-local target session, exact evidence revision and request interval, market-plane policy version, alias revision, roster digest, calendar dataset digest, record/policy versions, and sorted detector identities/configuration.
  - [x] Use the canonical manifest digest as `input_revision`; test that semantically identical reordered inputs hash equally and any material input change hashes differently.

- [x] Persist immutable per-detector cache fragments (AC: 5, 8)
  - [x] Extend `app/repositories/backtest_repo.py` with a detector-cache table whose composite primary key is exactly `(security_id, date, detector, detector_version, input_revision)`.
  - [x] Store canonical detector-fragment JSON plus its repository-computed digest. Before insertion the repository parses the closed envelope and verifies all five envelope key fields equal the SQL key. Full records are assembled deterministically from fragments; Story 1.6 owns transactional monthly snapshot persistence.
  - [x] Implement compare-and-insert under `BEGIN IMMEDIATE`: insert-or-ignore, read the winner, return it when bytes/digest match, and raise `integrity_error` when they conflict.
  - [x] Add SQLite triggers rejecting UPDATE and DELETE, following the repository's existing immutable evidence patterns.

- [x] Verify determinism, isolation, and failure behaviour (AC: 1-8)
  - [x] Add fixed-fixture golden tests for complete canonical record bytes/digest, stage classifications, technical outputs, VCP true/false outcomes, null policy, date/units, and provenance warning facts.
  - [x] Add tests proving future rows cannot affect earlier results and direct out-of-bounds access fails.
  - [x] Add tests for missing/ambiguous evidence, insufficient lookback, null/zero volume distinction, non-finite values, malformed enums, unknown fields, and no partial record on any detector failure.
  - [x] Add cache hit/miss, immutable trigger, conflicting-content, process-reopen, and `ThreadPoolExecutor` concurrency tests. Concurrency uses independent SQLite connections, converges on one row, and proves rollback leaves no partial transaction.
  - [x] Block/monkeypatch network and subprocess entry points during reconstruction tests and assert they are never called.
  - [x] Run focused Backtest/Analyst/Scanner tests, then the complete repository test and quality suite.

### Review Findings

- [x] [Review][Patch] [High] Validate authoritative roster-member, MIC, and yfinance-ingestion provenance directly in Story 1.5 reconstruction inputs [app/services/backtest/historical_scan_reconstruction.py:55]
- [x] [Review][Patch] [High] Verify every manifest `detector_version` against the locally generated AD-5 source-manifest digest [app/services/backtest/historical_scan_reconstruction.py:279]
- [x] [Review][Patch] [High] Verify `StoredHistoricalEvidence` content/revision integrity and bind `provider_evidence_manifest_digest` to the evidence actually replayed [app/services/backtest/historical_scan_reconstruction.py:262]
- [x] [Review][Patch] [High] Validate the exact 252 exchange-calendar sessions and the claimed calendar digest, not only row count and final date [app/services/backtest/historical_scan_reconstruction.py:87]
- [x] [Review][Patch] [High] Isolate VCP Decimal arithmetic from the process-global Decimal context [skills/vcp-screener/scripts/calculators/volume_pattern_calculator.py:89]
- [x] [Review][Patch] [High] Reject missing or wrongly typed VCP calculator fields instead of defaulting/coercing them into fabricated valid output [app/services/backtest/detectors.py:281]
- [x] [Review][Patch] [High] Map unexpected detector adapter exceptions such as malformed contraction `AttributeError` to stable `integrity_error` [app/services/backtest/detectors.py:89]
- [x] [Review][Patch] [High] Make nested detector configurations, input identities, source manifests, and provenance version maps genuinely immutable [app/services/backtest/historical_scan_record.py:239]
- [x] [Review][Patch] [Medium] Preserve legacy integer live-volume outputs for integral inputs while retaining fractional reconstruction values [skills/vcp-screener/scripts/calculators/volume_pattern_calculator.py:194]
- [x] [Review][Patch] [Medium] Enforce canonical record cross-field and semantic invariants, including snapshot month, MIC/currency/unit compatibility, non-negative technicals, and bounded/ordered contraction dates [app/services/backtest/historical_scan_record.py:278]

## Dev Notes

### Contract Boundary

This story reconstructs and caches detector evidence only. Story 1.6 owns calendar targeting, monthly snapshot transactions, coverage/readiness, exclusions, and profile commits. Stories 1.7-1.9 own durable jobs and UI lifecycle; Story 2.3 owns the Strategy-facing bounded `MarketView`. Do not add routes, job orchestration, UI, snapshot commits, coverage extension, or BAU promotion here.

`HistoricalScanRecordV1` is not a reuse of permissive live `StockScan`. Its V1 wire schema is closed by the following exhaustive table. `decimal` means the canonical JSON string form defined below, not a JSON float. A path marked nullable is still required and serializes as explicit `null`.

| Path | JSON type | Null | Unit / closed values |
|---|---|---:|---|
| `schema_version` | string | no | literal `historical_scan_record.v1` |
| `security_id`, `observed_symbol`, `mic` | string | no | non-empty stable identity / observed alias / ISO 10383 MIC |
| `snapshot_month` | string | no | `YYYY-MM` |
| `as_of_session_date` | string | no | exchange-local `YYYY-MM-DD` |
| `currency` | string | no | `USD | GBP` |
| `quote_unit` | string | no | `USD | GBP | GBp` |
| `provenance_quality` | string | no | `best_effort_reconstructed | observed_bau` |
| `technicals.price`, `sma10`, `sma30`, `sma50`, `sma150`, `sma200`, `atr14`, `high_52w`, `low_52w`, `high_base`, `handle_low` | decimal | no | price in `quote_unit` |
| `technicals.volume`, `vol_ma50` | decimal | no | split-continuous shares, 8-place capable; zero is distinct from missing |
| `technicals.rsi14` | decimal | no | index points `[0,100]` |
| `technicals.rel_volume` | decimal | no | ratio |
| `technicals.pct_from_52w_high`, `pct_change_week` | decimal | no | percentage points |
| `stage.value` | string | no | `Stage 1 | Stage 2 | Stage 3 | Stage 4` |
| `vcp.valid_vcp` | boolean | no | pattern-calculator validation only |
| `vcp.score` | integer | no | `[0,100]` |
| `vcp.trend_template_score` | decimal | no | index points `[0,100]` |
| `vcp.trend_template_passed`, `vcp.wide_and_loose`, `vcp.breakout_volume_detected` | boolean | no | exact calculator outcomes |
| `vcp.num_contractions` | integer | no | `[0,4]`, equals `len(contractions)` |
| `vcp.contractions[]` | array of object | no | ordered `T1..T4`; may be empty |
| `vcp.contractions[].label` | string | no | `T1 | T2 | T3 | T4` |
| `vcp.contractions[].high_session`, `low_session` | string | no | `YYYY-MM-DD`, each `<= as_of_session_date` |
| `vcp.contractions[].high_price`, `low_price` | decimal | no | price in `quote_unit` |
| `vcp.contractions[].depth_pct` | decimal | no | percentage points |
| `vcp.contractions[].duration_sessions` | integer | no | non-negative completed-session count |
| `vcp.pivot_price`, `last_contraction_low` | decimal | yes | price in `quote_unit`; null when no contraction/pivot |
| `vcp.atr_compression_ratio`, `right_side_range_ratio`, `dry_up_ratio` | decimal | yes | ratios |
| `vcp.distance_from_pivot_pct` | decimal | yes | percentage points; negative means below pivot |
| `vcp.execution_state` | string | no | `Invalid | Damaged | Overextended | Extended | Early-post-breakout | Breakout | Pre-breakout` |
| `enrichment.sector`, `enrichment.observed_source` | string | yes | current/observed-only text |
| `enrichment.eps_growth`, `annual_eps_growth`, `roe`, `inst_ownership_pct`, `pe_ratio`, `rel_strength_vs_spy` | decimal | yes | growth/ownership/ROE are fractions; relative strength is percentage points |
| `enrichment.inst_count`, `funds_buying`, `funds_selling`, `funds_net`, `congress_buys`, `congress_sells`, `senate_buys`, `senate_sells` | integer | yes | observed counts |
| `enrichment.spy_uptrend`, `in_stocktwits`, `in_whale_wisdom` | boolean | yes | observed/current-only flags |
| `provenance.price_provider` | string | no | literal `yfinance` |
| `provenance.universe_basis` | string | no | literal `captured_configured_roster` |
| `provenance.roster_captured_at` | string | no | canonical UTC timestamp |
| `provenance.point_in_time_universe` | boolean | no | reconstruction emits `false` |
| `provenance.survivorship_bias` | string | no | `known | not_applicable`; reconstruction emits `known` |
| `provenance.renamed_or_delisted_may_be_absent` | boolean | no | reconstruction emits `true` |
| `provenance.historical_tradingview_screen_available` | boolean | no | reconstruction emits `false`; observed BAU may emit `true` only for an actually retained screen |
| `provenance.roster_digest`, `alias_revision`, `calendar_dataset_version`, `calendar_dataset_digest`, `provider_evidence_manifest_digest`, `provider_data_revision`, `provider_request_contract_version`, `yfinance_ingestion_version`, `input_revision` | string | no | exact non-empty revision/digest |
| `provenance.detector_versions` | object | no | exactly the three detector IDs mapped to their lowercase 64-hex AD-5 source-manifest digests; keys sorted canonically |

`ReconstructabilityPolicyV1` applies conditionally by `provenance_quality`. Reconstruction emits only `best_effort_reconstructed`: every `technicals`, `stage`, and `vcp` field is required/fatal; every `enrichment` field is required-null; and the provenance values in the table are enforced. The shared model also accepts `observed_bau`; for that producer the same technical/detector types apply, policy-approved enrichment may be populated or null, and point-in-time/survivorship/source-screen facts must describe the actually retained observation. This allows Story 1.10 to serialize through the contract without a schema redesign but does not authorize BAU production here.

Canonical serialization is application-owned: validate strict/frozen/forbid-extra models; retain every explicit null; normalize all strings to Unicode NFC; format dates/timestamps as above with UTC timestamps ending `Z`; reject NaN/infinity; serialize recursively sorted keys with `ensure_ascii=False`, separators `(',', ':')`, and no trailing newline; then UTF-8 encode and SHA-256 those exact bytes. Canonical decimal strings use finite `Decimal`, fixed-point notation, no exponent or leading `+`, no unnecessary leading/trailing zeroes, and normalize both positive and negative zero to `"0"`. Parse/serialize round trips must reproduce identical bytes.

`DetectorFragmentEnvelopeV1` uses those identical canonicalization rules and has exactly these required fields: `schema_version` (literal `scan_detector_fragment.v1`), `security_id` (non-empty string), `date` (exchange-local `YYYY-MM-DD`), `detector` (`technical_indicators_v1 | weinstein_stage_v1 | vcp_v1`), `detector_version` (lowercase 64-hex AD-5 source-manifest SHA-256), `detector_api_version` (non-empty algorithm/API version string), `input_revision` (lowercase 64-hex manifest SHA-256), and `result`. `result` is a detector-discriminated closed union: exactly `{ "technicals": <TechnicalsV1> }`, `{ "stage": <StageV1> }`, or `{ "vcp": <VcpV1> }`, matching `detector`; no failure envelope is cached. Envelope key fields are authoritative and must equal the five SQL key columns byte-for-byte after canonical parsing.

Do not persist a large price-history copy in each record. The immutable evidence revision is the audit source; detector fragments carry only outputs needed by downstream snapshot/backtest contracts.

The future UX will render provenance as “Best-effort yfinance” and warn “Survivorship-biased reconstruction; not a point-in-time market universe.” This story must preserve the structured facts needed for that exact presentation, but does not implement the UI.

### Detector Contracts and Composition

All detectors receive exactly 252 validated split-continuous rows, oldest first at the registry boundary, ending on `as_of_session_date`. The VCP adapter alone reverses a copy to newest-first because the existing pure calculators require that order.

- `technical_indicators_v1` calls the shared technical core and outputs only the complete `technicals` object. Missing OHLC or volume in any of the 252 required rows is `required_data_missing`; zero volume remains valid. Reconstruction uses Decimal-capable outputs without the live Scanner's integer truncation or display rounding.
- `weinstein_stage_v1` takes the canonical technical values plus weekly closes derived only from those 252 rows and outputs only `stage`. It preserves the current Analyst rules and closed Stage values.
- `vcp_v1` calls, in order, `calculate_trend_template(ohlcv, quote, rs_rank=None)`, `calculate_vcp_pattern(ohlcv)`, `calculate_volume_pattern(ohlcv, pivot_price, contractions)`, `calculate_pivot_proximity(...)`, then `compute_execution_state(...)`. `valid_vcp` is solely `calculate_vcp_pattern()['valid_vcp']`; trend-template failure does not rewrite it. `rs_rank=None` makes the relative-strength criterion fail conservatively and is not replaced by current data. A well-formed calculator result that finds no pattern/pivot is a valid `valid_vcp=false` fragment with nullable pivot-derived fields. A thrown exception, malformed result, non-finite output, or calculator-reported input/contract error despite 252 valid rows is `integrity_error` and prevents the complete record.

The VCP fragment contains exactly the `vcp` fields in the schema table. The adapter derives `duration_sessions=low_idx-high_idx`, resolves indices to bounded session dates, and drops free-text calculator details/reasons from the canonical record. No detector may write another detector's fragment.

### Error Mapping

| Condition | Stable outward code |
|---|---|
| Story 1.4 `MarketDataPolicyError` | propagate its actual `.code` unchanged; do not translate or invent aliases |
| absent/partial required interval or fewer than 252 valid rows | `required_data_missing` |
| roster/alias identity has multiple valid resolutions | `identity_ambiguous` |
| detector ID is not in the closed registry | `provider_contract_error` |
| detector exception/malformed/non-finite output | `integrity_error` |
| record violates reconstructability or closed schema | `integrity_error` |
| canonical serialization/round-trip failure | `integrity_error` |
| existing immutable cache key has different envelope/bytes/digest | `integrity_error` |

Failures are typed and include deterministic context (`security_id`, `as_of_session_date`, and detector where known), but volatile exception text is not part of the code or canonical record.

### Detector Source Manifests

Manifests list files explicitly; imported dependencies are not recursively traversed. The detector protocol, registry, adapters, and all default values live in one prescribed file: `app/services/backtest/detectors.py`. Every detector allowlist contains exactly these common paths:

- `app/services/backtest/source_manifest.py`
- `app/services/backtest/historical_scan_record.py`
- `app/services/backtest/historical_scan_reconstruction.py`
- `app/services/backtest/detectors.py`

It then adds exactly:

- `technical_indicators_v1`: `app/core/technical_indicators.py`;
- `weinstein_stage_v1`: `app/core/stage_classification.py` and `app/core/technical_indicators.py`;
- `vcp_v1`: `skills/vcp-screener/scripts/calculators/execution_state.py`, `skills/vcp-screener/scripts/calculators/pivot_proximity_calculator.py`, `skills/vcp-screener/scripts/calculators/trend_template_calculator.py`, `skills/vcp-screener/scripts/calculators/vcp_pattern_calculator.py`, and `skills/vcp-screener/scripts/calculators/volume_pattern_calculator.py`.

Paths are repository-relative POSIX paths. Each entry contains path and SHA-256 of normalized source bytes; the manifest includes detector ID, detector API version, Python runtime major/minor, relevant locked dependency versions, and explicit default/config values. Missing or extra allowlisted files fail manifest construction. Tests prove changes to each shared/runtime file or default alter only the applicable detector digest, while tests/generated files do not.

### Architecture Guardrails

- Story 1.4 is a hard prerequisite. Preserve its strict request contract, canonical float handling, OHLC geometry validation, Decimal isolation, and distinction between missing and zero volume.
- Detectors receive bounded split-continuous-as-of planes only. Provider-native and as-traded planes remain available for audit/simulation but are not interchangeable detector inputs.
- There is one result per roster security, not merely one live scanner candidate. `valid_vcp=false` is valid historical output.
- No live portfolio, `TraderAgent`, orders, `price_cache`, network client, current enrichment, or present-day rank may enter reconstruction.
- Source and input manifests are replay identity. Changes to executable detector source/config must invalidate the cache without tests or generated files causing spurious misses.
- SQLite UPSERT does not itself prove equal content under a colliding key; always compare the stored canonical bytes/digest after insert-or-ignore. Use an explicit write transaction for deterministic concurrent convergence.

### Project Structure Notes

Expected additions:

- `app/core/stage_classification.py`
- `app/core/technical_indicators.py`
- `app/services/backtest/source_manifest.py`
- `app/services/backtest/historical_scan_record.py`
- `app/services/backtest/historical_scan_reconstruction.py`
- focused tests under `tests/backtest/`

Expected modifications:

- `app/agents/analyst/analyst_agent.py` and its tests, only to delegate shared Stage logic;
- `app/agents/scanner/scanner_agent.py` and its tests, only to delegate shared technical calculations;
- `skills/vcp-screener/scripts/calculators/volume_pattern_calculator.py` and focused tests, only to remove lossy volume coercion while preserving integral-input compatibility;
- `app/repositories/backtest_repo.py` for immutable detector-cache persistence.

Avoid modifying VCP network/screener orchestration. Import only its pure calculator modules or move genuinely shared pure calculations behind a dependency-neutral core adapter while retaining compatibility.

### Previous Story Intelligence

- Story 1.4 supplies `HistoricalMarketPlanes.from_evidence(exact StoredHistoricalEvidence)` and `split_continuous_as_of(D)` with oldest-first, typed Decimal rows.
- It deliberately leaves missing volume as `None` and zero volume as zero, raises stable `MarketDataPolicyError`, and rejects future or malformed evidence.
- Commit `4752418cdf8f940dcc4605d6e1709b401e8aeaba` contains the reviewed Story 1.4 prerequisite. Implement Story 1.5 from that baseline and keep unrelated `AGENTS.md` untouched.

### Library and Platform Notes

- The repository's locked Pydantic v2 supports strict/frozen models, forbidden extras, and non-finite-number rejection through `ConfigDict`; use repository-locked versions rather than introducing upgrades.
- SQLite `ON CONFLICT` acts per uniqueness constraint, while `BEGIN IMMEDIATE` starts the write transaction immediately. The repository pattern still requires reading and comparing the winner to detect key/content conflicts.
- Keep canonical serialization application-owned; do not rely on incidental dictionary, filesystem, locale, float, or platform ordering.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Epic 1, Story 1.5]
- [Source: `_bmad-output/planning-artifacts/prds/prd-Agents.stocks-2026-08-09/prd.md` — historical initialization, reproducibility, and provenance requirements]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-1, AD-2, AD-5, AD-18, AD-20]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/DESIGN.md` — initialization and provenance presentation]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/EXPERIENCE.md` — resumable initialization experience and historical-only constraints]
- [Source: `_bmad-output/implementation-artifacts/1-4-produce-deterministic-price-volume-corporate-action-and-fx-planes.md` — market-plane APIs and prior-story constraints]
- [Source: `app/agents/analyst/analyst_agent.py` — current Stage and VCP composition behaviour]
- [Source: `app/agents/scanner/scanner_agent.py` — current technical calculation behaviour]
- [Source: `app/repositories/backtest_repo.py` — repository migrations and immutability patterns]
- [SQLite UPSERT](https://sqlite.org/lang_upsert.html)
- [SQLite Transactions](https://sqlite.org/lang_transaction.html)
- [Pydantic ConfigDict](https://docs.pydantic.dev/latest/api/config/)

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- RED: `tests/backtest/test_historical_scan_record.py` initially failed because the contract module did not exist.
- GREEN: 8 contract tests passed; full regression suite passed with 829 tests.
- RED/GREEN: shared-core imports initially failed; 70 focused Detector/Scanner/Analyst tests and 834 full-suite tests then passed.
- RED/GREEN: bounded reconstruction initially had no module; 7 focused reconstruction tests and 841 full-suite tests passed.
- RED/GREEN: source/input manifest imports initially failed; 14 focused identity/reconstruction tests and 848 full-suite tests passed.
- RED/GREEN: cache repository symbols initially did not exist; 12 focused repository tests and 853 full-suite tests passed.
- GREEN: exact cache reuse and concurrent complete reconstruction tests passed after integrating the repository cache into reconstruction.
- FINAL: 104 focused Backtest/Scanner/Analyst tests and all 860 repository tests passed; touched-file Ruff, formatting, Pyrefly, and `git diff --check` passed.
- REVIEW GREEN: all 10 adversarial findings were patched; 119 focused Story/Scanner/Analyst tests, all 226 VCP skill tests, and all 875 repository tests passed.

### Implementation Plan

- Build the closed canonical models first, then extract shared calculation cores behind compatibility delegates.
- Add bounded detector orchestration and canonical source/input identities before persistence.
- Finish with compare-and-insert cache concurrency, isolation checks, and full repository quality gates.

### Completion Notes List

- Implemented the strict `HistoricalScanRecordV1`, reconstructability policy, canonical Decimal/UTF-8 serialization, provenance, and detector-fragment contracts.
- Extracted live-compatible technicals and dependency-neutral Stage logic; registered bounded in-process historical technical, Stage, and VCP detectors with Decimal-capable volume.
- Added all-or-nothing bounded reconstruction from exact Story 1.4 evidence with stable identity/data failures and fixed-order fragment assembly.
- Added exact source allowlist hashing and a sorted reconstruction input manifest whose digest is the sole `input_revision` authority.
- Added immutable per-detector cache persistence with exact-key verification, repository-owned digests, process-reopen reads, and concurrent convergence.
- Bound each manifest detector API/configuration to the closed runtime registry, preventing a cache identity from describing configuration that was not executed.
- Verified all acceptance criteria and moved the story to review. Repository-wide Ruff and Pyrefly still report only pre-existing findings outside Story 1.5's touched files.
- Hardened review boundaries with authoritative roster/ingestion identity, evidence and calendar verification, runtime source-digest binding, strict VCP adapter validation, fixed Decimal context, deep immutability, and canonical semantic invariants.
- Preserved legacy integral live-volume behavior while retaining fractional reconstructed volume; all review findings are resolved and the story is done.

### File List

- app/services/backtest/historical_scan_record.py
- app/services/backtest/historical_scan_reconstruction.py
- app/services/backtest/source_manifest.py
- app/repositories/backtest_repo.py
- app/core/stage_classification.py
- app/core/technical_indicators.py
- app/services/backtest/detectors.py
- app/agents/analyst/analyst_agent.py
- app/agents/scanner/scanner_agent.py
- skills/vcp-screener/scripts/calculators/volume_pattern_calculator.py
- tests/backtest/test_detector_cores.py
- tests/backtest/fixtures/historical_scan_record_v1.json
- tests/backtest/test_historical_scan_record.py
- tests/backtest/test_historical_scan_reconstruction.py
- tests/backtest/test_source_manifest.py
- tests/backtest/test_scan_reconstruction_cache.py

### Change Log

- 2026-08-11: Implemented deterministic canonical historical scan reconstruction, source/input fingerprinting, immutable detector-fragment caching, compatibility delegates, and complete isolation/concurrency coverage; moved Story 1.5 to review.
- 2026-08-11: Applied all BMAD adversarial review patches, completed full regression verification, and moved Story 1.5 to done.
