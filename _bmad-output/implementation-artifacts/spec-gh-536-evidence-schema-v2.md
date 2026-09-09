---
title: GH-536 additive compressed historical evidence schema v2
type: feature
created: 2026-09-09
status: in-progress
github_issues: [444, 536]
---

## Intent

Replace the v1 physical duplication of canonical historical manifests and
normalized observations for newly written evidence. v2 retains the canonical
uncompressed revision digest, stores immutable revision metadata once, and
maps each revision to immutable compressed chunks addressed by the digest of
their uncompressed canonical JSON.

## Boundaries

- v2 is additive and idempotent. #537 owns migration, activation, rollback,
  and any removal of v1 evidence; #538 owns retention and deletion.
- A v2 read must reconstruct the identical canonical manifest and
  `StoredHistoricalEvidence` returned by v1, or reject the evidence.
- Chunks are bounded by calendar year and content-addressed by their
  uncompressed canonical payload. Compression is an encoding, never identity.
- Use the standard-library zlib codec with an explicit format version. A
  separate benchmark must record actual chunk size and read behavior before a
  production migration is authorized.
- Do not change route callers, provider fetches, pinned references, or
  immutable revision identity.

## Initial Schema

- `historical_price_v2_revisions`: integer `revision_id`, existing
  `data_revision`, immutable metadata JSON, counts, and canonical format
  version.
- `historical_price_v2_chunks`: content digest, codec/version, compressed
  bytes, uncompressed byte length, and immutable payload metadata.
- `historical_price_v2_revision_chunks`: revision surrogate id, ordered year
  chunk mapping, and kind (`rows` or `actions`).

## Acceptance

- Overlapping revisions reuse an identical compressed chunk without changing
  either revision's canonical digest.
- Loading a v2 revision decompresses only its mapped chunks and rejects wrong
  codec/version, length, digest, JSON shape, ordering, or reconstructed
  manifest identity.
- Existing repository callers receive the same `StoredHistoricalEvidence`
  fields and exact rows/actions for v1 and v2 fixture payloads.
- Source and test changes are covered by focused repository tests, format
  checks, and the full suite before review.

## Progress

- [x] Add immutable v2 revision, chunk, and mapping tables.
- [x] Add opt-in v2 write/read reconstruction, chunk reuse, generic read
  compatibility, and corruption/immutability tests.
- [x] Benchmark a read-only deterministic 25-revision sample from main's
  historical database. It measured 89.53% smaller encoded payloads; this is
  not a full-cache migration or cutover claim.
- [ ] Complete migration/cutover integration in GH-537; no v1 deletion occurs
  in this story.
- [ ] Perform independent review and final regression validation.

Focused repository/evidence checks: 36 passed. The full suite completed 3,205
tests successfully; one pre-existing lifespan test failed because this clean
worktree lacks the configured Backtest database file.

## Deferred

No production database migration, v1-to-v2 cutover, deletion, garbage
collection, or 70% reduction claim is part of this increment.
