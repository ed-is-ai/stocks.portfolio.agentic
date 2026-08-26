---
title: 'GitHub #323: Bulk Opening Lot FIFO status'
type: 'performance'
created: '2026-08-27'
status: 'done'
---

<intent-contract>

## Intent

Trade History must derive every displayed Opening Lot status from one supplied
trade history and at most one FIFO replay per relevant portfolio.

## Constraints

Keep single-lot edit/delete guards authoritative and fresh. Preserve missing,
malformed-date, alias, unconsumed, partial, and consumed outcomes. Do not cache
mutable status between requests.

</intent-contract>

## Tasks & Acceptance

- [x] Add a supplied-history bulk Opening Lot status operation.
- [x] Update Trade History to call it once.
- [x] Add multi-lot, multi-portfolio, alias, malformed-date, and call-count tests.

## Evidence

- `uv run pytest tests/test_realised_pnl_service.py tests/test_realised_pnl_route.py -q` — 82 passed.
- Ruff check/format and `git diff --check` — passed.

Representative result: Trade History now makes one bulk status API call after
its existing history load; the service replays FIFO once per relevant portfolio,
rather than once per displayed Opening Lot.

## Review Triage Log

- adversarial/edge-case review: no findings.
- Fresh edit/delete guards remain on the single-lot status operation.

## Final Verification

- Focused service/route tests: 82 passed.
- Independent review verification: 101 targeted tests passed; Ruff and
  `git diff --check` passed.
