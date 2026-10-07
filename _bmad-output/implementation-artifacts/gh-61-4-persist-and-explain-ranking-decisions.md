---
title: 'Persist Skill-supplied ranking evidence and generic slot outcomes'
type: story
story_key: gh-61-4-persist-and-explain-ranking-decisions
parent_issue: 61
issue: 65
issue_url: https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/65
status: done
created: '2026-10-06'
depends_on: [gh-61-1-skill-priority-contract-and-buy-hold, gh-61-2-weinstein-momentum-with-bounded-currency]
feature_plan: ../planning-artifacts/feature-gh-61-skill-owned-entry-ranking.md
---

# Story 4 — Explain preference and actual allocation

As a backtest user, I want to see the Skill's ranking alongside actual admission/fill outcomes, so I can tell whether a stronger candidate lost a scarce slot, was already held, or could not be bought for another reason.

Parent: https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/61

GitHub prerequisites: #62 and #63. Can proceed alongside #64.

## Scope

Persist the original supplied priority and structured explanation without recalculating scores. Add generic mechanical allocation evidence to the existing session-batch staging/completion path. Engine instrumentation may record its existing preflight and slot decisions; it must not implement ranking policy or parse explanation facts to choose winners.

Prefer optional versioned companion audit records joined by run/session/security/side/rule and event identity/sequence. Preserve existing economic trade events and old Result payload meaning. Capture the intended fill session, pending reservations/releases and whether rejection came before slot consideration, from no usable slot, from competition, or after admission at fill time.

Expose a concise summary and paginated candidate audit in the existing Result surface. Render Skill explanations generically; no strategy-specific presenter formulas. Old Results show evidence was not recorded. No new comparison screen is required.

## Acceptance criteria

- **AC1:** Given a Skill-supplied rank and explanation, when the candidate is admitted, skipped or later fails to fill, then the Result retains the original evidence and links it to the actual outcome without inventing a score.
- **AC2:** Given ten held positions with no releasable slot, when many entries signal, then skips count as full-book rejections and do not inflate contested-opportunity counts.
- **AC3:** Given eight occupied slots and three otherwise eligible candidates filling on the same date, when a cap of ten is applied, then diagnostics record two admitted and one competition rejection. The Skill rank is distinct from the host-filtered cohort position.
- **AC4:** Given pending BUY reservations and SELLs releasing slots on different exchange sessions, when candidates compete, then recorded availability matches the allocator's actual candidate-specific fill-date logic. Do not approximate it using only signal-date holdings.
- **AC5:** Given a high-ranked candidate already held or pending, when rejected by preflight, then the audit names that reason; lower-ranked admitted candidates are not incorrectly called ranking violations.
- **AC6:** Given a retried session batch, interrupted worker, cancellation or failed completion, when persistence resumes, then audit rows are idempotent/atomic with that lifecycle and partial rows do not appear as a completed Result.
- **AC7:** Given completed historical Results lacking audit rows, when read/rendered, then they remain readable with a not-recorded state. Migration never rewrites historical manifest/result bytes or backfills invented rankings.
- **AC8:** Given audit data tampering or missing required rows for a new audited Result, when integrity validation runs, then it reports a precise failure; legacy absence is handled by the stored contract version.
- **AC9:** Given a large Result, when its first page loads, then candidate details are paginated and historical scores are read from persisted evidence without current-price queries, full-log recomputation or Skill reruns.
- **AC10:** Given a synthetic unknown Skill with a different explanation, when it supplies priorities, then the same generic audit path works without a host strategy-name branch.

## Tasks

- [x] Add a minimal versioned audit model and additive companion persistence schema.
- [x] Capture original signal evidence plus generic preflight/slot/fill links without altering selection arithmetic.
- [x] Integrate incremental staging, promotion, idempotency and integrity checks.
- [x] Add summary counts, missing-evidence coverage and paginated Result projection/rendering.
- [x] Test full-book versus actual contests, cross-calendar releases, old data, retries and tampering.
- [x] Measure audit storage growth, bounded memory and Result-read behavior.

## Likely files and seams

Planning guidance: `backtest_engine.py` event observation only; `worker.py` session staging; `app/repositories/backtest_repo.py` additive audit storage and reads; `strategy_protocol.py`/existing explanation types only as needed for data contracts; `result_presenter.py`, `_backtest_result.html`, existing Strategy Manager result routes; repository/worker/result-rendering and engine tests.

## Checks and rollout

Test schema creation and upgrade on disposable databases, atomic batch retries, restart/completion consistency, deterministic serialization, old Result reading, paginated routes and accessible rendering. Run relevant worker/repo/engine/UI tests and static checks. Keep host execution identity/versioning honest; release audit support before the final comparison. Do not modify production historical rows as a migration step.

Depends on stories 1 and 2; can proceed alongside story 3. Generic data and audit plumbing is allowed, but ranking methodology remains exclusively inside the Skills. No implementation was performed when this story was authored.
