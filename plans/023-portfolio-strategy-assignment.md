# Plan 023: Assign one active Strategy to each portfolio (story #440)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat c12ef330..HEAD -- app/repositories/db.py app/repositories/portfolios_repo.py app/api/routes/portfolios.py app/services/portfolio_service.py app/api/dependencies.py`
> If any of these changed since `c12ef330`, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it
> as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: — (first delivery story of feature #439)
- **Category**: feature
- **Planned at**: commit `c12ef330`, 2026-08-30
- **GitHub story**: ed-is-ai/Agents.stocks#440 (parent feature #439)
- **UX reference**: `docs/mockups/portfolio-strategy-recommendations.html`
  (surfaces 1 and 3)

## Why this matters

Portfolios currently share the scanner's generic recommendations. This plan
delivers the persistence + UI seam that lets a portfolio owner assign exactly
one active Strategy to an account, so stories #441 (recommendations screen)
and #442 (per-portfolio daily email) have an authoritative assignment to read.
It is deliberately persistence-and-UI only: no recommendation calculation, no
email dispatch, no backtest/scan triggering.

## Current state

- `app/repositories/db.py` owns the whole `trades.db` schema in the `_SCHEMA`
  constant (`CREATE TABLE IF NOT EXISTS`) plus idempotent migration helpers.
  There is no `PRAGMA user_version`; additive DDL is the migration mechanism.
  `connect()` sets `PRAGMA foreign_keys = ON`.
- `portfolios` table: `id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT
  NULL, created_at TEXT NOT NULL`. Per-portfolio uniqueness precedent:
  `cash_balances (PRIMARY KEY (portfolio_id, currency))` and
  `portfolio_trade_revisions (portfolio_id PRIMARY KEY, revision)`.
- `PortfoliosRepository.delete()` hard-deletes trades, cash flows, snapshots
  and `account_state` keys — an explicit-delete pattern (not relying on
  cascade) is the house style.
- Repos take a `Connect = Callable[[], sqlite3.Connection]` in `__init__`;
  `db.session(connect)` commits and closes.
- DI seam: `app/api/dependencies.py` exposes `@lru_cache` providers; repo
  providers construct with `db.make_connect(lambda: str(<CONFIG_PATH>))` then
  call `ensure_schema()`. Routes use `Annotated[..., Depends(get_*_service)]`
  and money-mutating endpoints are guarded by `Depends(require_local_or_token)`
  (`app/core/security.py`).
- `app/api/routes/portfolios.py` renders the partial back via:
  ```python
  def _render(request, portfolio, portfolio_id):
      context = portfolio.default_portfolio_context(portfolio_id)
      return templates.TemplateResponse(request, "_portfolio.html", context=context)
  ```
- Strategy metadata: `discover_strategies(skills_root) -> StrategyDiscoveryResultV1`
  (`app/services/backtest/skill_discovery.py`) is fail-soft (warnings, never
  raises for a bad skill folder) and cached per filesystem revision.
  `StrategyDescriptorV1` carries `strategy_id`, `display_name`, `description`,
  `parameters`, `default_parameters`, `source_digest`. The shared parameter
  validator is `validate_strategy_parameters(schema, submitted, *,
  apply_defaults)` in `app/services/backtest/strategy_protocol.py` — the single
  authority; do not write a second validator.
- Freshness authority: `read_analysis_artifact_meta(ANALYSIS_JSON) ->
  AnalysisArtifactMeta | None` (`app/schemas/analysis_artifact.py`) gives
  `generated_at`; `ANALYSIS_JSON` and `SKILLS_DIR` are exported from
  `app/core/config.py`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| New tests | `uv run pytest tests/test_portfolio_strategies_repo.py tests/test_strategy_assignment_service.py tests/test_strategy_assignment_routes.py -v` | all pass |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |
| Types | `uv run pyrefly check` | no new errors |

## Scope

**In scope** (the only files you should create/modify):
- `app/repositories/db.py` — add `portfolio_strategies` table to `_SCHEMA`
- `app/repositories/portfolio_strategies_repo.py` — **new**
- `app/services/strategy_assignment_service.py` — **new**
- `app/api/dependencies.py` — add `get_portfolio_strategies_repository`,
  `get_strategy_assignment_service`
- `app/api/routes/portfolios.py` — add assign/clear endpoints
- `app/api/templates/_portfolio.html` — Strategy control in account controls
- `app/api/templates/_strategy_assign.html` — **new** assign modal partial
- `app/schemas/trade.py` (or a new `app/schemas/strategy_assignment.py`) —
  `StrategyAssignment` pydantic model
- `tests/test_portfolio_strategies_repo.py`, `tests/test_strategy_assignment_service.py`,
  `tests/test_strategy_assignment_routes.py` — **new**

**Out of scope** (do NOT touch):
- Recommendation calculation, market-view adapters, email dispatch (plans
  024/025)
- `discover_strategies` / `validate_strategy_parameters` internals
- The existing generic recommendation and daily-email paths
- Custom per-portfolio parameter editing (V1 stores validated defaults only)

## Git workflow

- Branch: `feat/023-portfolio-strategy-assignment`
- Commit message: `feat(portfolio): assign one active strategy per portfolio`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the `portfolio_strategies` table

In `app/repositories/db.py`, append to `_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS portfolio_strategies (
    portfolio_id    INTEGER PRIMARY KEY REFERENCES portfolios(id) ON DELETE CASCADE,
    strategy_id     TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    assigned_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

The `portfolio_id` PRIMARY KEY enforces "at most one assignment per portfolio"
at the database level (AC #440.3). Additive `CREATE TABLE IF NOT EXISTS` needs
no data migration; existing databases get the table on next open and no
backfill happens (AC #440.8).

In `PortfoliosRepository.delete()`, add an explicit
`DELETE FROM portfolio_strategies WHERE portfolio_id = ?` alongside the other
hard-deletes (house style; do not rely on the cascade alone).

**Verify**: `uv run python -c "import sqlite3, app.repositories.db as d; c=sqlite3.connect(':memory:'); c.executescript(d._SCHEMA); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio_strategies'\")])"`
→ `['portfolio_strategies']`

### Step 2: Create `PortfolioStrategiesRepository`

New `app/repositories/portfolio_strategies_repo.py`, following
`portfolios_repo.py`:

```python
class PortfolioStrategiesRepository:
    def __init__(self, connect: Connect) -> None: ...
    def get(self, portfolio_id: int) -> StrategyAssignment | None: ...
    def upsert(self, portfolio_id: int, strategy_id: str,
               parameters: Mapping[str, JsonScalar]) -> StrategyAssignment: ...
    def clear(self, portfolio_id: int) -> bool: ...
    def list_assigned(self) -> list[StrategyAssignment]: ...
```

- `upsert` uses `INSERT ... ON CONFLICT(portfolio_id) DO UPDATE SET ...`,
  sets `assigned_at` only on first insert and `updated_at` on every write
  (ISO-8601 UTC, matching the rest of the schema).
- `parameters_json` stores the canonical snapshot: `json.dumps(mapping,
  sort_keys=True, separators=(",", ":"))`.
- `list_assigned` powers plan 025's dispatch loop; keep it cheap.

**Verify**: `uv run pytest tests/test_portfolio_strategies_repo.py -v` (after
Step 5 adds the tests) — or a quick REPL upsert/get/clear round-trip against a
tmp_path DB using the `TraderAgent.db_path` reassignment pattern from
`tests/test_multi_portfolio.py`.

### Step 3: Create `StrategyAssignmentService` (the provider seam)

New `app/services/strategy_assignment_service.py`. Routes must depend on this
service, never on repositories or strategy runtime files directly (story
"Technical direction"). Responsibilities:

- `list_choices() -> tuple[StrategyDescriptorV1, ...]` +
  `list_warnings() -> tuple[StrategyDiscoveryWarningV1, ...]` — thin wrappers
  over `discover_strategies(SKILLS_DIR)` (fail-soft; warnings surfaced, never
  raised).
- `assign(portfolio_id, strategy_id) -> StrategyAssignment` — resolves the
  descriptor, validates `descriptor.default_parameters` with
  `validate_strategy_parameters(schema, defaults, apply_defaults=False)`
  (must return a mapping, not errors), stores the canonical snapshot. Raises a
  domain error type for unknown/incompatible `strategy_id` — the route turns
  this into a visible warning, never a silent switch (AC #440.6).
- `clear(portfolio_id) -> None`.
- `get_assignment(portfolio_id) -> StrategyAssignment | None`.
- `enrich(assignment) -> AssignmentView` — joins the stored `strategy_id`
  against current discovery: found → `available` with `display_name`;
  not found → `unavailable` (retained, with repair/clear actions; AC
  #440.6).
- `freshness() -> ScanFreshness` — reads
  `read_analysis_artifact_meta(ANALYSIS_JSON)`: `None` → `missing`;
  `generated_at` naive/unparseable → `unknown`; else age = now −
  `generated_at`, `stale` iff `age > timedelta(hours=24)` **exactly** (the
  boundary itself, `age == 24h`, is fresh — AC #440.5). Non-blocking either
  way.

**Verify**: `uv run pytest tests/test_strategy_assignment_service.py -v`
(after Step 5).

### Step 4: Routes, DI and templates

1. `app/api/dependencies.py`: add `get_portfolio_strategies_repository`
   (connect against `TRADES_DB`, following `get_backtest_repository`) and
   `get_strategy_assignment_service` (both `@lru_cache`).
2. `app/api/routes/portfolios.py`:
   - `POST /portfolios/{portfolio_id}/strategy` — body `strategy_id`;
     `Depends(require_local_or_token)`; calls
     `assignment_service.assign(...)`; on domain error re-renders the partial
     with a visible warning (200, not 500); on success re-renders via
     `_render` (AC #440.4: no backtest/scan/email/trade is triggered here).
   - `POST /portfolios/{portfolio_id}/strategy/clear` — same guards, calls
     `clear`, re-renders.
3. `app/api/templates/_portfolio.html`: in the account-controls row, add a
   **Strategy** control between the account select and Manage (see mockup
   surface 1): assigned → chip with display name + `Change` + `Clear`
   (`hx-post` clear with `hx-confirm`); none → `No Strategy` chip + `Assign…`
   button that `hx-get`s the modal partial into a modal target.
4. New `app/api/templates/_strategy_assign.html` (mockup surface 3): radio
   list from `list_choices()` with `display_name`, `strategy_id`, description
   and the default-parameter snapshot; discovery warnings rendered as a
   non-blocking alert; unavailable previously-assigned strategy shown
   retained with Repair/Clear; freshness warning banner when
   `freshness()` is `missing`/`unknown`/`stale`; submit is
   `hx-post="/portfolios/{{ portfolio_id }}/strategy"` targeting
   `#tab-content`.
5. Extend `PortfolioService.default_portfolio_context` (or the assignment
   service, called from `_render`) to add `strategy_assignment` (the
   `AssignmentView` or `None`) and `strategy_freshness` keys. A portfolio
   with no assignment must render byte-identical structure apart from the new
   control (AC #440.7).

**Verify**: `uv run pytest tests/test_strategy_assignment_routes.py -v`
(after Step 5).

### Step 5: Tests

Follow `tests/test_portfolios_routes.py` (TestClient +
`app.dependency_overrides`) and `tests/test_multi_portfolio.py` (tmp_path DB
via `db_path` reassignment). Async tests use anyio, not asyncio.

`tests/test_portfolio_strategies_repo.py`:
- upsert inserts then replaces (one row per portfolio — uniqueness/lifecycle,
  AC #440.3/9)
- `assigned_at` stable across replace, `updated_at` advances
- `clear` removes the row; `get` returns `None` after
- `PortfoliosRepository.delete` removes the assignment (AC #440.8)
- canonical parameter snapshot: dict key order in storage is sorted

`tests/test_strategy_assignment_service.py`:
- assign stores descriptor id + validated defaults (canonicalized)
- unknown `strategy_id` → domain error, stored assignment untouched
- previously-assigned strategy missing from discovery → `enrich` returns
  `unavailable`, assignment retained (AC #440.6)
- freshness: missing file → `missing`; corrupt meta → `unknown`;
  `generated_at` exactly 24h old → fresh; 24h + 1s → `stale` (exact boundary,
  AC #440.5)
- discovery warnings surfaced without breaking choices (AC #440.2)

`tests/test_strategy_assignment_routes.py`:
- assign/clear require auth (401/403 without token — reuse the
  `require_local_or_token` test pattern from plan 020)
- assign re-renders `_portfolio.html` with the new chip (HTMX rendering)
- assign with unknown strategy → 200 partial with visible warning, not 500
- clear on a portfolio with no assignment → idempotent success
- a portfolio with no assignment renders the same as before except the new
  control (no-assignment compatibility, AC #440.7)

### Step 6: Full suite, lint, format, types

**Verify**:
- `uv run pytest` → all pass
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformat; re-stage only
  in-scope files)
- `uv run pyrefly check` → no new errors

## Test plan

See Step 5 — 4 repo tests, 6 service tests, 5 route tests, all offline
(tmp_path DBs, monkeypatched discovery/`ANALYSIS_JSON`, TestClient with
dependency overrides). No network, no SMTP, no strategy runtime execution.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `portfolio_strategies` table present in `_SCHEMA`; PK is
      `portfolio_id`
- [ ] `uv run pytest tests/test_portfolio_strategies_repo.py
      tests/test_strategy_assignment_service.py
      tests/test_strategy_assignment_routes.py -v` → all pass
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `uv run pyrefly check` → no new errors
- [ ] Routes import no strategy runtime files and no repositories directly
      (grep: `app/api/routes/portfolios.py` has no
      `skill_discovery`/`repositories` imports)
- [ ] `git status` shows only in-scope files modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows any anchor file changed and the "Current state"
  excerpts no longer match.
- `validate_strategy_parameters` rejects any discovered strategy's own
  `default_parameters` — that is a discovery/validator contract bug, not
  something this plan should paper over with `apply_defaults=True` tricks.
- `ANALYSIS_JSON` meta turns out to lack a usable `generated_at` on the
  current artifact (legacy bare-list files) — the `unknown` freshness state
  covers it, but report if the envelope reader itself fails.
- Adding the table requires touching any existing table (it must not).

## Maintenance notes

- `list_assigned()` is the contract plan 025's dispatch loop consumes; keep
  its return shape stable.
- The `AssignmentView.unavailable` state is what the UI and (later) the email
  pipeline use to skip evaluation — do not silently drop the row.
- Custom per-portfolio parameter editing is explicitly deferred (feature
  #439 decision); `parameters_json` is the place a future plan extends.
