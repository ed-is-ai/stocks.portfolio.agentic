---
github_issue: 503
baseline_revision: f7163e49
---

# GH-503: Historical Data Preparation Liveness

Status: review

## Acceptance Criteria

- Persist completed-month progress, last committed month/time, cache reuse counts, and fresh-month timing for initialization jobs.
- Render inclusive progress, ETA, last commit age, reuse/fetch totals, and stable completed/failed/cancelled detail through the existing activity poll.
- Preserve the existing one-month atomic snapshot checkpoint and lifecycle fencing.

## Tasks

- [x] Add durable initialization progress and worker outcome accounting.
- [x] Render liveness and truthful terminal detail.
- [x] Add focused repository, engine, and route tests; run quality checks.

## Verification

- Focused lifecycle, repository, and Strategy Manager route tests: 291 passed.
- Ruff, Pyrefly, and `git diff --check` pass for touched files.
