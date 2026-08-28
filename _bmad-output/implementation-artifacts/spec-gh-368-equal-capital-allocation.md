---
title: 'Use equal-capital allocation across all backtest strategies'
type: 'feature'
github_issue: 368
parent_issue: 366
status: 'in-progress'
baseline_revision: '9d92782067e6617a4bbdcde3a37bf08e77128e80'
context:
  - '{project-root}/_bmad-output/planning-artifacts/feature-gh-368-equal-capital-allocation.md'
---

<intent-contract>

Engine-owned BUY allocation replaces every production Strategy's fixed-share
BUY sizing. A same-session eligible cohort receives equal base-currency targets
from unreserved cash; each target is reserved until its own next-MIC fill,
where actual open/FX produces whole shares. Existing manifests/results remain
readable; new execution semantics receive a distinct manifest identity.

</intent-contract>

## Tasks & Acceptance

- [ ] Add deterministic per-cohort allocation reservations and actual-fill
  whole-share calculation to the engine without changing full SELL behavior.
- [ ] Make reservations safe across multiple MIC fill dates, currencies,
  unaffordable candidates, exits/re-entry and retries.
- [ ] Remove `fixed_shares` from all six Strategy metadata/runtimes and make
  the engine the sole BUY-sizing authority.
- [ ] Advance execution identity and add focused/all-project regression tests.

## Verification

- `uv run pytest -q`
- scoped `ruff` and direct changed-module `pyrefly` checks
- `git diff --check`
