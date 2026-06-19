## 1. Relocate runtime outputs

> **As-built note:** both CSVs go to `data/` (revised from the original
> `pipeline_runs.csv → logs/`); `logs/` holds the test report and `run_error.log`.

- [x] 1.1 Move `pipeline_runs.csv` → `data/pipeline_runs.csv` (filesystem move, preserve data)
- [x] 1.2 Move `portfolio_value.csv` → `data/portfolio_value.csv` (filesystem move, preserve data — it seeds first-run cash)
- [x] 1.3 Repoint `app/core/config.py`: `PIPELINE_RUNS_CSV = ROOT_DIR / "data" / "pipeline_runs.csv"`, `PORTFOLIO_VALUE_CSV = ROOT_DIR / "data" / "portfolio_value.csv"`
- [x] 1.4 Repoint `pytest.ini`: `--json-report-file=logs/test-results.json`
- [x] 1.5 Add `parent.mkdir(parents=True, exist_ok=True)` in `orchestrator._append_run_log` + `_append_portfolio_snapshot` and in `ArtifactsRepository.write_json` / `append_csv_row` so a fresh, dir-less clone does not crash
- [x] 1.6 Grep for stray literal references — app runtime path is config-driven; **one-off `scripts/` still hardcode `Path("portfolio_value.csv")` at the old root** (see task 2.2 / design Decision 3)
- [x] 1.7 Move stale `run_error.log` → `logs/run_error.log` (no code writes it); drop the stale root `test-results.json`; add tracked `logs/.gitkeep`

## 2. Finish the scratch sweep

- [x] 2.1 Move `debug_positions.py`, `init_email_tracking.py`, `query_alert_history.py` → `scratch/`
- [x] 2.2 Move the privacy-sensitive `scripts/` one-offs (`backfill_portfolio_history.py`, `backfill_weekly.py`, `backfill_portfolio_weekly.py`, `import_brokerage_trades.py`, `merge_brokerage_history.py`, `migrate_portfolio_col.py`) → `scratch/` — applied the design default (all to `scratch/`); `scripts/` now holds only tracked utilities (`check_results.py`, `regen_excel.py`, `validate_specs.py`). `patch_cash_history.py` also swept to `scratch/`

## 3. Consolidate `.gitignore`

- [x] 3.1 Remove dead `dashboard-data.json` rule; remove redundant per-file `tmp_*` / `refresh_data.ipynb` rules; add a `scratch/` directory rule _(done ad hoc in working tree)_
- [x] 3.2 After §2, drop the now-redundant `**/debug_positions.py` / `**/init_email_tracking.py` / `**/query_alert_history.py` and the privacy `**/<script>.py` rules whose files now live under `scratch/` — 8 per-file rules removed; verified all moved files remain ignored via the `scratch/` directory rule
- [x] 3.3 Add `logs/*` + `!logs/.gitkeep`; relocated CSVs stay covered by their bare-name rules; `run_error.log` bare rule dropped (now under `logs/`). Confirmed `logs/test-results.json` ignored, `logs/.gitkeep` tracked

## 4. Verify & supersede

- [x] 4.1 `pytest` green (85 passed); report writes to `logs/test-results.json`; cash seed + value chart resolve from the relocated `data/portfolio_value.csv` (tests exercise the config path)
- [x] 4.2 Ignore checks pass (relocated files + `run_error.log` ignored; `logs/.gitkeep` tracked); no personal-data file newly exposed
- [x] 4.3 Mark `plans/007-tidy-root-scripts.md` superseded by this change in `plans/README.md` — updated the 007 status row and the dependency/findings note
- [x] 4.4 `ruff check` + `ruff format --check` clean on touched files (pyrefly unavailable in this env)
