---
title: 'Backtest metrics formatting consistency'
type: 'feature'
created: '2026-08-29'
status: 'in-progress'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
github_issue: 420
baseline_revision: '29abbc4b'
---

<intent-contract>

## Intent

**Problem:** Backtest list/detail metrics use inconsistent precision, sign, colour, and money conventions versus each other and Realised P&L; neither backtest surface shows absolute P&L.

**Approach:** Centralize presentational backtest formatting, apply it to the existing list and Result values, and derive display-only P&L from persisted result evidence. No metric or storage calculation changes.

## Decisions recorded

- Max Drawdown remains detail-only; no list column is added.
- Win Rate remains the existing rate; win/loss counts are out of scope.
- Absolute P&L uses the final persisted equity-curve point minus starting capital; this is presentation-only.
- GBP uses `£1,234.56`; other currencies use `1,234.56 USD`.

## Boundaries & Constraints

**Always:** Preserve metrics JSON, repository queries, routes, list column order, calculation code, unavailable states, and comparison semantics. Use one decimal-place percentages; Total Return is signed/classed; Max Drawdown is loss-classed; Win Rate is unsigned/uncoloured.

**Never:** Do not change metric calculations, persistence, migration, strategy execution, or win-rate denominator.

## Tasks & Acceptance

- [ ] Add presenter and route/template tests for positive/negative/zero percentage and monetary formatting, unavailable metrics, and unchanged list shape.
- [ ] Add centralized display formatting and safe Result-derived P&L view values.
- [ ] Apply the views to backtest Result and list templates without adding columns.
- [ ] Run focused checks and record any baseline limitations.

**Acceptance Criteria:**
- Given a completed backtest, when Total Return and Win Rate render in the list and Result, then they use identical one-decimal conventions and gains/losses receive Realised-P&L-like sign/colour treatment.
- Given a completed backtest Result, when it renders, then it shows signed absolute P&L and starting capital in the run base currency, with GBP symbols and grouping.
- Given a drawdown, when the Result renders, then it is a one-decimal loss presentation; it does not become a results-list column.
- Given unavailable/incomplete evidence, when either screen renders, then its current unavailable behavior remains and no persisted/calculated data changes.
