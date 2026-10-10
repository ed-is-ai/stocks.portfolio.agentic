---
title: 'Strategy Manager backtest intelligence (#101–#103)'
type: feature
created: '2026-10-09'
baseline_revision: 2cbe385f53a117d732b6bb86b4325f97a8d8912e
status: done
review_loop_iteration: 1
followup_review_recommended: true
context:
  - _bmad-output/planning-artifacts/feature-gh-20-strategy-manager-contextual-agent-surfaces.md
  - _bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md
  - _bmad-output/implementation-artifacts/spec-gh-15-strategy-experiment-agent.md
warnings: [multiple-goals, oversized]
---

<intent-contract>

## Intent

**Problem:** Strategy backtest outcomes must be opened one at a time, making comparable performance and its evidence difficult to scan. Users also lack a grounded way to ask why results differed or ask follow-up questions from the Strategy Manager.

**Approach:** Add a deterministic, bounded outcome summary and accessible local valuation comparison to the existing landing view. Add explicit Claude-first insight and Strategy Q&A actions over the same sanitized evidence, with local validation and audit records.

## Boundaries & Constraints

**Always:** Preserve the landing status, CTA, readiness/coverage, provenance, result list, sub-screen templates, and route contracts. Inspect no more than 25 latest verified completed non-tombstoned Result candidates. Use `BacktestRepository.is_comparable` as the sole eligibility rule, select cohorts deterministically, and report exclusions. Display stored metrics/availability and stored equity only; normalize curves for display only. Keep factual claims linked to local evidence. Landing GET is local and never calls a model. Claude receives only the allowlisted aggregate summary; Foundry fallback, if used, stays loopback-only. Validate structured responses and every citation locally. Provider and copilot calls run off the event loop. Q&A is stateless, audited, and read-only.

**Block If:** Meeting an acceptance criterion requires changing comparison eligibility, backtest calculations, persisted Result contracts, the GH-13 `/copilot/ask` contract, or sending fields outside the approved summary/question allowlist.

**Never:** Generate executable Strategy source, browse the web, expose raw price/trade/event/curve histories or full manifests to a provider, let a model select evidence or use tools, enqueue or approve a job, optimize parameters automatically, change active Strategies/portfolios, or implement GH-20.4 insight-to-experiment handoff in these stories.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Comparable cohort | Up to 25 verified Results across Strategies | Deterministic largest cohort, newest Result per Strategy, linked metrics and local curve | Report smaller/ineligible coverage and reasons |
| No shared cohort | Empty, one-Strategy, incompatible, corrupt, deleted, failed, or cancelled runs | Honest individual/empty state; no cross-Strategy winner; preserve CTA | Never convert missing data or metrics to zero |
| Insight request | Explicit Generate/Refresh for summary digest | Strict cited report from allowlisted fields, or exact cached report | Keep deterministic summary and last good report on invalid/unavailable providers |
| Strategy question | One question plus current Strategy and bounded summary | Concise cited answer and explicit unknowns; no mutations | Reject invalid/unknown citations and show unavailable state |
| Untrusted evidence | User question or stored text contains instructions | Treat all supplied text as data; cite only local handles | Reject unsupported result identities/numbers or malformed output |

</intent-contract>

## Authorized Claude Payload Allowlist

Calls are opt-in only: no provider request on page load, and the UI identifies the fields sent before the user submits an explicit Generate Insights or Ask action. The user explicitly approved both payloads on 2026-10-10 (“approve both”). Implement only these allowlists.

**GH-20.2 / #102 — summarized backtest insights:**

- Summary version/digest; cohort period, currency, strategy/result counts, exclusion counts/reasons and stated limitations.
- For each of at most 25 selected results: opaque evidence handle (`R01` etc.), Strategy ID/API version, tested period and currency, validated scalar parameter names/values (excluding universe-selector fields), selected-universe ticker symbols, the four persisted metrics and availability reasons, closed-trade/candidate counts, provenance quality/snapshot count, and whether it is the pinned SPY reference.
- Exclude run IDs, result URLs, profile hashes, starting capital, raw or indexed equity points, trade/event/price histories, full manifests, credentials, portfolio holdings and user text. The #102 request contains no user-submitted text.

