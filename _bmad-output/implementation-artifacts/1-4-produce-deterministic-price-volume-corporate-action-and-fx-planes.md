---
baseline_commit: 43bea299129383f3b584858e60f1a5a4ae4f2122
github_issue: 187
---

# Story 1.4: Produce Deterministic Price, Volume, Corporate-Action, and FX Planes

Status: done

## Story

As a portfolio owner,
I want one deterministic interpretation of yfinance prices, volume, actions, and currencies,
so that splits, dividends, indicators, fills, and mixed-market valuations are not double-counted or implementation-dependent.

## Acceptance Criteria

1. **As-traded plane:** Given exact provider-native price/action evidence, when the as-traded plane is derived, then every provider-applied retroactive split factor after each row through that evidence revision's exclusive-end cutoff is reversed deterministically, `Adj Close` and dividend adjustments are never used, and the resulting typed accessor is the only price contract intended for fills and portfolio valuation.
2. **Bounded indicator plane:** Given simulated session `D`, when indicator history is requested, then `split_continuous_as_of_D` derives from as-traded OHLC using only splits effective in `(row_session, D]`; rows/actions after `D` are inaccessible, and dividends never alter indicator prices.
3. **Volume plane:** Given provider-native session volume, when split-continuous volume is calculated, then prior volume is multiplied by split ratios effective in `(row_session, D]` using `Decimal` arithmetic quantized to eight places with `ROUND_HALF_EVEN`; zero remains zero, missing remains null, and the accessor—not direct stored-volume reads—is the contract for Story 1.5 detectors.
4. **Exactly-once splits:** Given an ordinary or reverse split effective on session `D`, when pure accounting policy is applied before `D` signals, then shares are multiplied and per-share basis is divided by the positive split ratio exactly once, position value is unchanged at the matching as-traded prices, and deterministic ledger provenance identifies the action session, ratio, evidence revision, and policy version.
5. **DividendCashPolicyV1:** Given a yfinance dividend event on session `D`, when `DividendCashPolicyV1` is applied before `D` signals, then only the explicitly supplied shares carried into `D` receive one `shares × dividend_per_share` cash credit, quantized to eight places, and provenance states that the provider event date approximates both entitlement and payment date. Same-session buys are never eligible.
6. **Quote units and FX:** Given USD, GBP, or GBp quote evidence and a GBP/USD base-currency conversion, when FX is required, then GBp first scales by `0.01 GBP`; only an exact immutable `GBPUSD=X` revision with USD-per-GBP orientation is accepted; GBP-to-USD multiplies and USD-to-GBP divides using the latest explicitly completed FX session no more than five calendar days old.
7. **Deterministic failures and rounding:** Given a ledger conversion, dividend credit, or valuation, when its base-currency amount is produced, then provider inputs enter through `Decimal(str(value))`, stored amounts are quantized to `0.00000001` with `ROUND_HALF_EVEN`, and missing/stale/non-positive/ambiguous FX, unsupported currency/unit, malformed evidence, or unsupported action raises a typed visible failure rather than guessing, refetching, or falling back.

## Tasks / Subtasks

- [x] Gate 1 — Implement bounded price and volume planes (AC: 1–3)
  - [x] Add one pure typed market-plane module under `app/services/backtest/` that consumes `StoredHistoricalEvidence` returned by an exact `HistoricalPriceRepository` read. It must not query yfinance, choose a latest revision, merge intervals, mutate evidence, or use `Adj Close`.
  - [x] Decode Story 1.3's finite IEEE-754 hex values back to provider numbers, then enter arithmetic as `Decimal(str(float_value))`. Reject malformed/non-finite values, invalid session order, duplicate/conflicting actions, non-positive split ratios, and actions outside the exact revision bounds with stable typed failures.
  - [x] Derive `as_traded` OHLC by reversing the product of all later split ratios through the evidence cutoff. Derive `split_continuous_as_of_D` from as-traded OHLC by dividing rows before each split effective by `D`. Never use future actions in the bounded indicator view and never apply dividends to either plane.
  - [x] Preserve provider-native/as-traded share-count volume, derive split-continuous volume with the architecture's multiplication direction and `(row_session, D]` boundary, and retain zero/null semantics. Expose named accessors so Story 1.5 does not read storage rows directly.
