---
title: 'Persist Skill ranking evidence and explain generic slot outcomes'
type: feature
baseline_revision: a375a5586e3993279acc790b00df05d6b456e01d
final_revision: b7df5a65c9b443dc1c00da263492c8108c6a3385
created: '2026-10-06'
status: done
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
---

<intent-contract>

## Intent

**Problem:** Results preserve fills and skips but lose the Skill's supplied priority and explanation, as well as the host's slot decision. Users cannot distinguish an ineligible/held candidate, a full book, a true contest, or a later fill failure.

**Approach:** Persist generic per-candidate audit evidence as versioned companion data through the existing session-batch and atomic Result lifecycle. Add summary and paginated audit views to the existing Result page.

## Boundaries & Constraints

**Always:** Preserve the original Signal priority and explanation as supplied. Record engine mechanics at the existing preflight, slot, and fill decisions, including intended fill session, host-filtered cohort position, candidate-specific held/pending BUY reservations and pending SELL releases, disposition, and linked event sequence. Ranking priority remains distinct from host cohort order. New audit evidence is versioned, integrity-checked, staged and promoted atomically, and excluded from existing economic event meaning. Keep explanations opaque to execution and render them generically. Historical Results without the new contract remain unchanged and display “not recorded”.

**Block If:** Existing storage or lifecycle constraints make required audit persistence impossible without rewriting historical Result or manifest evidence; report the exact constraint.

**Never:** Recalculate or interpret Skill scores; branch on strategy identity; alter selection arithmetic, trade events, or manifest bytes; rerun Skills, query prices, or reconstruct scores to render an audit; backfill old Results.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Full book | No slot can release by candidate fill | Record a full-book rejection; do not count a contest | Preserve skip event link |
| Contested cohort | Available slots are consumed by higher-priority admitted candidates | Record admissions and only the actual losing contest, with Skill priority separate from host cohort position | Preserve deterministic allocator behavior |
| Preflight rejection | Candidate is held, pending, unpinned, or has no fill session | Record the actual preflight reason before slot consideration | Do not call it a ranking loss |
| Delayed fill | Admitted candidate fills or skips at its intended session | Link original evidence to final fill/skip event and outcome | Missing link in a new audited Result is an integrity error |
| Legacy read | Result predates audit contract | Show evidence not recorded; do not infer rows | Continue normal Result integrity behavior |
| Retry or cancellation | Batch is retried, fails, or is cancelled | Idempotent append; no partial audit visible as a completed Result | Reject divergent retry; preserve lifecycle fencing |
| Large audit | More candidates than one page | Return one bounded page plus summary | Validate page bounds and stored evidence |

</intent-contract>

## Code Map

- `app/services/backtest/backtest_engine.py` -- validated signals, candidate preflight, fill-date slot allocation, pending orders, and fill events.
- `app/services/backtest/worker.py` -- publishes each session through the fenced staging sink.
- `app/repositories/backtest_repo.py` -- bounded staging batches, atomic Result promotion, immutable Result digests, typed reads.
- `app/services/backtest/strategy_protocol.py` and `strategy_explanation.py` -- existing priority and versioned generic explanation contracts.
- `app/services/backtest/result_presenter.py` -- pure display projections for persisted evidence.
- `app/api/routes/strategy_manager.py`, `app/api/templates/_backtest_result.html` -- existing Result route and accessible Result surface.
- `tests/backtest/test_backtest_engine.py`, `test_backtest_repo_results.py`, `test_backtest_worker.py`, `test_result_presenter_trade_log.py`, `tests/test_strategy_manager_routes.py` -- execution, lifecycle, persistence, presentation, and route proofs.

## Tasks & Acceptance

