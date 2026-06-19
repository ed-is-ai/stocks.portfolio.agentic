## ADDED Requirements

### Requirement: Runtime and generated artifacts live outside the repo root

Runtime and tooling-generated files (the pipeline run log, the portfolio value log, and the test report) SHALL be written under dedicated directories rather than the repository root, at locations owned by `app/core/config.py` (application artifacts) and `pytest.ini` (the test report).

#### Scenario: Pipeline run log is written under `data/`

- **WHEN** the orchestrator records a pipeline run
- **THEN** it appends to `data/pipeline_runs.csv` (resolved from `config.PIPELINE_RUNS_CSV`)
- **AND** no run log is written to the repository root

#### Scenario: Portfolio value log lives under `data/`

- **WHEN** the trader appends a portfolio value snapshot or seeds first-run cash
- **THEN** it reads/writes `data/portfolio_value.csv` (resolved from `config.PORTFOLIO_VALUE_CSV`)
- **AND** the cash-seed history is preserved across the move (the file is moved, not regenerated)

#### Scenario: Test report is written under `logs/`

- **WHEN** the test suite runs
- **THEN** the JSON report is written to `logs/test-results.json` (per `pytest.ini`)

#### Scenario: A missing artifact directory is created on write

- **WHEN** an artifact directory (e.g. `logs/`) does not exist on a fresh clone
- **THEN** the writer creates it before writing
- **AND** the first write does not fail

### Requirement: Scratch is a single ignored directory; `scripts/` holds only tracked utilities

Throwaway scripts and notebooks SHALL live under a single gitignored `scratch/` directory rather than being ignored one file at a time, and `scripts/` SHALL contain only tracked, shareable utilities (no files that are silently gitignored).

#### Scenario: Throwaway files are ignored via the directory rule

- **WHEN** a `tmp_*` or one-off debug script is added under `scratch/`
- **THEN** it is ignored by the `scratch/` directory rule
- **AND** `.gitignore` does not need a new per-file rule

#### Scenario: `scripts/` has no invisible members

- **WHEN** the `scripts/` directory is inspected
- **THEN** every file in it is tracked by git (none is matched by an ignore rule)

#### Scenario: Repo root contains only source and config

- **WHEN** the repository root is listed
- **THEN** it contains no runtime/generated artifacts and no loose one-off scripts
- **AND** generated files appear only under `logs/`, `data/`, `reports/`, or `scratch/`
