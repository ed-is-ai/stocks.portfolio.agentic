---
title: 'GH-20 Strategy Manager contextual backtest intelligence'
type: feature
method: bmad-mini-plan
issue: 20
issue_url: https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/20
story_issues: [101, 102, 103, 104]
status: implementation-ready
created: '2026-10-09'
baseline_revision: 2cbe385f53a117d732b6bb86b4325f97a8d8912e
architecture: _bmad-output/planning-artifacts/architecture/architecture-Agents.stocks-2026-08-09/ARCHITECTURE-SPINE.md
completed_prerequisite_issues: [13, 15]
---

# GH-20 — Contextual backtest intelligence on Strategy Manager

## User problem and outcome

A portfolio owner can run and compare reproducible Strategy backtests, but must open results one at a time to understand what performed better, what evidence may explain the difference, and what is worth testing next. Experiment proposals and the research copilot also sit away from the Strategy Manager landing view.

The landing view should keep its readiness/preparation status, summarize comparable Strategy outcomes, show their valuation path and important metrics, explain evidence-backed observations, and suggest testable ideas. The user can ask follow-up questions against the same bounded evidence and route a compatible idea into the existing experiment review flow.

The feature describes historical outcomes for tested configurations. It does not declare a Strategy universally successful, establish causation from correlated metrics, or predict future performance.

## Confirmed scope

- Keep the current Strategy Manager landing information and adaptive next-action CTA.
- Add a bounded comparison summary of recent, verified completed backtests, with a normalized equity/value curve, persisted metrics, tested parameters, period, universe, currency, evidence provenance, sample counts, and source links.
- Explain differences only from supplied outcomes and aggregate candidate/exit audit facts. Make unsupported causal explanations explicit hypotheses.
- Generate structured, evidence-linked insights and ideas for new or changed Strategies. The user authorized summarized backtest outcomes to be sent to Claude as public data.
- Keep the GH-15 experiment proposal block on the landing page, showing a real draft or a declared empty state. Review still goes through the existing approval step.
- Keep a Strategy-scoped copilot on the landing page. It answers only from current Strategy configuration and the same bounded backtest evidence.
- Offer a user-controlled handoff from a compatible idea to GH-15's parameter-only draft flow. Approval is required before enqueueing a candidate backtest.
- Restrict UI work to the Strategy Manager landing view and additive partial/agent endpoints. Keep setup, readiness, initialization, activity, results, comparison, configuration, universe sub-screens and existing route contracts intact.

## Out of scope

- Changing Strategy rules, backtest calculations, comparison eligibility, persisted historical results, market evidence, live parameters, or portfolio state.
- Automatic backtests, experiment approval, optimization/grid search, or Strategy activation.
- Generating Strategy Skill source. A new methodology remains a research concept until separately specified and implemented.
- Declaring winners across incompatible evidence, inferring missing data, or claiming future returns.
- Changing GH-13's existing one-security copilot route, GH-15's approval contract, or any other tab's agent surface.

## Repository evidence and architecture fit

Reviewed the current origin/main baseline 2cbe385f and:

- GH-20 and its planning comment: landing view only, with _strategy_manager.html and GET /partials/strategy-manager as the primary seams; preserve existing content/routes.
- docs/agentic-ai-contextual-concept.html: toolbar, experiment block and adjacent copilot panel.
- app/api/routes/strategy_manager.py and app/api/templates/_strategy_manager.html: prerequisites, setup/coverage states, provenance, adaptive primary CTA, and _backtest_results_list.html already render on the landing.
- app/repositories/backtest_repo.py: verified result reads, metric availability, candidate-audit summaries, experiment reads, and canonical is_comparable.
- app/services/backtest/metrics.py and result_presenter.py: fixed four-key metric contract; unavailable values have explicit reasons.
- GH-15's completed plan/spec and StrategyExperimentService: validated proposals, digest-bound approval and idempotent candidate enqueue. Its Claude-first/local Foundry provider attempt pattern is available to follow.
- GH-13's completed spec and app/agents/research/copilot.py: the current question/evidence contract is one security at a time; its route does not accept Strategy/Backtest context.
- GH-19 and GH-21 specs: additive, read-only surfaces, token-based layout, cited evidence, truthful unavailable states, agent boundaries.
- ARCHITECTURE-SPINE.md, especially AD-1 through AD-3, AD-8, AD-19, AD-20, AD-30/31 and the bounded-read/deterministic-result invariants.

This is an additive presentation and research feature over verified Backtest Results. It does not require a spine change. Reuse the repository comparator rather than adding a second eligibility rule. Keep provider-neutral insight/evidence models between repositories/routes and Claude. Do not broaden GH-13; add a Strategy-scoped evidence adapter for GH-20.

