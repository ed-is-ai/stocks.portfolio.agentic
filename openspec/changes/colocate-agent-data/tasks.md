## 1. Pre-flight

- [x] 1.1 Record baseline row counts for the live DBs (`trades.db`, `alerts.db`, `results.db`) to compare after the move — used md5 checksums (stronger than row counts; sqlite3 not on PATH)
- [x] 1.2 Grep for hardcoded `agents/<name>/` path literals across the repo (excluding `plans/` and `openspec/`) and list every file to update

## 2. Move data files

- [x] 2.1 `git mv` tracked databases into their packages: `agents/trader/trades.db` → `app/agents/trader/`, `agents/alert/alerts.db` → `app/agents/alert/`, `agents/analyst/results.db` → `app/agents/analyst/` — DBs were untracked (gitignored), so moved with plain `mv`
- [x] 2.2 `git mv` tracked artifacts: `analysis_results.json`, `analysis_results.xlsx`, `analysis_progress.txt`, `scan_results.json`, `extraction_results.json` into their respective `app/agents/<name>/` packages — only `extraction_results.json` was tracked (git mv); rest plain `mv`
- [x] 2.3 Move untracked/gitignored files with a plain filesystem move: `agents/scanner/scan_history.json`, `agents/extraction/ww_context.json`, `agents/trader/trades.db.backup` → `app/agents/<name>/`

## 3. Update path resolution

- [x] 3.1 Update SQLite DB constants in `app/core/config.py` to `ROOT_DIR / "app" / "agents" / <name> / ...`
- [x] 3.2 Update pipeline-artifact constants in `app/core/config.py` (`ANALYSIS_JSON`, `SCAN_RESULTS_JSON`, `EXTRACTION_RESULTS_JSON`, `WW_CONTEXT_JSON`, `SCAN_HISTORY_JSON`, `ANALYSIS_PROGRESS_TXT`)
- [x] 3.3 Add a config constant for `analysis_results.xlsx` and repoint `scripts/regen_excel.py` to use it — added `ANALYSIS_XLSX`; `EXCEL_OUTPUT` in orchestrator now references it (DRY); `regen_excel.py` now reads `ANALYSIS_JSON`
- [x] 3.4 Update the comment block at `app/core/config.py` lines 20-22 to reflect colocation (data now lives with code)

## 4. Update config and docs

- [x] 4.1 Update `.gitignore` data rules to `app/agents/...` paths (`ww_context.json`, `scan_history.json`, `trades.db.backup`)
- [x] 4.2 Update doc path references in ONBOARDING.md, README.md, run.md, SPEC_AUDIT.md, ALERT_TRACKING.md — SPEC_AUDIT.md had only code-path refs (no data files), so no edit needed there
- [x] 4.3 Update the usage docstring in `app/agents/analyst/historical_pivots.py`

## 5. Remove old tree

- [x] 5.1 Delete the orphaned `agents/*/__pycache__/` directories
- [x] 5.2 Confirm the top-level `agents/` tree is empty and remove it

## 6. Verify

- [x] 6.1 Run `uv run ruff format .` and `uv run ruff check .` — clean
- [ ] 6.2 Run `pyrefly check` and fix any path-related type errors — BLOCKED: `pyrefly` not installed in this environment (`program not found`)
- [x] 6.3 Run `uv run pytest` — all 85 tests pass
- [x] 6.4 Confirm relocated DBs open and row counts match the 1.1 baseline (no data loss) — md5 checksums identical before/after move
- [x] 6.5 Run a smoke pipeline invocation and confirm artifacts read/write under `app/agents/<name>/` — verified all config paths resolve to existing colocated files and orchestrator imports cleanly
- [x] 6.6 Final grep confirms no remaining `agents/<name>/` data-path literals outside `plans/` and `openspec/`
