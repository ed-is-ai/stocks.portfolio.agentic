# Run Instructions

## Prerequisites

- Python 3.14+ (`python --version`)
- Dependencies installed via `uv` or pip

## Setup (first time only)

Install dependencies:

```powershell
pip install -r requirements.txt
```

## Commands

All commands must be run from the **project root** directory.

### Run once with default watchlist (ignores market hours)

```powershell
python orchestrator.py --once
```

### Run once pulling live top-50 holdings from WisdomWise

```powershell
python orchestrator.py --once --extract
```

### Run individual agents standalone

```powershell
python -m agents.extraction.extraction_agent
python -m agents.scanner.scanner_agent
python -m agents.analyst.analyst_agent
python -m agents.alert.alert_agent
```

> Use `python -m agents.<name>.<name>_agent` (not `python agents/...`) so imports resolve correctly.

### Run on a schedule (market hours only, Mon–Fri 9:30–16:00 ET)

```powershell
python orchestrator.py --interval 15
```

Add `--extract` to refresh the watchlist from WisdomWise on every scheduled run.

`--interval` sets the polling frequency in minutes (default: 15). Press `Ctrl+C` to stop.

## Output

- `agents/extraction/extraction_results.json` — grouped ticker list by source (StockTwits, WisdomWise)
- `agents/scanner/scan_results.json` — raw scanner output for all tickers
- `agents/analyst/analysis_results.json` — analyst scores and entry/stop levels
- Console table — ranked by score with CANSLIM breakdown, entry price, stop loss, and risk %

## Notes

- `uv` may not be on PATH; use `python` directly
- Alerts are deduplicated — previously alerted tickers are skipped
- `--extract` falls back to the default watchlist if WisdomWise returns no data
- The watchlist is managed via `agents/extraction/extraction_results.json`; running with `--extract` appends any new WisdomWise tickers under a dated source group