**Execution:**
- [x] `backtest_engine.py` -- emit one typed generic BUY-candidate audit record with original priority/explanation, preflight or slot decision, candidate fill date, reservation/release snapshot, host cohort position, final outcome, and matching event sequence; carry admitted evidence to fill without changing allocation.
- [x] `worker.py` and `backtest_repo.py` -- stage audit deltas in the same fenced, idempotent transaction as session batches; atomically promote a version/count/digest summary and immutable rows; distinguish legacy contracts and verify missing/tampered rows.
- [x] `backtest_repo.py` -- expose bounded summary and paginated reads from persisted rows; keep event economics and historical manifest/result bytes unchanged.
- [x] `result_presenter.py`, `strategy_manager.py`, `_backtest_result.html` -- show counts, recorded/not-recorded state, generic explanations and candidate pages without price reads, score calculation, or Skill execution.
- [x] `tests/backtest/test_backtest_engine.py`, `tests/backtest/test_backtest_repo_results.py`, `tests/backtest/test_backtest_worker.py`, `tests/backtest/test_result_presenter_trade_log.py`, and `tests/test_strategy_manager_routes.py` -- add full-book/contest, cross-calendar, preflight/fill, unknown Skill, retry/cancel, legacy/tamper, pagination, and accessibility proofs.
- [x] `tests/backtest/test_backtest_repo_results.py` -- measure audit storage growth and verify page retrieval stays bounded on a large fixture.

**Acceptance Criteria:**
- Given a ranked BUY candidate, when it is rejected, admitted, filled or skipped later, then the exact supplied priority/explanation and actual outcome are linked without inventing a score.
- Given a full ten-position book with no eligible SELL release, when candidates signal, then they are full-book rejections and do not inflate contested counts.
- Given eight occupied slots and three otherwise-eligible same-date candidates under cap ten, when allocated, then two are admitted and one loses a real contest; Skill priority and host cohort position remain separate.
- Given pending BUYs and SELL releases across exchange calendars, when candidates compete, then recorded availability follows each candidate's actual fill-date allocator inputs.
- Given a held or pending high-priority candidate, when preflight rejects it, then its reason is recorded and lower-ranked admissions are not described as ranking violations.
- Given a retry, worker interruption, cancellation, or failed completion, when lifecycle work resumes or ends, then writes are idempotent/atomic and no partial audit appears as a completed Result.
- Given an older Result, when read or rendered, then it remains readable as not recorded and is never backfilled.
- Given tampered audit data or missing audit rows for a new contract, when integrity validation runs, then it reports the specific defect.
- Given a large Result, when its first audit page loads, then candidate details are paginated from persisted evidence without price queries, Skill reruns, or rank reconstruction.
- Given an unregistered Skill with a different valid explanation, when it supplies a priority, then the same generic audit path handles it without strategy-name branching.

## Spec Change Log

## Review Triage Log

### 2026-10-06 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 12 (high 2, medium 10, low 0)
- defer: 0
- reject: 0
- addressed_findings:
  - `[high]` `[patch]` Blind Hunter and Edge Case Hunter both found valid delayed BUY outcomes could be recorded out of candidate-sequence order and fail completion. Validate the global sequence independently from outcome-session order.
  - `[medium]` `[patch]` Runs resumed from pre-audit session batches could mix old and new batch coverage. Persist a staging contract marker; resumed legacy runs stay not-recorded instead of inventing missing history.
  - `[high]` `[patch]` Missing every audit batch header could be inferred as a legacy run. The staging contract marker now makes missing coverage on an audited run an integrity error.
  - `[medium]` `[patch]` A page read parsed all candidate rows and queried every linked event before applying pagination. Read and validate only the selected page and its event links.
  - `[medium]` `[patch]` The initial Result load repeated full audit validation. Summary reads now use manifest metadata and boundary probes; selected rows are validated by the page query.
  - `[medium]` `[patch]` A candidate-page integrity error was not handled locally on the full Result route. Render it in the candidate-audit alert while preserving the rest of the Result.
  - `[medium]` `[patch]` The audit omitted available slots at cohort start and which earlier admissions consumed them. Show cohort-start capacity and prior admissions.
  - `[medium]` `[patch]` The audit reduced held positions and pending BUY orders to aggregate counts. Show their identities, pending fill dates, and reserved amounts.
  - `[medium]` `[patch]` The Trade Log event number was not a direct link. Link to the exact event row in the Result Trade Log.
  - `[medium]` `[patch]` The storage test counted JSON payload bytes only. Compare SQLite allocated database pages against an otherwise identical result without audit rows.
  - `[medium]` `[patch]` The pagination test did not detect unbounded detail queries. Trace SQL and require each candidate-detail read to use the requested LIMIT/OFFSET and event-link set.
  - `[medium]` `[patch]` A missing StrategyRun escaped from the audit fragment route. Return a not-found response.

