## Why

After the layered-architecture refactor moved code under `app/`, the repo root
still collects runtime outputs and one-off scripts that obscure what is actually
source vs. generated:

- **Runtime outputs squat at the root.** `pipeline_runs.csv`, `portfolio_value.csv`,
  and `test-results.json` are written into the repo root on every pipeline/test
  run. They are gitignored, so they are invisible to `git`, but they clutter the
  working tree and blur "what is checked in."
- **Scratch is half-swept.** `scratch/` now holds the `tmp_*` throwaways, but three
  ignored one-off scripts (`debug_positions.py`, `init_email_tracking.py`,
  `query_alert_history.py`) are still loose in the root.
- **`scripts/` has invisible members.** Several files now living in `scripts/`
  (`backfill_portfolio_history.py`, `backfill_weekly.py`, `import_brokerage_trades.py`,
  `merge_brokerage_history.py`, `migrate_portfolio_col.py`) are silently matched by
  per-file `**/<name>.py` ignore rules — so a tracked-looking utility directory
  contains files `git` cannot see.
- **`.gitignore` organizes by accretion.** A dozen per-file rules hide scratch one
  at a time instead of using directory rules, and some rules are now dead (e.g.
  `dashboard-data.json` after the dashboard was deleted).

The earlier `plans/007-tidy-root-scripts.md` aimed at this but is now stale: it was
written to *protect* `orchestrator.py` / `models.py` / `web/app.py` as root entry
points, all of which the refactor has since moved or deleted. This change replaces
it.

## What Changes

- Relocate runtime/generated outputs out of the repo root, addressed only through
  `app/core/config.py` (and `pytest.ini`):
  - `pipeline_runs.csv` → `data/pipeline_runs.csv`
  - `portfolio_value.csv` → `data/portfolio_value.csv`
  - `test-results.json` → `logs/test-results.json`
  - `run_error.log` → `logs/run_error.log` (stale capture; no code writes it)
- Make the run-log writer create its parent directory, so a fresh clone (where
  `logs/` is gitignored and absent) does not crash on first write.
- Finish the scratch sweep: move the three loose ignored one-offs into `scratch/`.
- Resolve the `scripts/` collision so that directory contains only tracked,
  shareable utilities — the privacy-sensitive one-offs move to `scratch/` (see
  design for the alternative).
- Consolidate `.gitignore`: directory-based rules (`scratch/`, `logs/`) replace the
  per-file scratch rules; drop dead rules.
- Supersede `plans/007-tidy-root-scripts.md`.

## Capabilities

### New Capabilities

- `repo-layout`: Repository layout rules — runtime/generated artifacts live under
  dedicated directories (not the repo root), throwaway scratch lives under a single
  gitignored `scratch/`, and `scripts/` contains only tracked utilities.

## Impact

- **Behaviour:** No user-facing change. The web run-log tab, portfolio value chart,
  SIPP cash seed, and pipeline outputs are functionally identical — only their
  on-disk paths move. `portfolio_value.csv` carries the cash-seed history and must
  be moved with its data intact, not regenerated.
- **Code:** `app/core/config.py` (two path constants), `pytest.ini`
  (`--json-report-file`), `app/orchestration/orchestrator.py` (mkdir before append),
  `.gitignore`.
- **Files moved (gitignored, working-tree only):** `pipeline_runs.csv`,
  `portfolio_value.csv`, and the three root one-offs into `scratch/`.
- **Unchanged:** `trades.db`/`alerts.db`/`results.db` and the `agents/` data tree
  (already settled by the layered-architecture change), `skills/`, `data/` inputs.
