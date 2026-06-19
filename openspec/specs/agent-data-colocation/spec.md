# agent-data-colocation Specification

## Purpose
TBD - created by archiving change colocate-agent-data. Update Purpose after archive.
## Requirements
### Requirement: Agent data colocated with agent code

Each agent's runtime data SHALL reside inside that agent's code package under
`app/agents/<name>/`. This covers its SQLite databases and JSON/XLSX/TXT
artifacts, which live in the agent's package rather than a parallel top-level
`agents/<name>/` tree.

#### Scenario: Trader data lives with trader code

- **WHEN** the trader agent reads or writes its trade ledger
- **THEN** the database resolves to `app/agents/trader/trades.db`

#### Scenario: Every agent's artifacts colocated

- **WHEN** the analyst, scanner, alert, or extraction agent persists a result
- **THEN** the file is written under `app/agents/<that-agent>/`

#### Scenario: No parallel top-level agents tree remains

- **WHEN** the repository is inspected after the change
- **THEN** no top-level `agents/` directory exists (neither data files nor
  orphaned `__pycache__` directories)

### Requirement: Filesystem paths resolved through central config

All agent data paths SHALL be resolved through the central config module
([app/core/config.py](../../../../app/core/config.py)) so that callers never
derive data locations from `__file__` chains or hardcoded literals.

#### Scenario: Path constants point at colocated locations

- **WHEN** a module needs an agent database or artifact path
- **THEN** it imports the corresponding constant from `app.core.config`, and that
  constant resolves to a path under `app/agents/<name>/`

#### Scenario: Relocation preserves existing data

- **WHEN** the data files are moved to their new locations
- **THEN** the contents of `trades.db` (and all other live databases) are
  preserved byte-for-byte, with no re-import required

