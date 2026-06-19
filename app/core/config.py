"""Centralized configuration — the single owner of filesystem paths and env.

Every module that needs ``trades.db``, an artifact file, or the templates
directory imports the resolved path from here rather than deriving it from
``__file__`` chains or mutating ``sys.path``. Environment access is likewise
funnelled through the accessors below.
"""

import os
from pathlib import Path

# Repo root: app/core/config.py -> app/core -> app -> <root>
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# --- SQLite databases ------------------------------------------------------
# Each agent's data lives alongside its code under app/agents/<name>/.
TRADES_DB = ROOT_DIR / "app" / "agents" / "trader" / "trades.db"
ALERTS_DB = ROOT_DIR / "app" / "agents" / "alert" / "alerts.db"
RESULTS_DB = ROOT_DIR / "app" / "agents" / "analyst" / "results.db"

# --- Pipeline artifacts ----------------------------------------------------
# Data/artifact files are colocated with each agent's code under app/agents/.
ANALYSIS_JSON = ROOT_DIR / "app" / "agents" / "analyst" / "analysis_results.json"
ANALYSIS_XLSX = ROOT_DIR / "app" / "agents" / "analyst" / "analysis_results.xlsx"
SCAN_RESULTS_JSON = ROOT_DIR / "app" / "agents" / "scanner" / "scan_results.json"
EXTRACTION_RESULTS_JSON = (
    ROOT_DIR / "app" / "agents" / "extraction" / "extraction_results.json"
)
WW_CONTEXT_JSON = ROOT_DIR / "app" / "agents" / "extraction" / "ww_context.json"
SCAN_HISTORY_JSON = ROOT_DIR / "app" / "agents" / "scanner" / "scan_history.json"
ANALYSIS_PROGRESS_TXT = (
    ROOT_DIR / "app" / "agents" / "analyst" / "analysis_progress.txt"
)
PORTFOLIO_VALUE_CSV = ROOT_DIR / "data" / "portfolio_value.csv"
PIPELINE_RUNS_CSV = ROOT_DIR / "logs" / "pipeline_runs.csv"

# --- Web / static assets ---------------------------------------------------
TEMPLATES_DIR = ROOT_DIR / "app" / "api" / "templates"

# --- Reference data --------------------------------------------------------
TICKER_ALIASES_JSON = ROOT_DIR / "config" / "ticker_aliases.json"
STOCKTWITS_WATCHLIST_JSON = ROOT_DIR / "config" / "stocktwits_watchlist.json"

# --- Claude Code skill packages (used by scanner/analyst subprocesses) -----
SKILLS_DIR = ROOT_DIR / "skills"


def APP_AUTH_TOKEN() -> str | None:
    """Return the shared-secret token for money-mutating endpoints, if set."""
    return os.getenv("APP_AUTH_TOKEN")