**GH-20.3 / #103 — Strategy-scoped question:**

- The same bounded #102 summary, plus the exact question the user submits, current Strategy ID/API version, and its declared scalar parameter names/values.
- Exclude Strategy source code/digests, portfolio/account data, credentials, repository/database access and unbounded conversation history. Process one bounded question per explicit submission; use no tools or web access.

**Authorization record:** On 2026-10-10, the user approved both exact Claude payload allowlists for #102 and #103. This includes validated scalar parameter values, selected-universe ticker symbols, the submitted bounded question, and current Strategy ID/API version/scalar parameters. All exclusions above remain binding. Provider requests must result only from explicit user actions; tests must mock providers and must not send a live Claude request.

## Code Map

- `app/repositories/backtest_repo.py` — verified Result reads, canonical comparison predicate, Strategy experiment records, additive schema and append-only audit patterns.
- `app/services/backtest/result_presenter.py` — persisted metric availability and multi-Result indexed equity payload.
- `app/services/backtest/strategy_experiment_service.py` — real GH-15 drafts, detail, Review and Discard lifecycle.
- `app/agents/strategy_experiment/agent.py` — Claude-first strict output and fixed-loopback Foundry fallback precedent.
- `app/agents/research/copilot.py`, `app/schemas/copilot.py`, `app/api/routes/copilot.py` — GH-13 evidence citations, bounded async route, and behavior to preserve.
- `app/api/dependencies.py` — provider/repository dependency injection.
- `app/api/routes/strategy_manager.py` — existing landing context and additive Strategy endpoints.
- `app/api/templates/_strategy_manager.html`, `_multi_comparison.html`, and new partials — landing composition, existing accessible chart, and new UI.
- `tests/test_strategy_manager_routes.py`, `tests/test_copilot_route.py`, `tests/test_research_copilot.py`, and `tests/backtest/` — route, rendering, comparison, integrity, provider and repository tests.

## Tasks & Acceptance

**Execution:**
- [x] `app/repositories/backtest_repo.py` — add bounded latest completed Result candidate reads; keep Result records immutable.
- [x] `app/repositories/backtest_repo.py` — add transactional digest-keyed insight/audit persistence; keep Result records immutable.
- [x] `app/schemas/strategy_outcomes.py`, `app/services/backtest/strategy_outcomes.py` — define the local typed outcome summary; inspect at most the 25 latest verified completed non-tombstoned Results; use only the canonical comparator; choose the cohort with the most distinct Strategies, then newest completion time, then lexicographically smallest sorted run-ID set; keep the newest Result per Strategy (completion descending, run ID ascending on ties) and retain coverage/exclusion facts and local citation handles.
- [x] `app/agents/strategy_insights/agent.py`, `app/services/backtest/strategy_insights.py`, and `app/schemas/strategy_insights.py` — add strict Claude-first insight and Q&A contracts, attempt provenance including total failure, local Foundry fallback only if available, and reject unsupported identities, values, or citation handles.
- [x] `app/api/routes/strategy_manager.py` — inject the local outcome and experiment services; show the actual pending GH-15 draft or empty state; no provider call on landing GET.
- [x] `app/api/dependencies.py`, `app/api/routes/strategy_manager.py` — add authenticated explicit insight/refresh and stateless Q&A endpoints, executed in a worker thread.
- [x] `app/api/templates/_strategy_manager.html` and `app/api/templates/_strategy_outcome_summary.html` — preserve existing landing content and CTA; render linked metrics, tested setup/provenance/sample counts, local normalized curve with accessible table, and the real draft Review/Discard controls.
- [x] New insight and copilot partials — render provider disclosure, reports, and unknown states.
- [x] `tests/backtest/test_strategy_outcomes.py`, `tests/backtest/test_backtest_repo_comparison.py`, `tests/backtest/test_backtest_repo_experiments.py`, and `tests/test_strategy_manager_routes.py` — cover the bounded read, canonical cohort selection, pinned SPY labeling, curve accessibility, draft/empty states, and landing regressions.
- [x] `tests/backtest/test_strategy_insights.py` and `tests/test_strategy_manager_routes.py` — cover provider allowlists/fallback/audit/cache, prompt-injection text, read-only Q&A, and GH-13/19 regressions.