## Auto Run Result

Status: done

Summary: Persisted Skill-supplied candidate priority and explanation with generic engine allocation decisions, staged and promoted atomically beside existing Result data. Added paginated Result presentation with capacity reservations, prior cohort decisions, explanations, and linked Trade Log outcomes.

Files changed:
- `app/services/backtest/backtest_engine.py` — capture candidate evidence at preflight, slot allocation, and fill outcomes.
- `app/services/backtest/worker.py` — stage each session's audit with the existing fenced batch.
- `app/repositories/backtest_repo.py` — audit staging contract, integrity validation, atomic promotion, bounded page reads.
- `app/services/backtest/result_presenter.py` — format generic candidate and capacity evidence.
- `app/api/routes/strategy_manager.py`, `app/api/templates/_backtest_result.html`, `app/api/templates/_backtest_candidate_audit.html` — Result and paginated audit surfaces.
- `tests/backtest/test_backtest_engine.py`, `tests/backtest/test_backtest_repo_results.py`, `tests/test_strategy_manager_routes.py` — execution, lifecycle, capacity, integrity, storage, pagination, and route regressions.
- `scripts/benchmark_incremental_backtest_staging.py` — update bounded staging benchmark for the session audit payload.
- `_bmad-output/implementation-artifacts/github-bmad-tracking.yaml`, `_bmad-output/implementation-artifacts/sprint-status.yaml`, `gh-61-4-persist-and-explain-ranking-decisions.md`, and this specification — track story progress and review evidence.

Review findings: 12 unique patch findings fixed (2 high, 10 medium); no findings deferred or rejected. The sequence-order finding was independently reported by both reviewers.

Follow-up review recommendation: true. The fixes span lifecycle contracts, staging compatibility, result integrity, pagination, and visible capacity evidence.

Verification:
- Focused engine, repository, worker, presenter, and route suite: 364 passed, 2 warnings.
- Full suite: 4,185 passed, 7 failed, 12 errors. The 7 failures were browser tests blocked by Chromium's `bootstrap_check_in` permission error. Eleven browser setup errors were caused by localhost binding being denied; one teardown error came from the isolated worktree's empty 610 KB `data/backtest.db` being touched by the suite. That file has no Strategy Runs or Results, and the main checkout's 20 GB database was unchanged.
- `ruff check` passed on changed Python files. Repository-wide Ruff reported 10 existing lint errors in untouched files. Repository-wide `ruff format --check` reported 34 files, including the baseline `backtest_repo.py`; unrelated formatting was restored to keep this patch focused.
- `ruff format --check` passed on the other changed Python files; `git diff --check` and `compileall -q app tests` passed.
- Pyrefly reports one pre-existing nullable `historical_price_repository.pin()` error at `app/repositories/backtest_repo.py:3682`; no other error was reported for the checked modules.

Residual risks: Full audit integrity is validated during promotion; Result reads validate the manifest and selected page rows/events to keep pagination bounded. A changed row outside the requested page is checked when that page is read. The full browser suite needs a host that allows localhost binding and Chromium startup.

## Design Notes

Persist each candidate only when its disposition is known: preflight/slot rejections in the signal session, admitted candidates with their fill or fill-time skip. The pending order carries the original opaque evidence and slot snapshot. This keeps session writes incremental and gives every candidate one final linked outcome row. Audit version, row count, summary and digest are bound to the new Result contract; legacy Result contracts keep their current interpretation.

## Verification

**Commands:**
- `rtk pytest -q tests/backtest/test_backtest_engine.py tests/backtest/test_backtest_repo_results.py tests/backtest/test_backtest_worker.py tests/backtest/test_result_presenter_trade_log.py tests/test_strategy_manager_routes.py` -- expected: all targeted cases pass.
- `rtk ruff check .` and `rtk ruff format --check .` -- expected: clean.
- `rtk pyrefly check` -- expected: no new type errors.
- `rtk pytest -q` -- expected: full suite passes, with any environment-blocked browser tests reported separately.

</intent-contract>