- [x] Gate 2 — Implement exactly-once corporate-action policies (AC: 4–5, 7)
  - [x] Add pure immutable position/action result contracts and versioned split policy. Apply positive ordinary/reverse ratios before signals, multiply carried shares, inversely adjust per-share basis, quantize ledger values, and emit evidence-rich provenance without coupling to the future Backtest Engine.
  - [x] Implement `DividendCashPolicyV1` as the sole dividend cash owner. Require carried-at-open shares as input, credit once before signals, preserve action/evidence/quote-unit provenance, and explicitly exclude same-session buys by API shape and tests.
  - [x] Reject unknown actions as `unsupported_corporate_action`; do not invent merger, spin-off, fractional-share, cash-in-lieu, withholding-tax, or payment-date behavior. Story 2.4 will orchestrate these pure policies and persist Trade Log events.
- [x] Gate 3 — Implement quote-unit normalization and immutable FX conversion (AC: 5–7)
  - [x] Add a closed USD/GBP/GBp money/quote-unit contract. Apply unit scale before FX, preserve source currency/unit, and quantize every ledger/dividend/valuation result to eight base-currency places with `ROUND_HALF_EVEN`.
  - [x] Build a cache-only FX accessor over exact `StoredHistoricalEvidence`. Require requested and observed symbol `GBPUSD=X`, provider `yfinance`, currency/unit `USD`, scale `1`, and unambiguous ordered closes. The caller supplies the latest FX session known complete at the fill/valuation instant; the accessor must never infer completion from wall-clock time or expose a later row.
  - [x] Select the newest close on or before that completion bound, reject age greater than five calendar days and non-positive/malformed rates, multiply GBP→USD, divide USD→GBP, and bypass FX only after valid same-currency unit normalization.
  - [x] Return typed stable codes/details for `evidence_missing`, `integrity_error`, `fx_missing`, `fx_stale`, `fx_ambiguous`, `unsupported_quote_unit`, and `unsupported_currency`. Never fetch, repair, use inverse symbols, or use an alternate provider.
- [x] Gate 4 — Qualification fixtures and regression proof (AC: 1–7)
  - [x] Align ordinary/reverse-split fixture inputs with the architecture's provider-native split-adjustment semantics while retaining pinned deterministic fixture digests, then assert as-traded price/share value continuity and split-continuous price/volume continuity in both directions.
  - [x] Add focused tests for cutoff and split-session boundaries, multiple splits, future-action exclusion, OHLC direction, `Adj Close`/dividend non-use, zero/null volume, malformed hex/actions, deterministic repeated derivation, and exact revision isolation.
  - [x] Add accounting tests for carried shares, same-session buy exclusion, once-only dividend credit, ordinary/reverse splits, basis/value continuity, quantization ties, provenance, and unsupported actions.
  - [x] Add FX tests for USD/GBP/GBp same- and cross-currency paths, orientation, exact five-day carry acceptance/six-day rejection, weekends, completion bounds, missing/ambiguous/non-positive evidence, unsupported units, and eight-place `ROUND_HALF_EVEN` results.
  - [x] Run Story 1.1–1.3 focused suites, the full repository suite, Ruff check/format, and Pyrefly. No network-dependent test may be added.

### Review Findings