**Acceptance Criteria:**
- Given valid completed Results, when the summary builds, then only canonical-comparable Results share a cohort, selection is bounded and deterministic, each fact links to its Result, and the latest Result per Strategy is shown.
- Given incompatible or damaged candidates, when selection runs, then they are excluded with coverage/reasons and cannot be called winners.
- Given no multi-Strategy cohort, when the landing renders, then it shows individual/empty status and preserves the next-action CTA.
- Given null/insufficient metrics or differing starting capital, when the summary renders, then persisted availability is shown and the curve uses stored points, labels shared period/currency, indexes for display only, and has an accessible table.
- Given SPY reference evidence, when the summary includes it, then it is pinned, matching and accepted by canonical comparison; otherwise no SPY benchmark is shown.
- Given a pending GH-15 draft, when the landing renders, then actual hypothesis/status/locked facts and existing Review/Discard routes appear; an empty store shows no sample and enqueues nothing.
- Given explicit insight generation, when a provider is called, then it receives only the allowlisted structured summary and the screen discloses the transmitted fields; validated observations and testable hypotheses cite supplied handles, distinguish facts from hypotheses, and retain survivorship, sample-size, tested-parameter, and benchmark limitations without causal or future-return claims.
- Given invalid output or provider failure, when validation completes, then it is rejected and deterministic facts plus any last good report remain visible; each attempt records provider/model/outcome, digest, versions, timestamp and accepted citations.
- Given a Strategy question, when answered, then only current Strategy fields and the bounded summary are used, valid citations and unknowns are returned, the request is audited, and no state can be mutated.
- Given any visit to a Strategy Manager sub-screen or GH-13/19 copilot route, when it renders, then its existing contract and behavior remain unchanged.

## Spec Change Log

- 2026-10-10: User approved both exact Claude payload allowlists for #102 and #103. Issues can proceed with explicit-action requests and mocked-provider verification; no live provider call is authorized for development or tests.

## Review Triage Log

### Review Findings

Review date: 2026-10-10. Two independent passes completed (blind review and edge-case review). The acceptance-audit pass could not start because the agent thread limit was reached, so that review layer remains incomplete.

- [x] [Review][Patch] Cohort membership can combine different selected universes [app/services/backtest/strategy_outcomes.py:319]
  - `_select()` previously compared each candidate only with the first Result in a cohort. Since the canonical v2/v3 universe-digest check applies only when both Results have `universe_selection`, a legacy Result without selection could let two different selected universes share a cohort. Cohort assignment and exclusion reporting now compare against every member.
- [x] [Review][Patch] A settled experiment can remain on the pending-draft card [app/services/backtest/strategy_experiment_service.py:179]
  - `pending_details()` now checks the freshly loaded status and returns only experiments that are still drafts; the repository discard guard remains authoritative.
- [x] [Review][Patch] Outcome-summary failure can hide a separately loaded pending experiment [app/api/templates/_strategy_outcome_summary.html:1]
  - The summary now renders its unavailable state when summary loading fails, and the experiment card renders independently.

Resolution: all three review patches are applied. Focused verification passed 216 tests; scoped Ruff, formatting, Pyrefly, and `git diff --check` passed. Issue #101 is marked done in sprint tracking. Issues #102 and #103 are unblocked by the explicit payload authorization recorded above.

### Follow-up Review Findings

Review date: 2026-10-10. Independent blind and edge-case passes reviewed the full #101–#103 implementation diff. Triage found 14 patch findings (7 high, 7 medium), grouped below into 9 remediations, one deferred medium performance concern, and no intent gaps, bad specifications, or rejected changes.

