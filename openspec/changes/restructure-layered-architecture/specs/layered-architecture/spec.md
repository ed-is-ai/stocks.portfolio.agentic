## ADDED Requirements

### Requirement: Centralized configuration owns all paths and env

A single module `app/core/config.py` SHALL be the only source of filesystem
paths and environment access. Modules SHALL import paths from it rather than
deriving them from `__file__` or mutating `sys.path`.

#### Scenario: Paths resolved from config, not `__file__`

- **WHEN** any module needs `trades.db`, an artifact file, or the templates dir
- **THEN** it imports the path from `app.core.config`
- **AND** no module derives those paths via `Path(__file__).parent` chains
- **AND** no module calls `sys.path.insert` to enable imports

### Requirement: Repositories are the sole owner of trades.db access

All access to `trades.db` SHALL go through `app/repositories/`. The SQL schema
SHALL be defined in exactly one place (`repositories/db.py`). No agent, service,
or API module SHALL open a `sqlite3` connection directly.

#### Scenario: Agents use repositories instead of raw SQL

- **WHEN** `trader`, `alert`, `analyst`, or `ingestion/sipp` reads or writes
  trade/cash/price data
- **THEN** it calls a repository method
- **AND** it does not `import sqlite3` or define `CREATE TABLE` statements

#### Scenario: Schema has a single owner

- **WHEN** the `trades.db` schema needs a change
- **THEN** the change is made only in `repositories/db.py`
- **AND** no `CREATE TABLE`/`ALTER TABLE` text exists in agent modules

### Requirement: Web and scheduler route through the service layer

API route handlers and the scheduler/orchestrator SHALL invoke `app/services/`
rather than instantiating agents or accessing repositories directly. Route
handlers SHALL obtain services via dependency injection.

#### Scenario: Route handlers depend on services

- **WHEN** an API route handles portfolio, trade, or pipeline-refresh requests
- **THEN** it receives a service via FastAPI `Depends`
- **AND** it does not instantiate `TraderAgent` or call repositories directly

#### Scenario: Scheduler uses the pipeline service

- **WHEN** the scheduled pipeline runs
- **THEN** it invokes `PipelineService` (or `build_momentum_pipeline`)
- **AND** stage wiring is not duplicated inside `orchestrator.py`

### Requirement: Layer dependency direction is enforced

Dependencies SHALL flow `api`/`orchestration` → `services` → `agents` →
`integrations`, with `services` and `agents` → `repositories`. Lower layers
SHALL NOT import higher layers.

#### Scenario: Agents stay decoupled

- **WHEN** any pipeline-stage agent module is inspected
- **THEN** it does not import another agent package
- **AND** it does not import `services`, `api`, or `workflows`

#### Scenario: Repositories do not import upward

- **WHEN** any repository module is inspected
- **THEN** it does not import `agents`, `services`, `api`, or `workflows`

### Requirement: Data models remain Pydantic, not ORM

The schema layer (`app/schemas/`) SHALL contain Pydantic models only. The change
SHALL NOT introduce an ORM. A backward-compatibility shim SHALL preserve
existing `models` imports during migration.

#### Scenario: Schemas are Pydantic

- **WHEN** `app/schemas/` modules are inspected
- **THEN** they define Pydantic `BaseModel` subclasses
- **AND** no ORM base class or `models/` ORM layer is introduced

#### Scenario: Legacy imports keep working mid-migration

- **WHEN** code imports a model from `models`
- **THEN** it resolves via the re-export shim to `app.schemas.*`

### Requirement: External clients are `integrations`; `skills/` is unchanged

External data clients SHALL live under `app/integrations/`. The pre-existing
top-level `skills/` directory (Claude Code skill packages) SHALL remain
unchanged and SHALL NOT be repurposed for agent-invokable tools.

#### Scenario: Clients are integrations

- **WHEN** the Alpha Vantage, Congress, FMP, or yfinance client is used
- **THEN** it is imported from `app.integrations`

#### Scenario: Claude Code skills untouched

- **WHEN** the refactor completes
- **THEN** top-level `skills/` retains its existing SKILL.md packages unchanged
