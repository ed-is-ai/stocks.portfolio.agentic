## Context

After the layered-architecture refactor, agent **code** lives in `app/agents/<name>/`
while agent **data** stayed in a parallel top-level `agents/<name>/` tree. Today
[app/core/config.py](../../../app/core/config.py) is already the single owner of
every path, so the data locations are defined in exactly one place — for example:

```python
TRADES_DB = ROOT_DIR / "agents" / "trader" / "trades.db"
ANALYSIS_JSON = ROOT_DIR / "agents" / "analyst" / "analysis_results.json"
```

This is what makes the move cheap: agent modules never hardcode paths, so
relocating the files plus editing these constants is the whole code change.

## Goals / Non-Goals

**Goals**
- Each agent's data files live inside `app/agents/<name>/`.
- All path constants in config.py updated to match.
- The top-level `agents/` tree (data + orphaned `__pycache__`) is deleted.
- Live databases move with contents intact — no re-import.

**Non-Goals**
- No change to agent logic, schemas, or query behavior.
- No change to where *non-agent* data lives (`data/portfolio_value.csv`,
  `config/*.json`, `logs/`) — those stay put.
- Not consolidating or renaming the databases themselves.

## Decisions

### Decision: Move data into the existing code packages

Each file moves to the sibling of its agent module:

| Current location | New location |
| --- | --- |
| `agents/trader/trades.db` | `app/agents/trader/trades.db` |
| `agents/trader/trades.db.backup` | `app/agents/trader/trades.db.backup` |
| `agents/alert/alerts.db` | `app/agents/alert/alerts.db` |
| `agents/analyst/results.db` | `app/agents/analyst/results.db` |
| `agents/analyst/analysis_results.json` | `app/agents/analyst/analysis_results.json` |
| `agents/analyst/analysis_results.xlsx` | `app/agents/analyst/analysis_results.xlsx` |
| `agents/analyst/analysis_progress.txt` | `app/agents/analyst/analysis_progress.txt` |
| `agents/scanner/scan_results.json` | `app/agents/scanner/scan_results.json` |
| `agents/scanner/scan_history.json` | `app/agents/scanner/scan_history.json` |
| `agents/extraction/extraction_results.json` | `app/agents/extraction/extraction_results.json` |
| `agents/extraction/ww_context.json` | `app/agents/extraction/ww_context.json` |

**Why:** the agent packages already exist; colocation needs no new directories.

**Alternative considered — a single `data/agents/` tree:** rejected. It would
re-create the same code/data split this change exists to remove, and config.py
already encodes per-agent locations, so colocation is the lower-friction path.

### Decision: Use `git mv` to preserve history and DB contents

Databases hold real portfolio data (`trades.db`). Use `git mv` so:
- file contents move byte-for-byte (no re-import, satisfies the preservation
  scenario in the spec),
- git tracks the rename rather than a delete+add.

`trades.db.backup` and the gitignored artifacts (`scan_history.json`,
`ww_context.json`, `trades.db.backup`) are not tracked, so move those with a
plain filesystem move; only the tracked DBs use `git mv`.

### Decision: config.py is the only code edit

Update the constants in the two relevant sections:

```python
# --- SQLite databases ---
TRADES_DB   = ROOT_DIR / "app" / "agents" / "trader"  / "trades.db"
ALERTS_DB   = ROOT_DIR / "app" / "agents" / "alert"   / "alerts.db"
RESULTS_DB  = ROOT_DIR / "app" / "agents" / "analyst" / "results.db"

# --- Pipeline artifacts ---
ANALYSIS_JSON           = ROOT_DIR / "app" / "agents" / "analyst"    / "analysis_results.json"
SCAN_RESULTS_JSON       = ROOT_DIR / "app" / "agents" / "scanner"    / "scan_results.json"
EXTRACTION_RESULTS_JSON = ROOT_DIR / "app" / "agents" / "extraction" / "extraction_results.json"
WW_CONTEXT_JSON         = ROOT_DIR / "app" / "agents" / "extraction" / "ww_context.json"
SCAN_HISTORY_JSON       = ROOT_DIR / "app" / "agents" / "scanner"    / "scan_history.json"
ANALYSIS_PROGRESS_TXT   = ROOT_DIR / "app" / "agents" / "analyst"    / "analysis_progress.txt"
```

The `analysis_results.xlsx` path is not currently a config constant (referenced
ad hoc in `scripts/regen_excel.py`); update that literal directly, or leave the
xlsx with its db since it is a generated artifact.

Also update the comment block at config.py lines 20-22, which currently states
the data files "stay at the top-level agents/ tree" — this change reverses that.

### Decision: Update gitignore and docs in the same change

`.gitignore` rules (`agents/extraction/ww_context.json`,
`agents/scanner/scan_history.json`, `agents/trader/trades.db.backup`,
`**/scan_history.json`) repoint to `app/agents/...`. Doc references in
ONBOARDING.md, README.md, run.md, SPEC_AUDIT.md, ALERT_TRACKING.md, and the
`historical_pivots.py` usage docstring update to the new paths.

The `plans/*.md` and `openspec/changes/*` files are historical records and are
**not** edited.

## Risks / Trade-offs

- **Risk: losing live portfolio data during the move.** Mitigation: `git mv`
  preserves contents; verify `trades.db` opens and row counts match before/after;
  the existing `trades.db.backup` is an extra safety net.
- **Risk: a hardcoded path missed somewhere.** Mitigation: grep for
  `agents/<name>/` literals across the repo (excluding `plans/`, `openspec/`)
  before and after; run the full test suite.
- **Trade-off: existing checkouts break** until users move their local data.
  Acceptable for a single-user/local tool; called out as operational BREAKING in
  the proposal.

## Migration Plan

1. Move tracked DBs with `git mv`; move untracked artifacts/backups with a plain
   move.
2. Edit config.py constants + comment.
3. Update `.gitignore` and docs.
4. Delete the emptied top-level `agents/` tree (including `__pycache__`).
5. Run the full test suite and a smoke pipeline run to confirm paths resolve.

## Open Questions

- Should `analysis_results.xlsx` get a first-class config constant during this
  change, or stay as an ad-hoc literal? (Lean: add the constant for consistency.)
