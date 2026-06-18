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

- [ ] 3.1 Create `app/schemas/{scan,record,trade}.py` and move the Pydantic models out of `models.py`
- [ ] 3.2 Make `models.py` re-export from `app.schemas.*` for backward compatibility
- [ ] 3.3 `pyrefly check` + `uv run pytest` green; commit `refactor(schemas): split models.py into app/schemas with shim`

## 4. Phase 4 — Services + thin API

- [ ] 4.1 Create `app/services/trader_service.py` wrapping `TraderAgent` + repositories
- [ ] 4.2 Create `app/services/portfolio_service.py` owning valuation + GBP/USD conversion (moved from `web/app.py` helpers)
- [ ] 4.3 Create `app/services/pipeline_service.py` that builds and runs the workflow (used by both web `/refresh-data` and the scheduler)
- [ ] 4.4 Move `require_local_or_token` to `app/core/security.py`
- [ ] 4.5 Create `app/api/app.py` (FastAPI factory), `app/api/dependencies.py` (`get_trader_service`, `get_portfolio_service`), and `app/api/routes/{portfolio,trades,pipeline,views}.py`; routes call services via `Depends`
- [ ] 4.6 Move `web/templates/` → `app/api/templates/`; keep a thin `web/app.py` shim importing the new app for existing run commands
- [ ] 4.7 `pyrefly check` + `uv run pytest` (incl. `tests/test_web_auth.py`) green; commit `refactor(api): route web through service layer`

## 5. Phase 5 — Typed pipeline, agents, integrations, orchestration

- [ ] 5.1 Create `app/workflows/pipeline.py` with `Step` (Protocol), `Pipeline` (generic builder: `start`, `then`, `run`, `run_traced`) — static-only contract, per-step timing/logging
- [ ] 5.2 Create `app/workflows/momentum.py` with Step adapters (`ScanStep`, `AnalyseStep`, `AlertStep`) and `build_momentum_pipeline()`; keep `extraction` as an explicit pre-step
- [ ] 5.3 Add an `AlertSummary` schema to replace `alert.run`'s bare `int` return
- [ ] 5.4 Repoint `orchestrator.py` to `pipeline_service` / `build_momentum_pipeline`; use `run_traced` to feed the Excel export; delete `ms_agent_framework.py`
- [ ] 5.5 Move `agents/**` → `app/agents/**`; move external clients (`alpha_vantage_client`, `congress_client`, FMP/yfinance helpers) → `app/integrations/*`
- [ ] 5.6 Move `orchestrator.py` → `app/orchestration/orchestrator.py`; add `app/main.py` entry point (`serve` / `run-pipeline`)
- [ ] 5.7 Remove the `models.py` and `web/app.py` shims; update `scripts/`, `backfill_portfolio_weekly.py`, `pytest.ini`, and run docs to new paths
- [ ] 5.8 Verify agents do not import each other or `workflows`; repositories do not import upward
- [ ] 5.9 `pyrefly check` + `uv run pytest` green; commit `refactor(workflows): typed linear pipeline + finalize app/ layout`

## 6. Verification

- [ ] 6.1 Confirm no user-facing behaviour change: web endpoints, scheduled pipeline, SIPP import, and CSV/JSON/Excel outputs match pre-refactor
- [ ] 6.2 Update `ARCHITECTURE.md` / `architecture.mmd` to the layered structure and dependency-flow rules
