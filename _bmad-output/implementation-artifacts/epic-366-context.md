# Epic 366 Context: Buy and Hold Top-X Strength Basket

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Make Buy and Hold a reproducible passive-momentum benchmark: configure a positive top-X count, rank the point-in-time Run universe once at the start of a Backtest, buy the strongest eligible basket using the shared equal-capital rule, and hold it. The completed Result must make the initial selection and every exclusion auditable while preserving exact replay from pinned evidence and compatibility with historical Results.

## Stories

- Story 366.1: Record one initial ranked entry selection
- Story 366.2: Rank and buy the strongest top-X basket
- Story 366.3: Configure and explain the top-X basket

## Requirements & Constraints

- Buy and Hold has a `top_x` positive-integer parameter, defaulting to 10. It must use shared discovery/parameter validation, show a clear description and inline field errors, and not affect other Strategy forms.
- Perform selection once, on the first normalized simulation session. Rank every Run-universe member using split-adjusted close prices only: last valid close strictly before selection divided by the close 252 security sessions earlier, minus one. This requires 253 valid historical closes; invalid, non-finite, non-positive, missing, or insufficient inputs exclude only that security.
- Select `min(top_x, eligible_count)` securities by return descending then canonical security ID ascending. The calculation, decision ordering, serialization, fills, residual cash, final open-position marks, restart, and replay must be deterministic for identical pinned inputs.
- Use the shared equal-capital allocation contract; do not retain a Buy-and-Hold-specific fixed-share path or duplicate allocator arithmetic. Buy and Hold must not rerank, rebalance, pyramid, or emit ordinary sells. Existing split, dividend, and final-mark policies remain in force.
- Persist decision evidence for every Run-universe member, including selection session, metric/version and window, canonical and display identity, score/rank/state, and stable plain-language exclusion explanation. A zero-eligible run completes successfully with no positions and explains why no initial basket qualified.
- Preserve the exact configured top-X value through preparation, canonical manifest, run summary, and restart. The manifest/evidence must remain the replay authority; no live data, portfolio, broker, repository, network, or order path may be used during selection or replay.
- Historical Results with no decision evidence must remain readable and render a compatible “not recorded for this historical run” state. Existing V1/V2 manifest bytes and historical Result records must not be rewritten.
- The supported clean-checkout Strategy Manager journey must demonstrate configuration, equal-capital entries, no exits, persisted decisions, restart equality, and final open-position marks. Do not add interactive reranking, editing of completed decisions, or comparison eligibility changes.

## Technical Decisions

- Extend, rather than replace, the V1 Strategy protocol with an optional runtime-checkable initial-selection capability. It returns one complete validated batch of BUY signals and provider-neutral ranked decisions. The engine owns exactly-once invocation; ordinary V1 strategies retain their existing per-session behavior.
- Validate capability output before it can mutate simulation state: decision rows must be canonical, valid, and consistent with the signal batch. Malformed batches, duplicate decisions/ranks, invalid scores or states, mismatched sessions, future evidence, and selected/signal disagreement fail with a stable typed fatal error.
- Decision evidence follows the existing lifecycle: in-memory simulation output, atomic SQLite staging, completed-Result promotion, immutable retrieval/integrity validation, and presenter projection. New persistence fields are additive and optional for old rows.
- Use the existing bounded `MarketView.price_history` split-continuous price plane. Do not expose provider-adjusted close or create another price plane. The Strategy reads bounded history once per security; expected ranking cost is O(universe size × 253).
- Strategy parameter metadata is declared in the Skill frontmatter and consumed by shared discovery and protocol-level validation. Store parameter keys verbatim in the Run; do not create UI-specific validation rules.
- Preserve the existing layered route → service → engine/domain → repository boundaries and deterministic Decimal conventions. Result presentation projects persisted evidence and must not derive or refetch decision data.

## UX & Interaction Patterns

- Use the existing Strategy Manager discovery-driven configuration flow: selected Strategy, Run security, period/capital/currency, remaining parameters, preparation summary, then launch. Parameter controls are labelled inputs with bounds, defaults, descriptions, and field-level validation.
- In a completed Result, present the initial basket evidence prominently in deterministic order. Show the selection session, metric name/version, 252-session window, identity, trailing return, rank, and selected/excluded state; use readable exclusion messages rather than internal reason codes.
- Keep the standard Result hierarchy and visual system. Use explicit text alongside status/performance color, tabular numeric styling for dates/counts/percentages, and accessible empty/error states. Do not make an all-excluded basket look like an engine failure.
- When a persisted security cannot be resolved for display, use the compatible fallback: `Unknown security` plus its canonical ID. Do not make the Result unreadable because historical identity projection fails.

## Cross-Story Dependencies

- Story 366.1 supplies the optional one-shot selection seam and persisted decision evidence that Stories 366.2 and 366.3 consume.
- The separate #368 shared equal-capital allocation contract is required before Story 366.2 can ship; Buy and Hold must consume it rather than implement allocation locally.
- Story 366.2 implements the Buy-and-Hold ranking and basket behavior that Story 366.3 configures, preserves through launch/restart, and presents. Story 366.3 depends on both #369 and #370.
- Release requires #368 and all three #366 stories, clean migration from an existing database, historical manifest/Result compatibility, regressions across all production Strategies, the clean-checkout journey, and repository quality gates.
