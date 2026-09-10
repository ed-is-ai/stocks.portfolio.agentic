---
title: GH-537 resumable verified historical evidence v1-to-v2 migration
type: migration
created: 2026-09-10
status: in-progress
github_issues: [444, 537]
---

## Intent

Populate the merged GH-536 v2 representation from offline v1 evidence without
network access, checkpoint progress durably, verify every reconstructed
canonical revision, and switch generic revision reads atomically only after a
complete verified migration. Rollback switches the active format to v1 without
deleting either representation.

## Boundaries

- Migration is an explicit offline repository/CLI operation. It must refuse a
  mutable source fingerprint, insufficient available capacity, malformed v1
  evidence, or incomplete verification.
- Each migrated revision is independently committed and verified before its
  checkpoint advances. Retrying after interruption is idempotent.
- The active-format singleton is the sole cutover switch. It defaults to v1;
  v2 activation and v1 rollback are each one SQLite transaction.
- v1 data remains intact. Consequently this story cannot claim a physical
  whole-cache reduction while rollback remains available; GH-538 owns safe
  removal and the final whole-cache measurement.

## Acceptance

- Restart after an injected interruption resumes from the durable checkpoint
  and performs no provider/network call.
- A changed v1 revision set or an invalid reconstructed digest refuses resume
  and activation.
- Preflight reports source size, free space, revision count, and required
  reserve before writing any v2 row.
- Activation requires every source revision verified in v2. Rollback restores
  v1 generic reads atomically.
- Tests cover restart, idempotency, corruption, source drift, capacity refusal,
  activation, and rollback.

## Deferred

The complete physical storage target and removal of v1 tables/rows are not
measurable or safe until GH-538's reference-aware retention decision.
