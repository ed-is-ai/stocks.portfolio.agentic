---
title: 'Namespace backtest Strategy Skills under rtly-backtest'
type: 'refactor'
created: '2026-08-20'
status: 'done'
baseline_commit: '4732490'
review_loop_iteration: 0
context:
  - '{project-root}/docs/strategy-manager/strategy-authoring-v1.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-backtest-strategy-skills.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The six production backtest Strategy Skills use generic names that do not identify their RTLY ownership or distribution namespace.

**Approach:** Rename every skill and stable strategy ID from `<methodology>-backtest` to `rtly-backtest-<methodology>`, updating all metadata, runtime identities, tests, documentation, BMAD tracking, and GitHub issue titles without changing trading behavior.

## Boundaries & Constraints

**Always:** Apply this exact mapping: `buy-and-hold-backtest` → `rtly-backtest-buy-and-hold`, `darvas-box-backtest` → `rtly-backtest-darvas-box`, `minervini-backtest` → `rtly-backtest-minervini`, `moving-average-backtest` → `rtly-backtest-moving-average`, `turtle-trend-backtest` → `rtly-backtest-turtle-trend`, and `weinstein-backtest` → `rtly-backtest-weinstein`; keep each folder name, SKILL frontmatter `name`, runtime `STRATEGY_ID`, `$skill` prompt, discovery expectation, and tracking key identical; preserve unrelated worktree changes.

**Ask First:** Add compatibility aliases or migrate persisted backtest records that reference the old IDs; change display names, parameters, trading rules, protocol, discovery behavior, or GitHub issue numbers.

**Never:** Leave duplicate discoverable old-name skills, modify strategy calculations, rewrite unrelated UI/notification work, or silently retain stale old-name references in maintained artifacts.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Discovery | Live `skills/` root | Six renamed IDs appear in deterministic order with unchanged defaults | Old six IDs are absent |
| Worker load | Each renamed descriptor | Production loader instantiates exactly one conforming runtime | Any folder/frontmatter/runtime mismatch fails tests |
| Skill invocation | `$rtly-backtest-<methodology>` | Matching `agents/openai.yaml` prompt resolves the renamed Skill | No prompt retains the old `$` name |
| Existing old ID | Previously stored reference | No alias is created in this refactor | Migration requires separate approval |

</frozen-after-approval>

## Code Map

- `skills/rtly-backtest-{buy-and-hold,darvas-box,minervini,moving-average,turtle-trend,weinstein}/` -- renamed Skill directories and their identity metadata.
- `tests/backtest/test_skill_discovery.py` -- authoritative live ID/default/order and worker-load coverage.
- `_bmad-output/implementation-artifacts/{spec-backtest-strategy-skills.md,github-bmad-tracking.yaml}` -- maintained implementation and external issue mapping references.
- `docs/strategy-manager/strategy-authoring-v1.md` -- naming examples remain generic and require no semantic change unless stale paths are found.

## Tasks & Acceptance

**Execution:**
- [x] `skills/*-backtest/` -- move all six directories to `skills/rtly-backtest-*/` and update SKILL names, runtime IDs, test assertions, commands, and `$skill` prompts.
- [x] `tests/backtest/test_skill_discovery.py` -- replace expected IDs while preserving defaults, deterministic order, warning isolation, and production worker loading.
- [x] `_bmad-output/implementation-artifacts/` -- update maintained paths and tracking keys, and record this namespace migration.
- [x] Repository -- search maintained text for stale old identifiers and run discovery, worker, contract, lint, format, scoped runtime/integration typing, and diff checks.

**Acceptance Criteria:**
- Given the live skills root, when discovery runs, then the six `rtly-backtest-*` IDs are returned with unchanged parameter defaults and none of the six old IDs are returned.
- Given every renamed descriptor, when the production worker loader imports it, then exactly one zero-argument `StrategyProtocolV1` implementation loads successfully.
- Given the renamed skill folders and maintained tracking, when identifiers are compared, then folder/frontmatter/runtime/prompt/test/tracking names agree exactly.
- Given identical bounded market and portfolio views, when renamed runtimes execute, then signals and sizing remain behaviorally unchanged.

**Post-review completion:**
- [x] GitHub issues `#246`–`#251` -- rename titles to the RTLY identifiers while preserving issue numbers and completion state after local review passes.

## Spec Change Log

- 2026-08-20: Renamed all six local Skill folders and maintained identities to the `rtly-backtest-*` namespace; GitHub issue title synchronization remains pending.
- 2026-08-20: Synchronized closed GitHub issues #246–#251 to the corresponding `rtly-backtest-*` identifiers without changing issue numbers or `bmad:done` state.

## Design Notes

This is an intentional breaking identity rename: source digests and persisted references containing old strategy IDs are not migrated or aliased. The six issue numbers remain stable so historical tracking continuity is preserved.

## Verification

**Commands:**
- `uv run pytest skills/rtly-backtest-*/scripts/tests tests/backtest/test_skill_discovery.py tests/backtest/test_strategy_runtime_import_boundary.py -q` -- renamed contracts, discovery, loading, and safety pass.
- `uv run ruff check <changed Python paths> && uv run ruff format --check <changed Python paths>` -- scoped lint and formatting pass.
- `uv run pyrefly check skills/rtly-backtest-*/scripts/strategy.py tests/backtest/test_skill_discovery.py tests/backtest/test_strategy_runtime_import_boundary.py` -- runtime and integration typing passes; unchanged contract-test bodies retain pre-existing pandas-stub inference errors.
- `git diff --check` -- no whitespace errors or unrelated staged files.

## Suggested Review Order

**Namespace contract**

- Start with the authoritative six-ID mapping, defaults, and legacy-ID rejection.
  [`test_skill_discovery.py:37`](../../tests/backtest/test_skill_discovery.py#L37)

- Confirm folder and frontmatter identity agree for a representative Skill.
  [`SKILL.md:1`](../../skills/rtly-backtest-minervini/SKILL.md#L1)

- Confirm runtime identity uses the renamed stable strategy ID.
  [`strategy.py:17`](../../skills/rtly-backtest-minervini/scripts/strategy.py#L17)

- Confirm invocation metadata targets the renamed `$skill` handle.
  [`openai.yaml:1`](../../skills/rtly-backtest-minervini/agents/openai.yaml#L1)

**Renamed packages**

- Review the passive benchmark package under its RTLY namespace.
  [`buy-and-hold/SKILL.md:1`](../../skills/rtly-backtest-buy-and-hold/SKILL.md#L1)

- Review the Darvas package under its RTLY namespace.
  [`darvas-box/SKILL.md:1`](../../skills/rtly-backtest-darvas-box/SKILL.md#L1)

- Review the moving-average package under its RTLY namespace.
  [`moving-average/SKILL.md:1`](../../skills/rtly-backtest-moving-average/SKILL.md#L1)

- Review the Turtle package under its RTLY namespace.
  [`turtle-trend/SKILL.md:1`](../../skills/rtly-backtest-turtle-trend/SKILL.md#L1)

- Review the Weinstein package under its RTLY namespace.
  [`weinstein/SKILL.md:1`](../../skills/rtly-backtest-weinstein/SKILL.md#L1)

**Tracking and history**

- Verify BMAD issue mappings now use the renamed stable identifiers.
  [`github-bmad-tracking.yaml:69`](github-bmad-tracking.yaml#L69)

- Preserve the original implementation record with updated live paths.
  [`spec-backtest-strategy-skills.md:45`](spec-backtest-strategy-skills.md#L45)
