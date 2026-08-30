---
title: 'Finish plain-language rename on Prepare historical data'
type: 'feature'
status: 'in-progress'
github_issue: 413
baseline_revision: 'a27a74c0'
---

## Tasks & Acceptance

- [ ] Add tests for Prepare data copy, concise month help, and human-readable history containing the preparation range and relative time.
- [ ] Thread each initialization run’s requested range into the existing history context without changing job behavior or routes.
- [ ] Update the initialization template’s button, help text, and history wording while preserving activity links.

**Acceptance Criteria:**
- The submit action says “Prepare data”.
- Past preparation entries display their prepared month range and relative time, never a raw UUID.
- The native month input help no longer restates `YYYY-MM`.
- POST/validation/job behavior is unchanged.

## Recorded decision

Use the existing `relative_time` filter and each initialization run’s persisted requested start/end range. If a historical job’s run cannot be loaded, omit that history entry rather than expose its opaque UUID.
