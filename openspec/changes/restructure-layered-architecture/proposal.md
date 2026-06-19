## Why

The codebase has grown organically into a working but layer-less shape. Two
structural problems make it hard to change safely:

1. **No data-access boundary.** Raw `sqlite3` against `trades.db` is opened in
   four places — `agents/trader/trader_agent.py`, `agents/alert/alert_agent.py`,
   `agents/analyst/analyst_agent.py`, and `agents/trader/ingestion/sipp.py`. Each
   site independently knows the schema. The web layer (`web/app.py`) also
   instantiates `TraderAgent` directly and reaches into raw SQL via it. There is
   no place to change persistence without touching business logic.

2. **The pipeline contract is `Any`.** `ms_agent_framework.AgentApp` chains
   `scanner → analyst → alert` by passing each stage's output to the next as
   `Any`. The composition only works because the runtime types happen to line
   up; a mis-wiring is a runtime surprise, not a type error. The wiring itself
   is buried in `orchestrator.py` alongside ~600 lines of Excel formatting.

This change introduces the missing layers (`repositories`, `services`, `core`)
and replaces the untyped pipeline with a statically-typed linear `Pipeline`,
aligning the project to a conventional layered architecture without adopting an
ORM (the project is Pydantic-first, not ORM-backed).

## What Changes

- Introduce `app/core/config.py` as the single owner of all filesystem paths and
  env access, removing the `Path(__file__).parent…` chains and the `sys.path`
  hack in `web/app.py`.
- Introduce `app/repositories/` to own every `trades.db` access behind typed
  repositories (`trades_repo`, `cash_flows_repo`, `price_cache_repo`,
  `artifacts_repo`); one owner of the SQL schema.
- Introduce `app/services/` (`trader_service`, `portfolio_service`,
  `pipeline_service`) so the web layer and the scheduler both go through
  services instead of touching agents or SQL directly.
- Split `models.py` into `app/schemas/` (Pydantic, **not** ORM); leave a
  re-export shim for backward compatibility.
- Replace `ms_agent_framework.AgentApp` with a typed linear `Pipeline` in
  `app/workflows/`. The stage contract is enforced statically via a `.then()`
  builder; agents stay pure `run(payload) -> result` and never import the
  pipeline or each other. **Validation is static-only** (no runtime Pydantic
  re-validation at stage boundaries).
- Rename the scattered external data clients (`alpha_vantage_client`,
  `congress_client`, FMP/yfinance helpers) into `app/integrations/`.
- Keep the existing top-level `skills/` directory unchanged — it holds Claude
  Code skill packages and is a different concept from the proposal's
  "agent-invokable tools" (which here are `integrations/`).

## Capabilities

### New Capabilities

- `layered-architecture`: A layered package structure with explicit dependency
  rules (`api`/`orchestration` → `services` → `agents` → `integrations`, plus
  `services`/`agents` → `repositories`), a centralized config/path owner, and a
  repository layer that is the sole owner of `trades.db` access.
- `workflow-pipeline`: A statically-typed linear `Pipeline` construct that
  composes pipeline-stage agents through thin Step adapters with a compile-time
  contract, replacing the untyped `AgentApp`.

### Modified Capabilities

- `system-architecture`: Existing architecture spec updated to describe the
  layered structure and the dependency-flow rules.

## Impact

- **Code (new):** `app/core/config.py`, `app/core/security.py`,
  `app/repositories/*`, `app/services/*`, `app/schemas/*`,
  `app/workflows/pipeline.py`, `app/workflows/momentum.py`,
  `app/integrations/*`, `app/main.py`.
- **Code (moved/refactored):** `web/app.py` → `app/api/` (thin routes +
  dependencies); `agents/**` → `app/agents/**`; `orchestrator.py` →
  `app/orchestration/orchestrator.py`; `ms_agent_framework.py` →
  `app/workflows/pipeline.py`; `models.py` → `app/schemas/*` (with shim).
- **Behaviour:** No user-facing behaviour change. Web endpoints, the scheduled
  pipeline, SIPP import, and outputs (CSV/JSON/Excel) remain functionally
  identical. This is a structural refactor verified by the existing test suite
  staying green after each phase.
- **Unchanged:** top-level `skills/` (Claude Code skill packages),
  `scripts/`, data files, `trades.db` schema and contents.
