## Why

The layered-architecture refactor moved each agent's **code** to `app/agents/<name>/` but left its **data** (SQLite databases and JSON artifacts) behind in a parallel top-level `agents/<name>/` tree. The split is confusing — two folders named `agents` with the same subfolders — and leaves stale `.pyc` files orphaned next to the live databases. Colocating each agent's data inside its own package makes the layout self-explanatory and removes the dead tree.

## What Changes

- Relocate every agent's runtime data file into its code package under `app/agents/<name>/`:
  - `agents/trader/trades.db`, `trades.db.backup` → `app/agents/trader/`
  - `agents/alert/alerts.db` → `app/agents/alert/`
  - `agents/analyst/results.db`, `analysis_results.json`, `analysis_results.xlsx`, `analysis_progress.txt` → `app/agents/analyst/`
  - `agents/scanner/scan_results.json`, `scan_history.json` → `app/agents/scanner/`
  - `agents/extraction/extraction_results.json`, `ww_context.json` → `app/agents/extraction/`
- Update the path constants in [app/core/config.py](../../../app/core/config.py) (the single owner of filesystem paths) to point at the new `app/agents/<name>/` locations.
- Update `.gitignore` data-file rules to the new paths.
- Delete the now-empty top-level `agents/` tree, including its orphaned `__pycache__` directories.
- Update doc references (ONBOARDING.md, README.md, run.md, etc.) that point at the old data paths.
- **BREAKING** (operational, not API): the live databases physically move. Anyone with an existing checkout must move their data files or re-import; the relocation must preserve `trades.db` (real portfolio data) intact.

## Capabilities

### New Capabilities
- `agent-data-colocation`: each agent's runtime data (databases and artifacts) lives inside that agent's code package, with all paths resolved through the central config module.

### Modified Capabilities
<!-- No spec-level behavior changes; this is a file-location refactor. -->

## Impact

- **Code**: [app/core/config.py](../../../app/core/config.py) path constants only — no agent logic changes, since every module already imports paths from config.
- **Filesystem**: live SQLite databases and JSON artifacts move from `agents/` to `app/agents/`; old tree deleted.
- **Config**: `.gitignore` rules updated.
- **Docs**: ONBOARDING.md, README.md, run.md, SPEC_AUDIT.md, ALERT_TRACKING.md path references.
- **Tests**: should be unaffected (they use temp dirs / config indirection) — verified by running the suite after the move.
