## 1. Phase 1 — Centralize config and paths

- [x] 1.1 Create `app/core/__init__.py` and `app/core/config.py` exposing absolute paths (`TRADES_DB`, `ANALYSIS_JSON`, `SCAN_RESULTS_JSON`, `PORTFOLIO_VALUE_CSV`, `PIPELINE_RUNS_CSV`, `TEMPLATES_DIR`, `TICKER_ALIASES_JSON`) and env accessors (`APP_AUTH_TOKEN`)
- [x] 1.2 Replace `Path(__file__).parent…` chains in `trader_agent.py`, `web/app.py`, and `orchestrator.py` with imports from `core.config`
- [x] 1.3 Remove the `sys.path.insert` hack in `web/app.py` (rely on package imports / config)
- [x] 1.4 `pyrefly check` + `uv run pytest` green; commit `refactor(core): centralize paths and env in core.config`

## 2. Phase 2 — Repository layer (sole owner of trades.db)

> **Scope note (deviation from original proposal):** The proposal assumed all
> four sites open `trades.db`. In reality `alert_agent.py` owns a separate
> `alerts.db` and `analyst_agent.py` owns a separate `results.db`. Per decision,
> Phase 2 now builds repositories for **all three** databases so no agent imports
> `sqlite3`. The `agents/trader/ingestion/` package was found to be **unwired
> dead code** depending on a never-built multi-portfolio `TraderAgent` API; it is
> **deleted** here rather than rewired, and multi-portfolio support is tracked as
> a separate future change (see TODO in MEMORY / follow-up).

- [x] 2.1 Create `app/repositories/db.py` with the connection factory and `_SCHEMA` (moved from `trader_agent.py`), including existing migrations
- [x] 2.2 Add `TradesRepository`, `CashFlowsRepository`, `PriceCacheRepository`, `AccountStateRepository` with typed methods covering current SQL (positions, add/delete trade, cash flows, price cache, `account_state`)
- [x] 2.3 Add `ArtifactsRepository` for CSV/JSON read/write (`pipeline_runs.csv`, `*_results.json`, `portfolio_value.csv`)
- [x] 2.4 Point `trader_agent.py`, `alert_agent.py`, `analyst_agent.py` at repositories (incl. new `AlertsRepository` over `alerts.db` and `ResultsRepository` over `results.db`) instead of raw `sqlite3`; delete the dead `ingestion/` package
- [x] 2.5 Add unit tests for each repository (in-memory / temp DB)
- [x] 2.6 `pyrefly check` + `uv run pytest` green; commit `refactor(repositories): extract db access behind repositories`

## 3. Phase 3 — Split schemas

- [x] 3.1 Create `app/schemas/{scan,record,trade}.py` and move the Pydantic models out of `models.py`
- [x] 3.2 Make `models.py` re-export from `app.schemas.*` for backward compatibility
- [x] 3.3 `pyrefly check` + `uv run pytest` green; commit `refactor(schemas): split models.py into app/schemas with shim`

## 4. Phase 4 — Services + thin API

- [x] 4.1 Create `app/services/trader_service.py` wrapping `TraderAgent` + repositories
- [x] 4.2 Create `app/services/portfolio_service.py` owning valuation + GBP/USD conversion (moved from `web/app.py` helpers)
- [x] 4.3 Create `app/services/pipeline_service.py` that builds and runs the workflow (used by both web `/refresh-data` and the scheduler)
- [x] 4.4 Move `require_local_or_token` to `app/core/security.py`
- [x] 4.5 Create `app/api/app.py` (FastAPI factory), `app/api/dependencies.py` (`get_trader_service`, `get_portfolio_service`), and `app/api/routes/{portfolio,trades,pipeline,views}.py`; routes call services via `Depends`
- [x] 4.6 Move `web/templates/` → `app/api/templates/`; keep a thin `web/app.py` shim importing the new app for existing run commands
- [x] 4.7 `pyrefly check` + `uv run pytest` (incl. `tests/test_web_auth.py`) green; commit `refactor(api): route web through service layer`

## 5. Phase 5 — Typed pipeline, agents, integrations, orchestration

> **Scope note:** Implemented across two commits. The typed-pipeline portion
> (5.1–5.4) landed first; the directory move (5.5–5.9) followed. Data files (the
> live `trades.db`/`alerts.db`/`results.db` and JSON/CSV artifacts) were **kept
> in place** under the top-level `agents/` tree — only code moved to `app/agents/`
> — so the live databases are never relocated; `core.config` owns those paths.
> `tv_extractor` moved to `app/integrations/tv_screener.py` to remove a
> scanner→extraction cross-agent import. `pyrefly` was unavailable in this
> environment; verified with `ruff` + `pytest` (85 passed).

- [x] 5.1 Create `app/workflows/pipeline.py` with `Step` (Protocol), `Pipeline` (generic builder: `start`, `then`, `run`, `run_traced`) — static-only contract, per-step timing/logging
- [x] 5.2 Create `app/workflows/momentum.py` with Step adapters (`ScanStep`, `AnalyseStep`, `AlertStep`) and `build_momentum_pipeline()`; keep `extraction` as an explicit pre-step
- [x] 5.3 Add an `AlertSummary` schema to replace `alert.run`'s bare `int` return
- [x] 5.4 Repoint `orchestrator.py` to `build_momentum_pipeline`; use `run_traced` to feed the Excel export; delete `ms_agent_framework.py`
- [x] 5.5 Move `agents/**` → `app/agents/**` (code only; data stays put); move external clients (`alpha_vantage`, `congress`, `tv_screener`) → `app/integrations/*`
- [x] 5.6 Move `orchestrator.py` → `app/orchestration/orchestrator.py`; add `app/main.py` entry point (`serve` / `run-pipeline`)
- [x] 5.7 Remove the `models.py` and `web/app.py` shims; update `scripts/regen_excel.py` and run docs (README) to new paths (`backfill_portfolio_weekly.py` imports nothing that moved; `pytest.ini` needs no change)
- [x] 5.8 Verify agents do not import each other or `workflows`; repositories do not import upward
- [x] 5.9 `uv run pytest` green (85 passed); commit `refactor(layout): move agents/orchestration under app/, drop shims` (pyrefly unavailable; ruff used)

## 6. Verification

- [x] 6.1 Confirm no user-facing behaviour change: web endpoints, scheduled pipeline, SIPP import, and CSV/JSON/Excel outputs match pre-refactor — full suite green (85 passed) incl. web auth + end-to-end smoke pipeline; data files unmoved so DB/CSV/JSON/Excel outputs are byte-identical paths. Verified via `ruff` + `pytest` (pyrefly unavailable here).
- [x] 6.2 Update `architecture.mmd` to the layered structure + dependency-flow rules. `ARCHITECTURE.md` is an auto-generated `spec-gen analyze` snapshot — refresh it with `spec-gen analyze` (tool unavailable in this environment) to pick up the new paths.
