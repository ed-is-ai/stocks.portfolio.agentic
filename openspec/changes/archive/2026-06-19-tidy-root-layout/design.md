## Context

This is a low-risk DX/housekeeping change. The files in question are already
gitignored, so the moves are working-tree operations — `git` history is
unaffected. The only behaviour-adjacent risks are (a) preserving the data inside
`portfolio_value.csv` (it seeds first-run cash) and (b) ensuring the run-log
writer's directory exists.

Some of this was started ad hoc in the working tree before this change was
written: the dashboard (`dashboard.html`/`dashboard-data.json`) and `run_tests.py`
were deleted, `scratch/` was created with the `tmp_*` files moved in,
`backfill_portfolio_weekly.py` was moved to `scripts/`, and `.gitignore` had its
dead/redundant scratch rules removed and a `scratch/` directory rule added. The
tasks below capture the full end state; items already done are marked complete.

## Goals / Non-Goals

**Goals:**
- Repo root holds only source and config — no runtime/generated artifacts, no
  loose one-off scripts.
- One gitignored `scratch/` for throwaway; `scripts/` for tracked utilities.
- Runtime artifact locations owned by `app/core/config.py` / `pytest.ini`.

**Non-Goals:**
- Relocating `trades.db` / `alerts.db` / `results.db` or the `agents/` data tree
  (settled by `restructure-layered-architecture`: data stays put).
- Changing any pipeline/web behaviour or output format.
- Introducing a log *framework* — `logs/` here just holds the run-log CSV and the
  pytest report, not application logging.

## Decisions

### 1. `data/` for the CSVs, `logs/` for diagnostics

Both CSVs (`pipeline_runs.csv`, `portfolio_value.csv`) are app-owned data the
code reads back (run-log tab, value chart, cash seed) → `data/`. Pure
diagnostic/tooling output (`test-results.json`, the stale `run_error.log`) →
`logs/`. (An earlier draft put `pipeline_runs.csv` in `logs/`; revised to keep all
CSVs together under `data/`.)

The relocated CSVs keep gitignore coverage via the bare-name rules
(`pipeline_runs.csv` / `portfolio_value.csv` match at any depth). `logs/` is kept
present on fresh clones with a tracked `logs/.gitkeep` while its contents are
ignored (`logs/*`), so `pytest` (which does not create the dir itself) can write
`logs/test-results.json`.

### 2. Fix the writer, don't just place a `.gitkeep`

`orchestrator._append_run_log` opens the path with a raw `open(path, "a")` and no
`mkdir`. On a fresh clone `logs/` will not exist (it is gitignored), so the first
write would crash. Rather than commit a `logs/.gitkeep` placeholder, add
`log_path.parent.mkdir(parents=True, exist_ok=True)` before the write — this fixes
the latent "writer assumes the directory exists" bug at the source. (The
`ArtifactsRepository` CSV/JSON writers should do the same for any path they own.)

### 3. `scripts/` = tracked utilities only; privacy one-offs go to `scratch/`

`scripts/` currently contains five files that are *silently* gitignored by
`**/<name>.py` privacy rules (`backfill_portfolio_history.py`, `backfill_weekly.py`,
`import_brokerage_trades.py`, `merge_brokerage_history.py`, `migrate_portfolio_col.py`).
A tracked-looking utility directory holding files `git` cannot see is the worst of
both worlds.

**Chosen:** move those privacy-sensitive one-offs into `scratch/` (already
gitignored as a directory), leaving `scripts/` with only genuinely tracked,
shareable utilities. This keeps the "may contain portfolio details" protection
(the user's original rationale) and removes the invisibility surprise.

**Alternative (requires a human call):** if any of those scripts are reusable and
contain no personal data, sanitize and *track* them in `scripts/` (and drop their
per-file ignore rule). This is a per-file privacy judgement and is out of scope
unless the owner opts in — hence the default is "move to scratch."

`backfill_portfolio_weekly.py` is the inconsistent member: it sits in `scripts/`
with no ignore rule (so it is trackable) while its two siblings are ignored. The
backfill trio should be treated consistently — default: all three to `scratch/`.

### 4. Directory-based ignores over per-file accretion

Replace the per-file scratch rules with directory rules: `scratch/` covers all
throwaway, and the runtime outputs are covered by their existing bare-name rules.
Drop dead rules (`dashboard-data.json`). Keep `test-results.json` (still produced)
and `run_error.log`.

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| Moving `portfolio_value.csv` loses the cash-seed/chart history | Move the file with its data (`git`-ignored, so a plain move), do not regenerate; verify the chart + first-run seed still resolve |
| Fresh clone crashes writing the run log into a missing `logs/` | Writer `mkdir(parents=True, exist_ok=True)` (Decision 2) |
| A relocated path is still referenced by an old absolute string | All paths flow through `app/core/config.py` / `pytest.ini`; grep for literal `pipeline_runs.csv` / `portfolio_value.csv` / `test-results.json` before finishing |
| Hiding useful scripts in `scratch/` | Reversible; the alternative (track in `scripts/`) remains open per file |
