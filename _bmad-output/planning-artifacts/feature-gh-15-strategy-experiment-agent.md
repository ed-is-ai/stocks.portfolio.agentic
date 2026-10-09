---
status: confirmed
created: 2026-10-09
method: bmad-dev-auto
github_issue: 15
repository: ed-is-ai/stocks.portfolio.agentic
completed_prerequisite_issues: [13, 33]
---

# Feature GH-15 — Strategy experiment agent

## Feature contract

### Problem

Strategy Manager can launch and compare backtests, but it has no controlled way to turn a research hypothesis into a one-parameter candidate run against a fixed baseline. Reconfiguring a second run by hand can change the dates, universe, or evidence and make the comparison misleading.

### Outcome

A user can submit a hypothesis against a completed baseline, review a typed single-parameter proposal, and explicitly approve one candidate run. The candidate copies the baseline's verified immutable run manifest and changes only the approved Strategy parameter. The application records the complete experiment and reports a deterministic historical comparison without activating or publishing the candidate.

## Confirmed scope

- Parameter-only variants of one currently discovered Strategy; one changed Strategy-declared parameter per experiment.
- A completed, integrity-verified baseline run is the sole source of Strategy identity, parameters, capital/currency, universe, dates, and evidence pins.
- The structured model proposal is untrusted input. Local code validates it against the Strategy schema and rejects engine-owned, unknown, multiple, wrong-type, or out-of-range changes.
- A draft also names one canonical result metric and expected direction. This is shown for explicit user approval. A strict movement in that direction is **supported**; strict movement in the opposite direction is **contradicted**; equal/unavailable metrics or invalid/ineligible runs are **inconclusive**. Always show closed-trade counts and evidence limitations; make no statistical-significance or future-performance claim.
- Drafting writes an immutable plan and audit event but never enqueues. The approval action binds to the stored draft digest and atomically enqueues one durable candidate. Retries return the same candidate.
- Reuse the existing durable job worker and `BacktestRepository.is_comparable`/verified result path. Candidate manifest construction copies the verified baseline manifest and changes only the validated parameter value; it must not rebuild evidence from the currently active profile.
- Persist experiment status, baseline/candidate IDs, model/draft data, approval, conclusion, and append-only audit events. Failed, cancelled, corrupt, or ineligible results conclude as inconclusive.
- Provide list/detail data and the review/approval dialog for future contextual use. The Strategy Manager landing-page proposal surface is GH-20 and remains out of this feature.

## Decisions and constraints

1. **Prerequisites are met.** GitHub #13 (research copilot / shared evidence reference) and #33 (position cap) are closed. #15 planning notes require #33 first and the Sprint 4 LLM/evidence decisions; current code contains the pinned run manifest versions, `EvidenceRefV1`, typed parameter validation, Anthropic structured output, and canonical comparison service.
2. **One outcome metric makes the verdict reproducible.** The model must return one supported metric key from `BacktestMetricsV1` and one expected direction. The model never determines the final verdict. Equal values and unavailable metrics are inconclusive. Any zero-closed-trade side is also inconclusive; otherwise show both sample counts as context without inventing a statistical threshold.
3. **Exact baseline reuse.** `BacktestLaunchService.launch()` rebuilds from the active profile and current evidence, so experiment approval must use a verified copy of the stored baseline manifest. A candidate must preserve every manifest field except its one Strategy parameter and keep the baseline as its durable parent.
4. **Approval is an explicit boundary.** The dialog states the job write and how its result will be verified. It is keyboard-operable and returns focus to its trigger. Creation, discard, and read routes cannot enqueue jobs.
5. **No live mutation.** Experiment output remains a historical result. It cannot assign parameters to a portfolio, publish a Skill, change a Strategy, or launch another experiment automatically.
6. **Claude-first proposal generation.** On 2026-10-09 the user directed GH-15 to use Claude by default and explicitly approved sending the hypothesis, Strategy ID, declared parameter definitions, and current parameter values to Anthropic. The baseline run ID and manifest, Strategy source, other run inputs, and historical results stay local. Claude uses model `claude-sonnet-5`; fixed-loopback Foundry Local at `http://localhost:5272/v1` is tried when Claude is unconfigured, unavailable, or cannot return schema-valid output. A schema-valid response that fails local Strategy parameter validation is rejected without a draft. Persist and display the attempted providers and outcomes alongside the provider/model that supplied the saved proposal.

## Implementation stories

### Story 1 — GH-97: Bound and persist strategy experiment drafts

Accept a hypothesis and a completed baseline ID; ask Claude by default, with Foundry Local as fallback, to propose one declared parameter change, a plain-language effect, and one metric/direction. Validate locally, pin the verified baseline manifest, persist the draft plus audit event, and expose list/detail reads. If no schema-valid proposal is available, the selected proposal fails Strategy parameter validation, or the baseline is invalid, create no draft or job.

### Story 2 — GH-98: Approve and launch the exact strategy experiment

Show all locked baseline facts and the exact write in an accessible approval dialog. Revalidate and bind approval to the draft digest, allow discard, and enqueue one candidate through the durable lifecycle using the exact baseline evidence. Make concurrent approval idempotent.

### Story 3 — GH-99: Compare strategy experiments and audit the conclusion

After the candidate job is terminal, verify both runs, enforce experiment-specific identity and manifest equality, then use canonical comparison eligibility. Record supported/contradicted/inconclusive, exact metrics, sample counts, limitations, provenance, and an immutable conclusion audit event.

## Acceptance traceability

| GH-15 requirement | Story |
|---|---|
| Only declared Strategy parameters can change | #97, #98 |
| Same immutable baseline evidence and full provenance | #97, #98, #99 |
| No changing universe or dates after viewing results | #97, #98 |
| Failed/ineligible runs are inconclusive | #99 |
| No live portfolio/Strategy activation | #98, #99 |
| Plan, approval, run IDs, comparison, and conclusion audited | #97, #98, #99 |

## Quality gates

- Test parameter type/range/enum validation, one-key-only changes, and rejection of host-owned values.
- Prove V1/V2/V3 candidate manifests preserve all baseline evidence and identity fields, change only the chosen parameter, and remain canonically comparable.
- Test draft refusal/unavailability, malformed structured output, audit writes, and the invariant that draft creation never queues.
- Test approval stale-digest rejection, discard, duplicate/concurrent approval, and exactly one queued candidate.
- Test automatic terminal reconciliation for success, failure/cancellation, missing/corrupt evidence, ineligibility, metric unavailability, and zero trades.
- Test the approval dialog's accessibility/keyboard contract and regression-test existing launch, compare, and strategy landing routes.
- Run the focused backtest/strategy-manager suites, then repository lint/type/test checks.

## GitHub tracking

- Feature: [#15](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/15)
- Draft story: [#97](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/97)
- Approval story: [#98](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/98)
- Conclusion story: [#99](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/99)
- Follow-on contextual Strategy Manager landing view: [#20](https://github.com/ed-is-ai/stocks.portfolio.agentic/issues/20)