- [x] [Review][Patch] Quantize quote-unit normalization only after FX conversion to avoid double rounding [app/services/backtest/currency.py:52]
- [x] [Review][Patch] Require the supplied FX completion bound to fall inside the exact evidence revision interval [app/services/backtest/currency.py:122]
- [x] [Review][Patch] Validate the immutable request-contract version and mandatory provider-native request flags on market and FX evidence [app/services/backtest/market_planes.py:114]
- [x] [Review][Patch] Reject non-canonical float-hex encodings and translate overflow into a stable typed integrity failure [app/services/backtest/market_planes.py:50]
- [x] [Review][Patch] Isolate Decimal arithmetic from ambient rounding modes and traps across market, corporate-action, and FX calculations [app/services/backtest/market_planes.py:76]
- [x] [Review][Patch] Translate malformed or non-finite quote-unit scales into stable typed failures [app/services/backtest/market_planes.py:114]
- [x] [Review][Patch] Classify unsupported source currencies as unsupported_currency rather than unsupported_quote_unit [app/services/backtest/currency.py:52]
- [x] [Review][Patch] Reject non-positive or internally inconsistent provider-native OHLC rows [app/services/backtest/market_planes.py:187]
- [x] [Review][Patch] Make provider-native, as-traded, and split-continuous price-plane row contracts non-interchangeable by type [app/services/backtest/market_planes.py:81]
- [x] [Review][Patch] Add integrated ordinary and reverse-split tests proving shares multiplied by matching as-traded price preserve market value [tests/backtest/test_market_planes.py:1]
- [x] [Review][Patch] Add explicit regression tests for dividend non-use, independent OHLC transformation, and exact half-even dividend ties [tests/backtest/test_market_planes.py:1]

## Dev Notes

### Scope and consumer ownership

Story 1.4 is a pure deterministic interpretation layer over Story 1.3's exact immutable revisions. It does not build `MarketView`, reconstruction, snapshots, jobs, the simulation loop, metrics, routes, or UI. Story 1.5 consumes split-continuous price/volume accessors for detectors; Stories 2.3–2.4 bind the accessors and corporate-action/FX policies into bounded views and the engine. This story must still make misuse difficult through explicit names and typed contracts.

The architecture's three price planes are non-interchangeable:

- `provider_native`: retained evidence only; never a fill, valuation, or detector API.
- `as_traded`: provider retroactive split normalization reversed through the pinned revision cutoff; future split knowledge is used only inside normalization and is not exposed as an event.
- `split_continuous_as_of_D`: as-traded history transformed with splits effective by `D`; detectors consume this plane; dividends never participate.

Do not derive any plane from `Adj Close`. It can contain dividend adjustment, and `DividendCashPolicyV1` is the sole owner of dividend economics.

### Existing code to extend and preserve

- `app/services/backtest/historical_price_evidence.py` owns the closed yfinance request and canonical provider-native encoding. Preserve request kwargs, normalization, retry classification, qualification digest compatibility, quote-unit metadata, and evidence immutability. Add only a shared public decode helper if it avoids duplicate hex validation.
- `app/repositories/historical_price_repo.py` owns exact immutable persistence. Consume `StoredHistoricalEvidence` via `get`/`get_exact`/`verify`; do not add overlap/latest selection or derived-plane persistence. Preserve SQL triggers, exact-reference failures, and digest verification.
- `tests/backtest/fixtures/market_mechanics_v1.json` is the deterministic qualification catalog. Any split-fixture correction requires recalculating and pinning its content digest through the existing canonical adapter; do not hand-edit digests without a test-derived value.
- `tests/backtest/test_market_mechanics_fixtures.py` currently verifies catalog shape and qualification. Extend it or add focused plane/accounting/FX files under `tests/backtest/`; do not turn qualification into a live-network suite.

### Numeric and session rules

- Use stdlib `Decimal`; do not add a money or FX dependency. Construct provider values through `Decimal(str(value))`, never `Decimal(float_value)`.
- The base quantum is `Decimal("0.00000001")` and rounding is `ROUND_HALF_EVEN`. Quantize stored/returned ledger, dividend, converted valuation, and split-continuous volume values. Keep arithmetic in Decimal until explicit presentation boundaries.
- Split ratios must be finite and strictly positive. Apply actions in stable `(session, action_type)` order; a split affects rows strictly before its effective session.
- FX orientation is closed: `GBPUSD=X` means USD per GBP. A five-calendar-day-old rate is valid; six days is stale. The engine/calendar layer—not this module—decides the latest session completed at an instant and passes that date as a bound.
- Preserve exchange-local security session dates. FX evidence is UTC-normalized. Do not compare naive datetimes across exchanges or use current wall-clock time.

