---
title: 'Equal-capital allocation across backtest strategies'
issue: 368
status: 'implementation-ready'
created: '2026-08-28'
---

# Issue #368 — Equal-capital allocation

## Confirmed scope

All six production backtest strategies use engine-owned equal-capital BUY
allocation. Capital is divided equally between stocks selected together;
strategy-owned `fixed_shares` is removed. Buy and Hold remains an indefinite
holder; its ranking/Top-X selection remains #370's scope.

## Allocation contract

1. A cohort is the unique, otherwise eligible BUY candidates emitted for one
   normalized union signal session, after duplicate/pending/held/end-of-run
   rejection. Exits remain full-position exits and never participate.
2. Allocatable cash is current cash less the base-currency reservations held
   by earlier unfilled BUY orders. The cohort gets equal deterministic targets
   of that amount divided by the cohort size.
3. Each BUY order reserves its target immediately. At its own next MIC session,
   its actual native opening price and pinned FX determine whole shares as
   `floor(target / base-cost-per-share)`. A zero-share order records an
   unaffordable-allocation skip; otherwise only the actual cost is debited and
   all remaining reservation is released.
4. Therefore different exchange calendars and currencies cannot alter the
   target, no later cohort can spend reserved capital, residual cash is
   preserved, and deterministic security ordering affects only event order.
5. Engine semantics/version identity advance for new manifests. Existing
   immutable manifests/results remain readable unchanged.

## Stories

1. **#368 (this story):** introduce the shared reservation/fill allocator,
   retire `fixed_shares` from all six production Strategy interfaces, and prove
   multi-currency/calendar, rounding, reservation, exit/re-entry, restart and
   clean-journey compatibility.
2. **#370 (depends on #368 and #369):** make Buy and Hold select the strongest
   Top-X universe members once, then hand its selected cohort to this allocator.
3. **#371 (depends on #370):** expose Top-X configuration and explain the
   initial ranking/allocation evidence in the result UI.

## Acceptance checks

- Equal eligible contemporaneous BUY candidates have equal base-currency
  targets, subject only to actual fill price/FX and whole-share flooring.
- No ordering, calendar, pending order, or later signal can overspend cash.
- All production strategy metadata/runtimes no longer supply BUY arithmetic.
- Identical pinned evidence replay identical quantities, cash and events.
- Historical completed results and manifests remain immutable/readable.
