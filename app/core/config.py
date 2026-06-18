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
TRADES_DB = ROOT_DIR / "agents" / "trader" / "trades.db"
ALERTS_DB = ROOT_DIR / "agents" / "alert" / "alerts.db"
RESULTS_DB = ROOT_DIR / "agents" / "analyst" / "results.db"

# --- Pipeline artifacts ----------------------------------------------------
ANALYSIS_JSON = ROOT_DIR / "agents" / "analyst" / "analysis_results.json"
SCAN_RESULTS_JSON = ROOT_DIR / "agents" / "scanner" / "scan_results.json"
PORTFOLIO_VALUE_CSV = ROOT_DIR / "portfolio_value.csv"
PIPELINE_RUNS_CSV = ROOT_DIR / "pipeline_runs.csv"

# --- Web / static assets ---------------------------------------------------
TEMPLATES_DIR = ROOT_DIR / "web" / "templates"

# --- Reference data --------------------------------------------------------
TICKER_ALIASES_JSON = ROOT_DIR / "data" / "ticker_aliases.json"


def APP_AUTH_TOKEN() -> str | None:
    """Return the shared-secret token for money-mutating endpoints, if set."""
    return os.getenv("APP_AUTH_TOKEN")
