## Context

The system has two entry points, not one:

- **Scheduled pipeline (the spine):** `APScheduler → orchestrator.py →
  AgentApp.execute(scanner → analyst → alert)`, writing JSON/CSV/Excel
  artifacts and `trades.db`.
- **Web UI (a view):** `FastAPI (web/app.py) → TraderAgent` instantiated at
  import, calling raw `sqlite3` for portfolio/trade state.

The generic FastAPI "standard layout" assumes a web-first CRUD app with an ORM.
This project is pipeline-first and Pydantic-first. The target structure below
adapts the convention to that reality rather than copying it verbatim.

Two concrete couplings drive the change:

1. **Persistence is spread across four agents.** `trader`, `alert`, `analyst`,
   and `ingestion/sipp` each open `trades.db` and each know the schema.
2. **The pipeline contract is erased to `Any`.** `AgentApp` passes stage output
   to the next stage as `Any`; nothing checks that `analyst` actually consumes
   `scanner`'s output type.

There is also a naming hazard: a top-level `skills/` directory already exists
and means **Claude Code skill packages** (e.g. `vcp-screener`). The proposal's
`app/skills/` ("tools agents invoke") is a different concept. We resolve the
collision by **not** reusing the word: agent-invokable external clients become
`integrations/`, and top-level `skills/` is left untouched.

## Goals / Non-Goals

**Goals:**
- Introduce explicit layers: `core`, `repositories`, `services`, `schemas`,
  plus a typed `workflows` pipeline.
- Make `repositories/` the sole owner of `trades.db` access.
- Make the web layer and the scheduler both route through `services/`.
- Replace the `Any` pipeline with a statically-typed linear `Pipeline`.
- Keep every phase independently mergeable with the test suite green.

**Non-Goals:**
- Introducing an ORM (SQLAlchemy). The project stays Pydantic + thin repos over
  `sqlite3`. `schemas/` is named honestly; there is no `models/` ORM layer.
- Branching/DAG workflows, shared mutable workflow context, or parallel stages.
  The pipeline is **linear** by decision (see Decision 5).
- Runtime re-validation of data at stage boundaries (see Decision 6).
- Changing any user-facing behaviour, endpoint, output format, or DB schema.
- Touching the top-level `skills/` Claude Code packages.

## Target structure

```
app/
├── main.py                  # entry point: `serve` (uvicorn) | `run-pipeline`
├── core/
│   ├── config.py            # ALL paths + env in one place
│   └── security.py          # require_local_or_token (from web/app.py)
├── api/                     # was web/ — thin request/response only
│   ├── app.py               # FastAPI factory
│   ├── dependencies.py      # get_trader_service / get_portfolio_service
│   ├── routes/{portfolio,trades,pipeline,views}.py
│   └── templates/           # unchanged jinja
├── schemas/                 # was models.py (split) — Pydantic, NOT ORM
│   ├── scan.py  record.py  trade.py
├── repositories/            # sole owner of trades.db + artifact files
│   ├── db.py                # connection + _SCHEMA + migrations
│   ├── trades_repo.py  cash_flows_repo.py  price_cache_repo.py
│   └── artifacts_repo.py    # CSV/JSON read/write (runs, *_results.json)
├── services/                # orchestration + business logic
│   ├── trader_service.py  portfolio_service.py  pipeline_service.py
├── agents/                  # pipeline-stage agents — pure run(payload)->result
│   ├── scanner/ analyst/ alert/ extraction/ trader/ (└─ ingestion/)
├── integrations/            # external data clients (were *_client.py)
│   ├── alpha_vantage.py  congress.py  fmp.py  yfinance_prices.py
├── workflows/
│   ├── pipeline.py          # Step / Pipeline (replaces ms_agent_framework)
│   └── momentum.py          # build_momentum_pipeline() — stage wiring
└── orchestration/
    └── orchestrator.py      # scheduling + Excel/CSV export; calls pipeline_service

skills/                      # UNCHANGED — Claude Code skill packages (top-level)
tests/{unit,integration}/
scripts/
```

### Dependency flow

```
   ┌── HTTP ──► api ──► services ──┐
   │                              ├──► agents ──► integrations (external data)
   └── cron ─► orchestration ─────┘        │
                                           └──► repositories ──► sqlite / files
```

Both entry points funnel through `services`. Agents never import each other,
never import a service, and never import `workflows`. Repositories never import
upward (no agents/services/api).

## Decisions

### 1. `core/config.py` lands first (path centralization)

Paths today are derived from `Path(__file__).parent.parent.parent` and a
`sys.path.insert` in `web/app.py`. The moment files move, every such chain
breaks. A single config module exposing absolute paths (`TRADES_DB`,
`ANALYSIS_JSON`, `PORTFOLIO_VALUE_CSV`, `PIPELINE_RUNS_CSV`, templates dir, …)
must exist before anything moves. Agents/repos read paths from config, not from
`__file__`.

### 2. `repositories/` is the sole owner of `trades.db`

`db.py` owns the connection factory and `_SCHEMA` (migrated from
`trader_agent.py`). Each table gets a repository exposing typed methods
(`TradesRepository.add`, `.delete`, `.positions`, …). The four current raw-SQL
sites call repositories instead. Agents receive a repository (constructor
injection) rather than opening their own connection.