## Product and technical decisions

1. **Meaning of “worked”.** Report measured return/risk trade-offs within a clearly identified comparable cohort. Do not create an arbitrary pass/fail threshold or opaque blended score. Say which tested configuration led on each available measure and what trade-off it carried.
2. **Comparable cohort.** Consider at most the 25 most recent complete, verified, non-tombstoned result candidates. Use BacktestRepository.is_comparable for eligibility. Select the cohort with the most distinct Strategies; tie-break by newest completion time, then stable run-ID order. Keep the newest result per Strategy in that cohort. Report coverage and exclusion reasons. Without a cross-Strategy cohort, show individual outcomes and no cross-Strategy winner.
3. **Valuation graph.** Plot stored equity paths locally for the selected cohort. Index curves to a common starting value when starting capital differs; label period/base currency and retain Result links plus an accessible table. Never send the curve to Claude.
4. **Evidence boundary.** Claude receives a locally built structured summary: Strategy ID, needed declared parameter values, opaque local evidence handles, compatible run period/universe/currency/provenance, persisted aggregate metrics and availability, sample counts, and bounded aggregate reason counts. Omit daily prices, full event/trade/equity-curve histories, complete manifests, database/filesystem details, secrets, and personal portfolio data.
For copilot Q&A, also send the user's submitted question and only the relevant Strategy fields and bounded result summary; disclose this on the panel.
5. **Provider.** Claude is primary for generated insights. Follow GH-15 provider-attempt conventions and its local Foundry fallback if the shared seam supports it. Record provider/model, all attempt outcomes, input digest, prompt/schema version, citations and timestamp. Provider calls require an explicit user action, run outside the event loop, and do not occur on normal landing GET.
6. **Persistence.** Save each validated report by the digest of its exact summary and prompt/schema version. Reuse the exact report on revisit. Refresh is explicit; a failed/new attempt must not replace the last good report or audit identity.
7. **Strategy copilot.** Add a separate bounded Strategy Q&A path. Keep current GH-13 one-security behavior unchanged. Model output has no tools or mutation capability; question, evidence digest, answer, citations and provider identity are locally auditable.
8. **Experiment handoff.** A compatible one-declared-parameter idea may be explicitly opened in GH-15's editable draft flow. New methodologies remain text-only research ideas. Only GH-15 explicit approval can enqueue.
9. **Failure states.** Keep deterministic summaries visible when backtests, Claude, Foundry or structured output are unavailable. Missing/corrupt/ineligible data is named, never replaced by a fabricated zero or sample idea.
10. **Layout.** Retain landing status, setup/coverage/provenance and result list. Use existing tokens; no literal page palette or rounded structural containers. Add keyboard-accessible responsive curve/table, experiment state, citations, and agent/data-use boundary text.

## Functional requirements

| ID | Requirement |
| --- | --- |
| FR1 | Preserve current landing content, adaptive CTA, and all sub-screen route contracts. |
| FR2 | Build a deterministic bounded summary from verified completed results using canonical repository comparability. |
| FR3 | Show a local normalized valuation curve, four persisted metrics, availability, tested setup, sample counts, provenance and source links. |
| FR4 | Explain observed facts with citations; label causal readings and new Strategy ideas as hypotheses. |
| FR5 | Send only user-approved aggregate outcome context to Claude; validate evidence handles and persist provider provenance. |
| FR6 | Answer Strategy questions through a separate bounded read-only evidence path without changing GH-13's API. |
| FR7 | Show the actual GH-15 draft state and allow an explicit validated single-parameter handoff; no landing action queues a job. |
| FR8 | Render honest empty, unavailable, partial, stale, corrupt and no-compatible-cohort states. |

## Non-functional requirements

- Landing GET stays local and bounded: no model/network request and no unbounded result/event-history traversal.
- Each insight context considers at most 25 verified outcome candidates with a documented maximum output size. Never fetch prices or rerun a Strategy to explain a stored Result.
- Run provider calls in a threadpool/worker boundary with a timeout. A slow provider cannot block landing rendering.
- Reuse cached reports only for the exact input plus prompt/schema version; deduplicate concurrent generation for the same digest.
- Store audit metadata without copying raw histories into model-request/report records.
- Keep summary/citations reproducible from immutable Results. Automated tests mock Anthropic and Foundry.
- Add no runtime dependency; use current provider clients and design tokens.

## Ordered implementation stories

### Story 1 — GH-20.1 / #101: Summarize comparable Strategy backtests