- [x] [Review][Patch] Replace greedy cohort selection with exact pairwise-compatible cohort search, then include compatible candidates for coverage.
- [x] [Review][Patch] Bind question forms to the local Result run ID so changing list order cannot redirect an old `R##` handle.
- [x] [Review][Patch] Keep numeric values distinct from their percentage rendering and validate against only cited Result rows plus shared cohort fields.
- [x] [Review][Patch] Bind free-text Strategy names and structured Strategy IDs to cited evidence; reject unsupported number words while allowing ordinary phrases such as “one of several”.
- [x] [Review][Patch] Expand future and causal claim validation across later sentences, negation cases, question unknowns, and idea titles/descriptions.
- [x] [Review][Patch] Preserve deterministic limitations ahead of provider-supplied unknowns; report top-level period/currency only for a comparable cohort.
- [x] [Review][Patch] Derive ticker counts from filtered valid symbols and retain exact exclusion counts rather than capping them at the Result scan limit.
- [x] [Review][Patch] Audit stale-Result, stale-Strategy, and unavailable-catalog question submissions without calling a provider.
- [x] [Review][Patch] Exclude credential-, account-, and portfolio-like parameter names from both provider payloads and numeric validation.
- [ ] [Review][Defer] Bound or cache the metadata-only query that aggregates non-complete and deleted Backtest job counts on landing renders; it reads no Result payloads, but query cost grows with job history.

All patch findings were applied and covered by regression tests. Follow-up review is recommended because the local provider-output validator changed substantially.

## Design Notes

Use a dense, flat layout consistent with `DESIGN.md` and existing tokens. Reuse `_multi_comparison.html`'s chart conventions and local stored equity payload. Treat the three issues as one implementation run with ordered dependencies: finish the summary/evidence handles first, then insight generation and Q&A. Keep research ideas advisory; GH-20.4 owns any experiment handoff.

## Verification

**Commands:**
- `pytest tests/backtest tests/test_strategy_manager_routes.py tests/test_copilot_route.py tests/test_research_copilot.py` — 1,802 passed; after final summary-copy/test edits, the focused 245-test set passed again.
- Scoped `ruff check` and `ruff format --check` on changed Python files — pass.
- Scoped `pyrefly check` on changed production modules — 0 errors.
- Repository-wide Ruff reports 9 findings in unrelated files; repository-wide format check reports 30 other files to reformat; repository-wide Pyrefly reports 89 project errors. These broad checks are not clean.

## Auto Run Result

- Completed and independently verified issue #101 in `/private/tmp/stocks-gh20-devauto` on branch `codex/gh-20-101-103`. The bounded deterministic local outcomes summary, comparable cohort selection, stored equity chart/table, limitations and exclusions, and actual pending experiment Review/Discard block are implemented.
- Checks: 1,802 relevant tests passed before the final copy/test refinement; the final focused run passed 245 tests. Scoped Ruff check/format passed; scoped Pyrefly passed with 0 errors; `git diff --check` passed. Broad repository-wide Ruff, format, and Pyrefly checks reported the counts noted above.
- Issues #102/#103 were not implemented in the prior run because automatic approval review rejected the provider integration before the user authorized the exact payload. The user has now approved the bounded #102/#103 allowlists above. Continue with only those fields; tests use mocked providers, and no live Claude request is made during development.

## Final Verification — Issues #102–#103

- Relevant suite: `tests/backtest tests/test_strategy_manager_routes.py tests/test_copilot_route.py tests/test_research_copilot.py` — 1,830 passed (final run).
- Focused validator suite: `tests/backtest/test_strategy_insights.py` — 16 passed.
- Scoped Ruff check and format check — passed; scoped Pyrefly — 0 errors (4 suppressed, 4 warnings not shown); `git diff --check` — passed.
- Provider tests use mocks. No live Claude or Foundry request was made.
- Deferred: bound/cache landing metadata aggregation as recorded in `deferred-work.md`.

The #101–#103 spec is complete. GH-20 remains in progress while issue #104 is backlog.