### 3. `services/` mediate; the web layer stops touching agents/SQL

`web/app.py` instantiates `TraderAgent` at module import and calls it from route
handlers. After the change, routes depend on services via FastAPI `Depends`:
`TraderService` wraps `TraderAgent` + repositories; `PortfolioService` owns
valuation and GBP/USD conversion; `PipelineService` runs the workflow. Routes
become thin.

### 4. `schemas/` not `models/` — no ORM

`models.py` is Pydantic (`StockScan`, `StockRecord`, `MomentumScore`). It is
split into `app/schemas/` by concern. A `models.py` shim re-exports the new
locations so unmoved imports keep working during the migration.

### 5. The pipeline is a statically-typed **linear** `Pipeline`

`ms_agent_framework.AgentApp` (untyped `Any` chain) is replaced by a generic
builder whose `.then()` advances the output type while pinning the input type,
so pyrefly enforces the stage contract at the wiring site:

```python
TIn = TypeVar("TIn"); TOut = TypeVar("TOut"); TNext = TypeVar("TNext")

class Step(Protocol[TIn, TOut]):
    name: str
    def run(self, payload: TIn) -> TOut: ...

class Pipeline(Generic[TIn, TOut]):
    @classmethod
    def start(cls, step: Step[TIn, TOut]) -> "Pipeline[TIn, TOut]": ...
    def then(self, step: Step[TOut, TNext]) -> "Pipeline[TIn, TNext]": ...
    def run(self, payload: TIn) -> TOut: ...
    def run_traced(self, payload: TIn) -> tuple[TOut, list[tuple[str, object]]]: ...
```

Agents stay pure and ignorant of the pipeline. Thin **Step adapters** in
`workflows/momentum.py` declare the typed boundary and call the agent:

```python
class ScanStep:
    name = "scan"
    def run(self, payload: list[str]) -> list[StockRecord]:
        return ScannerAgent().run(payload)

def build_momentum_pipeline() -> Pipeline[list[str], AlertSummary]:
    return Pipeline.start(ScanStep()).then(AnalyseStep()).then(AlertStep())
```

Adapters (not agents-as-Steps) are used so the contract types live in
`workflows/` and agents keep loose, independent signatures. `extraction` remains
an explicit pre-step (it is conditional, not part of the linear chain).

`run_traced` preserves the per-stage intermediates that `orchestrator.py`
currently feeds to the Excel export (today via
`AgentApp.execute_with_intermediates`). Per-step timing/logging lives in
`run`/`run_traced`, giving observability without agents knowing.

### 6. Validation is static-only

The `.then()` chain is checked at type-check time. We do **not** add Pydantic
re-validation at each stage boundary: the pipeline is a trusted in-process
chain, agents already emit Pydantic models, and re-validating every hop costs
speed for little gain. Runtime boundary checks can be added later if a stage
ever ingests untrusted input.

### 7. External clients become `integrations/`, `skills/` is untouched

The proposal's `app/skills/` concept maps onto the existing external data
clients, which move to `integrations/`. The top-level `skills/` directory keeps
its existing meaning (Claude Code skill packages) and is out of scope.

## Phased plan

Each phase is independently mergeable. Run `uv run ruff format .`,
`pyrefly check`, and `uv run pytest` after each. Backward-compat shims are left
in place until the final phase removes them.

| Phase | Scope | Risk |
|-------|-------|------|
| 1. Config | `core/config.py` centralizes paths + env; replace `__file__` chains and the `sys.path` hack | Low |
| 2. Repositories | Pull the 4 raw-sqlite sites behind `repositories/*`; `db.py` owns `_SCHEMA` | Low–Med |
| 3. Schemas | Split `models.py` → `app/schemas/*`; `models.py` becomes a re-export shim | Low |
| 4. Services + thin API | Extract `TraderService`/`PortfolioService`/`PipelineService`; routes use `Depends`; auth → `core/security` | Med |
| 5. Agents / integrations / workflows / orchestration | Move `agents/` under `app/`; `ms_agent_framework` → `workflows/pipeline.py` + `momentum.py`; `*_client` → `integrations/`; orchestrator → `orchestration/`; remove shims | Med–High |

The architectural payoff is concentrated in Phases 1–4. Phase 5 is largely
import churn for convention alignment and can be deferred or dropped without
losing the layering benefits.

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| Moving files breaks `__file__`-relative paths | Phase 1 centralizes paths before any move; nothing else depends on `__file__` depth |
| Large import churn in Phase 5 | Phase 5 is optional; shims keep old import paths working until it lands. A flatter variant (add layers at repo root, skip the `app/` wrapper) captures Phases 1–4 with far less diff |
| Repository extraction changes agent behaviour subtly | Existing agent tests must stay green per phase; repos get dedicated unit tests |
| `Pipeline` generics confuse pyrefly | `.then()`/`start` pattern is standard; keep `Step` a simple `Protocol`; `run` uses a single localized `type: ignore` on the final cast |
| Two meanings of "skill" reintroduced later | Decision 7 fixes the vocabulary: external clients are `integrations/`; `skills/` stays Claude Code packages |