**Value:** See recent outcomes, valuation paths, important metrics, and the next action on one landing view.

**Scope:** Add a bounded verified-result projection, latest comparable cohort selection, local valuation graph, metric/evidence summary, coverage state, and actual GH-15 proposal/empty state. Preserve the existing CTA, results list, readiness/coverage notices, provenance and routes.

**Out of scope:** Model calls, comparator changes, recomputed metrics, sub-screen edits or generated explanations.

**Acceptance criteria:**

- Given verified completed results, when the summary is built, then only runs accepted by the canonical comparator share a cohort; selection is deterministic and each displayed fact links to its Result.
- Given incompatible periods/profiles/evidence/currencies/contracts, corrupt Results, deleted runs, or failed/cancelled jobs, when a cohort is built, then invalid candidates are excluded and the view reports coverage/exclusion reasons without treating them as winners.
- Given one Strategy or no eligible results, when the landing view renders, then it shows individual/no-result status with no cross-Strategy winner and retains the current CTA.
- Given null metrics or insufficient samples, when the summary renders, then it uses persisted availability reasons and never substitutes zero.
- Given a compatible cohort, when the curve renders, then it uses local stored equity points, labels period/currency, normalizes for display only if starting capital differs, and has an accessible table alternative.
- Given a pending GH-15 draft, when the landing view renders, then its real hypothesis/status/locked facts appear with Review and Discard; no sample is shown and no job is enqueued.
- Existing landing content, sub-screen templates and route behavior remain intact.

**Likely seams:** BacktestRepository bounded reads/is_comparable/candidate audit; BacktestMetricsV1; pure Strategy outcome schema/service; strategy_manager.py landing context; _strategy_manager.html and a summary/draft include.

**Tests/gates:** Bounded result selection and integrity; canonical eligibility and deterministic tie selection; metric availability, coverage, source identity; existing CTA and empty/readiness states; browser curve accessibility/responsiveness and draft links; affected tests, Ruff, Pyrefly, format, full suite.

**Dependencies:** GH-13 #13 and GH-15 #15 are complete. Foundation for all other stories.

### Story 2 — GH-20.2 / #102: Generate cited Claude insights

**Value:** Understand what the tested results suggest and see evidence-based questions/ideas worth exploring.

**Scope:** Provider-neutral insight contract; Claude-first structured generation from Story 1's sanitized summary; local citation validation; digest-keyed report/attempt persistence; explicit Generate/Refresh action and data-use disclosure.

**Out of scope:** Model-controlled source selection, direct repository/tools access, browsing, backtest mutation, Strategy source generation, unsupported causal claims.

**Acceptance criteria:**

- Given a new summary digest, when the user requests insights, then only the allowlisted structured summary is sent and the response contains observations, valid evidence handles, limitations and testable hypotheses.
- Given malformed schema, unknown evidence IDs, unsupported result identities/numbers, refusal, truncation or provider failure, when validated, then output is rejected and the deterministic summary remains.
- Given survivor bias, small samples, differing tested parameters or missing comparable benchmark evidence, when described, then those limitations remain visible and no future-performance claim is made.
- Given a new concept not representable as one current Strategy parameter, when returned, then it is research-only text without executable source.
- Given the same summary/prompt/schema digest, when revisited, then the cached report is reused; Refresh is explicit and landing GET makes no provider call.
- Given Claude or local fallback is unavailable/invalid, when the action fails, then the insight panel states unavailable while factual data remains.
- Given any provider attempt, when audited, then provider/model/outcome, digest, prompt/schema version, timestamp and accepted citations are recorded.

**Likely seams:** New Strategy insight schema/service/agent; GH-15 provider-attempt pattern; additive BacktestRepository report/audit storage; dependencies.py; insight partial and action route.

**Tests/gates:** Allowlist with no curves/events/full manifests/secrets; schema/citation validation; fallback attempts; digest cache/concurrency; no call on GET; caveats/unavailable states; mocked-provider tests.

**Dependencies:** Story 1. Freeze evidence/citation handles for Story 3.

### Story 3 — GH-20.3 / #103: Ask a bounded Strategy-scoped copilot

**Value:** Ask follow-up questions about the current Strategy setup and comparable outcomes without leaving the tab.

**Scope:** Add a Strategy-context evidence builder, read-only Q&A service/route and accessible panel. Reuse local evidence handles and GH-13 citation/unknown patterns while keeping /copilot/ask unchanged.

**Out of scope:** Portfolio-wide advice, security qualification, arbitrary tools, model file/database access, web research, multi-session memory, Strategy execution.