### Architecture compliance and anti-patterns

- Follow Routes → Services → Engine → Repositories. These are pure service/domain helpers; no route, Agent, live portfolio, `TraderAgent`, order path, or SIPP/ISA repository dependency is allowed.
- Cache-only means no adapter call from plane, action, money, or FX modules. Missing exact evidence must fail visibly.
- Do not reuse live SIPP import money/FX state or `price_cache_repo.py`; Strategy Manager evidence and simulation remain isolated.
- Do not persist derived planes: they are deterministic projections of exact pinned evidence plus as-of bound/policy version. Later replay manifests pin their code and evidence revisions.
- Do not use pandas as an arithmetic authority here. A later `MarketView` may project typed rows to DataFrames, but canonical transformations remain Decimal-based and independently testable.

### Library and framework requirements

- Python `>=3.12`; stdlib `dataclasses`, `datetime`, `decimal`, and `enum` are sufficient.
- The lock is authoritative: yfinance 1.4.1, pandas 3.0.3, and exchange_calendars 4.13.2. No dependency upgrade is part of this story.
- Current yfinance documentation confirms history start is inclusive/end exclusive, `auto_adjust` defaults true unless explicitly disabled, and `repair=True` can alter split/dividend/currency evidence. Preserve Story 1.3's explicit `auto_adjust=False`, `repair=False`, and full request contract.

### Testing requirements

- Tests must prove equations, not merely object shapes: ordinary/reverse split value continuity, per-share basis inverse, price/volume continuity, and one dividend credit.
- Use multi-action fixtures and dates on both sides of `D` to prove no look-ahead and interval boundaries.
- Test exact half-even ties and conversion direction with values that distinguish multiplication from division and GBp from GBP.
- Assert typed error codes and concise details; do not expose a fabricated gap inventory.
- Preserve all 771 tests established by Story 1.3, including qualification digest and fixed 1970–2100 calendar regressions.

### Previous Story Intelligence

Story 1.3 established identity-bound `HistoricalEvidenceRequest`, immutable `HistoricalEvidencePayload`, exact IEEE-754 hex encoding, canonical action rows, and `StoredHistoricalEvidence`. It deliberately deferred every derived plane and accounting policy to this story. Its full regression found and fixed a rolling-calendar bug, reinforcing that date behavior must be fixed and fixture-driven. The successful repository pattern is typed immutable contracts, stable error codes, compare-and-insert storage, and exact-reference reads—extend those patterns without weakening them.

### Git Intelligence

- Baseline `43bea29` merged Story 1.3 and is the implementation base.
- Recent backtest modules use frozen dataclasses, explicit repository ownership, canonical manifests, and focused `tests/backtest/` fixtures.
- The merged SIPP import work is unrelated live-portfolio functionality. Preserve its files and do not share its state with Strategy Manager.

### Project Structure Notes

- Add deterministic market-plane, corporate-action, and FX/money modules under `app/services/backtest/` with matching `tests/backtest/test_*.py` files.
- Reuse `app/repositories/historical_price_repo.py` as the evidence boundary; no new database or schema is expected.
- No UI changes are required. Later UX surfaces must use factual labels, explicit currency/signs, and concise failure reasons without source-gap inventories.

### Latest Technical Information

