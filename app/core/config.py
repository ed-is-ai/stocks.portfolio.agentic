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
CONGRESS_CACHE_DB = ROOT_DIR / "app" / "agents" / "scanner" / "congress_cache.db"

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
PIPELINE_STATUS_JSON = ROOT_DIR / "logs" / "pipeline_status.json"
PIPELINE_RUN_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_RUN_TIMEOUT_SECONDS", "3600"))
PIPELINE_STALE_GRACE_SECONDS = int(os.getenv("PIPELINE_STALE_GRACE_SECONDS", "60"))

# --- Web / static assets ---------------------------------------------------
TEMPLATES_DIR = ROOT_DIR / "app" / "api" / "templates"
STATIC_DIR = ROOT_DIR / "app" / "api" / "static"

# --- Reference data --------------------------------------------------------
TICKER_ALIASES_JSON = ROOT_DIR / "config" / "ticker_aliases.json"
STOCKTWITS_WATCHLIST_JSON = ROOT_DIR / "config" / "stocktwits_watchlist.json"

# --- Claude Code skill packages (used by scanner/analyst subprocesses) -----
SKILLS_DIR = ROOT_DIR / "skills"


def APP_AUTH_TOKEN() -> str | None:
    """Return the shared-secret token for money-mutating endpoints, if set."""
    return os.getenv("APP_AUTH_TOKEN")


def ANALYST_LLM_SCORING_ENABLED() -> bool:
    """Return whether the analyst should score stocks via Foundry Local.

    Off by default: per-ticker LLM calls run sequentially in the scoring
    loop, so enabling this on a large scan makes the analysis stage much
    slower. Rule-based scoring is the default until that loop is
    parallelized.
    """
    return os.getenv("ANALYST_LLM_SCORING_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