**Acceptance criteria:**

- Given a question and valid Strategy evidence, when submitted, then the copilot answers from the supplied config/results only and returns valid citations plus unknowns.
- Given absent/stale/corrupt/incompatible evidence or a question outside the snapshot, when asked, then it says what is unknown rather than filling gaps from model knowledge.
- Given invalid citation/schema, refusal or provider error, when validated, then no unsupported answer is rendered and the panel offers a bounded unavailable/retry state.
- Given untrusted text in a hypothesis/event explanation, when included, then it is treated as data, not instructions.
- Given a Q&A request, when processed, then it cannot mutate portfolios/configuration/experiments/jobs; audit records question, context digest, provider/model, citations and outcome.
- Existing GH-13 single-security and GH-19 Portfolio copilot behavior remains unchanged.

**Likely seams:** Strategy evidence schema/builder/copilot service and additive route; dependencies.py; reusable _strategy_manager.html panel; copilot citation conventions in app/agents/research/copilot.py and app/schemas/copilot.py.

**Tests/gates:** Context allowlist, citations, injection fixture, missing/stale evidence, unsupported questions, provider failure, audit identity, read-only snapshot checks, #13/#19 regressions, keyboard/browser checks.

**Dependencies:** Story 1. Can run alongside Story 2 after the shared evidence/citation contract is fixed. Architecture assumption: a dedicated Strategy route satisfies #20 while preserving GH-13's one-security contract.

### Story 4 — GH-20.4 / #104: Hand off a chosen insight to GH-15 safely

**Value:** Take a supported insight into the existing controlled experiment flow without silently changing a Strategy or launching a backtest.

**Scope:** Explicit prefill/handoff for a one-declared-parameter idea and display of actual GH-15 draft/review state.

**Out of scope:** Executable source, multi-parameter changes, automatic drafts from model output, alternate enqueue paths or live activation.

**Acceptance criteria:**

- Given a compatible one-parameter hypothesis, when the user selects Explore, then the existing editable GH-15 draft form opens with hypothesis/Strategy/baseline suggested; rendering the insight alone creates no draft or job.
- Given a new methodology, unsupported parameter, changed Strategy source or stale baseline, when selected, then it stays research-only or is rejected by GH-15 validation.
- Given a submitted draft, when created, then GH-15 validation and audit/provenance remain authoritative.
- Given a candidate, when a user proceeds, then GH-15's existing digest-bound explicit approval remains required and enqueues at most one candidate.
- No landing action modifies active Strategy or portfolio state.

**Likely seams:** Typed insight-to-draft adapter; GH-15 StrategyExperimentService and existing draft/review routes; Strategy Manager proposal include.

**Tests/gates:** Supported/research-only ideas, prefill, no action on render, stale/invalid input, explicit approval, idempotent enqueue and no live state mutation.

**Dependencies:** Stories 1 and 2 plus complete GH-15.

## Traceability

| Requirement | Story | Main proof |
| --- | --- | --- |
| FR1 landing and route compatibility | 1, 3, 4 | Existing CTA/routes and sub-screen regression |
| FR2 bounded canonical comparison | 1 | Repository/cohort tests |
| FR3 graph, metrics and provenance | 1 | Accessible curve, unavailable metrics, source identity |
| FR4 evidence versus hypothesis | 2, 3 | Structured output and citation validation |
| FR5 Claude allowlist/provenance | 2 | Payload, attempt and cache tests |
| FR6 Strategy Q&A | 3 | Read-only cited-answer tests and #13 regression |
| FR7 safe experiment handoff | 1, 4 | Draft visibility and approval-only enqueue |
| FR8 fail-soft states | 1, 2, 3 | Empty/integrity/stale/provider failure cases |

## Rollout and verification

Implement story order. Backtest Result records remain immutable. If reports are persisted, use an additive/idempotent insights table. The landing view remains available if local result summaries or any provider are unavailable. Run each story's focused repository/service/route/browser tests, Ruff, Pyrefly and format checks; before release run the full repository suite and validate BMAD YAML. Mock all providers; no live backtests/provider calls are part of ordinary tests.

## Tracking

- Parent feature: [GitHub #20](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/20)
- GH-20.1: [#101](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/101)
- GH-20.2: [#102](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/102)
- GH-20.3: [#103](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/103)
- GH-20.4: [#104](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/104)

## Explicit assumption

The Strategy-scoped copilot described in #20 needs Strategy configuration and Backtest evidence, while the completed GH-13 route only accepts one-security evidence. Plan a dedicated bounded Strategy route and preserve the existing GH-13 contract.