- yfinance's current `PriceHistory.history` documentation exposes `actions`, `auto_adjust`, `back_adjust`, `repair`, `keepna`, `rounding`, timeout, and error controls; start is inclusive and end exclusive. Keep the explicit Story 1.3 contract because defaults differ. [Source: https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html]
- yfinance documents that repair can rewrite split, dividend, volume, and currency-unit evidence and may produce false positives. `repair=False` remains a replay-integrity requirement. [Source: https://ranaroussi.github.io/yfinance/advanced/price_repair.html]
- Python's stdlib `decimal` module provides exact decimal arithmetic, `quantize`, and `ROUND_HALF_EVEN`; use an explicit quantum and rounding mode instead of ambient context defaults. [Source: https://docs.python.org/3/library/decimal.html]

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` — Epic 1, Story 1.4]
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md` — AD-6, AD-20–AD-22, Stack, Structural Seed]
- [Source: `_bmad-output/planning-artifacts/sprint-change-proposal-2026-08-10.md` — revised FR-6 and implementation gates]
- [Source: `_bmad-output/planning-artifacts/ux-designs/ux-Agents.stocks-2026-08-09/EXPERIENCE.md` — failure voice, evidence labels, accessibility floor]
- [Source: `_bmad-output/implementation-artifacts/1-3-store-immutable-provider-native-historical-evidence.md` — prior implementation and verification intelligence]
- [Source: `app/services/backtest/historical_price_evidence.py` — exact provider-native payload and quote-unit contract]
- [Source: `app/repositories/historical_price_repo.py` — exact immutable evidence reads and failures]
- [Source: `tests/backtest/fixtures/market_mechanics_v1.json` — split, dividend, volume, FX, and freshness fixtures]

## Dev Agent Record

### Agent Model Used

OpenAI Codex (GPT-5)

### Debug Log References

- 2026-08-11: RED — focused suites failed at collection because the market-plane, corporate-action, and currency policy modules did not exist.
- 2026-08-11: GREEN — 32 Story 1.4/qualification tests passed after implementing all four gates and deterministic Decimal-context hardening.
- 2026-08-11: REGRESSION — all 800 repository tests passed with one existing warning; touched-scope Ruff, format, and Pyrefly checks passed.

### Implementation Plan

- Build pure Decimal-based planes from exact stored evidence with bounded as-of access.
- Add versioned exactly-once split/dividend policies and closed quote-unit/FX conversion.
- Prove all action, look-ahead, rounding, and failure boundaries through deterministic fixtures and full regression.

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Added versioned cache-only provider-native, as-traded, and split-continuous-as-of price/volume projections with exact revision provenance, bounded actions, fixed Decimal precision, and no `Adj Close`/dividend adjustment leakage.
- Added pure exactly-once split and `DividendCashPolicyV1` accounting contracts with deterministic action keys, carried-at-open entitlement, integer-share/fractional-action failure, value continuity, quote-unit provenance, and eight-place ledger rounding.
- Added the shared USD/GBP/GBp quote-unit contract and immutable `GBPUSD=X` conversion with explicit completion bounds, five-calendar-day carry, correct multiply/divide orientation, half-even rounding, and visible typed failures.
- Corrected and repinned ordinary/reverse-split qualification fixtures to the architecture's provider-restated basis and proved both price/share and price/volume continuity.
- Verification: 800 full-suite tests pass (one existing warning); touched-scope Ruff lint/format and Pyrefly report no errors.
- Applied all 11 code-review patches, including stricter evidence and Decimal contracts, final-only FX rounding, distinct plane row types, and adversarial integration coverage.
- Review verification: 821 full-suite tests pass (one existing warning); all changed files pass Ruff lint/format and Pyrefly.

### File List

- `app/services/backtest/market_planes.py`
- `app/services/backtest/corporate_actions.py`
- `app/services/backtest/currency.py`
- `tests/backtest/fixtures/market_mechanics_v1.json`
- `tests/backtest/test_market_mechanics_fixtures.py`
- `tests/backtest/test_market_planes.py`
- `tests/backtest/test_corporate_actions.py`
- `tests/backtest/test_backtest_currency.py`
- `_bmad-output/implementation-artifacts/1-3-store-immutable-provider-native-historical-evidence.md`
- `_bmad-output/implementation-artifacts/1-4-produce-deterministic-price-volume-corporate-action-and-fx-planes.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml`

### Change Log

- 2026-08-11: Created comprehensive Story 1.4 implementation context; marked ready-for-dev.
- 2026-08-11: Implemented all deterministic market-plane, corporate-action, and currency/FX gates; passed 800 tests and all touched-scope quality checks; moved Story 1.4 to review.
