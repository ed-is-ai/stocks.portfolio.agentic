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
NOTIFICATIONS_DB = ROOT_DIR / "app" / "agents" / "alert" / "notifications.db"
POSITION_STATE_DB = ROOT_DIR / "app" / "agents" / "alert" / "position_state.db"
RESULTS_DB = ROOT_DIR / "app" / "agents" / "analyst" / "results.db"
CONGRESS_CACHE_DB = ROOT_DIR / "app" / "agents" / "scanner" / "congress_cache.db"
BACKTEST_DB = ROOT_DIR / "data" / "backtest.db"
HISTORICAL_PRICE_CACHE = ROOT_DIR / "data" / "historical_price_cache.db"
BAU_RUN_ENVELOPES_DIR = ROOT_DIR / "data" / "bau_run_envelopes"

# --- Pipeline artifacts ----------------------------------------------------
# Data/artifact files are colocated with each agent's code under app/agents/.
ANALYSIS_JSON = ROOT_DIR / "app" / "agents" / "analyst" / "analysis_results.json"
ANALYSIS_XLSX = ROOT_DIR / "app" / "agents" / "analyst" / "analysis_results.xlsx"
SCAN_RESULTS_JSON = ROOT_DIR / "app" / "agents" / "scanner" / "scan_results.json"
EXTRACTION_RESULTS_JSON = (
    ROOT_DIR / "app" / "agents" / "extraction" / "extraction_results.json"
)
WW_CONTEXT_JSON = ROOT_DIR / "app" / "agents" / "extraction" / "ww_context.json"
# Watermark of the last StockTwits weekly email processed (#137), so the vision
# extraction only re-runs when a strictly newer email has arrived.
STOCKTWITS_EMAIL_WATERMARK_JSON = (
    ROOT_DIR / "app" / "agents" / "extraction" / "stocktwits_email_watermark.json"
)
SCAN_HISTORY_JSON = ROOT_DIR / "app" / "agents" / "scanner" / "scan_history.json"
# Sector-prevalence-per-run snapshots, kept separate from scan_history's
# ticker/zone snapshots so run-over-run sector deltas (#109) don't entangle
# with breakout-transition tracking.
SECTOR_ALLOCATION_HISTORY_JSON = (
    ROOT_DIR / "app" / "agents" / "scanner" / "sector_allocation_history.json"
)
# Cached deterministic (or, later, Claude-generated) MarketNarrative for the
# current run, so digest/web re-renders don't recompute it (#109).
MARKET_NARRATIVE_JSON = (
    ROOT_DIR / "app" / "agents" / "scanner" / "market_narrative.json"
)
# Last-known GICS sector per ticker, reused when yfinance throttling drops the
# sector on a given run (see app.agents.scanner.sector_cache).
SECTOR_CACHE_JSON = ROOT_DIR / "app" / "agents" / "scanner" / "sector_cache.json"
ANALYSIS_PROGRESS_TXT = (
    ROOT_DIR / "app" / "agents" / "analyst" / "analysis_progress.txt"
)
PORTFOLIO_VALUE_CSV = ROOT_DIR / "data" / "portfolio_value.csv"
# Archive of every uploaded SIPP import file, one copy per import, so past
# imports can be re-inspected after the fact. Each upload is parsed directly
# from its own request-owned bytes (#210) — there is no shared working copy
# for this archive to sit "alongside".
IMPORTED_FILES_DIR = ROOT_DIR / "data" / "imported"
PIPELINE_RUNS_CSV = ROOT_DIR / "logs" / "pipeline_runs.csv"
PIPELINE_STATUS_JSON = ROOT_DIR / "logs" / "pipeline_status.json"
PIPELINE_RUN_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_RUN_TIMEOUT_SECONDS", "3600"))
PIPELINE_STALE_GRACE_SECONDS = int(os.getenv("PIPELINE_STALE_GRACE_SECONDS", "60"))
DEFAULT_PIPELINE_STALE_AFTER_HOURS = 24.0
# Portfolio-wide trailing-stop threshold: a held position that falls this
# fraction from its high-water-mark triggers a large-adverse-move alert (#82).
DEFAULT_TRAILING_STOP_PCT = 0.15
# FR-23's confirmed 1-week retention for the import archive -- a fixed
# constant, not a tunable, unlike the max-size default below (PRD OQ-1
# leaves the size open but confirms the retention window).
IMPORTED_FILES_RETENTION_DAYS = 7
# A real SIPP export CSV (per-row header + numeric columns) runs roughly
# 100-200 bytes/row, so even a multi-year, multi-thousand-row export stays
# well under 1 MB. 10 MiB is a generous implementation-time default (PRD
# OQ-1 leaves the exact number open) -- large enough that no legitimate
# export is ever silently dropped, small enough to keep a pathological or
# adversarial upload out of the archive. This only bounds the archived
# copy; it does not limit how large an upload TraderAgent.import_sipp
# itself will read into memory and parse.
DEFAULT_IMPORTED_FILE_MAX_BYTES = 10 * 1024 * 1024

# --- Web / static assets ---------------------------------------------------
TEMPLATES_DIR = ROOT_DIR / "app" / "api" / "templates"
STATIC_DIR = ROOT_DIR / "app" / "api" / "static"
# Jinja2 templates for AlertAgent's outbound HTML emails, colocated with the
# agent's code (see the DB/artifact paths above) rather than under app/api,
# since these render standalone email bodies, not FastAPI request/response
# pages.
ALERT_TEMPLATES_DIR = ROOT_DIR / "app" / "agents" / "alert" / "templates"

# --- Reference data --------------------------------------------------------
TICKER_ALIASES_JSON = ROOT_DIR / "config" / "ticker_aliases.json"
STOCKTWITS_WATCHLIST_JSON = ROOT_DIR / "config" / "stocktwits_watchlist.json"

# --- Claude Code skill packages (used by scanner/analyst subprocesses) -----
SKILLS_DIR = ROOT_DIR / "skills"


def APP_AUTH_TOKEN() -> str | None:
    """Return the shared-secret token for money-mutating endpoints, if set."""
    return os.getenv("APP_AUTH_TOKEN")


def strategy_manager_worker_enabled() -> bool:
    """Return whether this process owns the local Strategy Manager worker."""
    return os.getenv("STRATEGY_MANAGER_WORKER_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


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


def pipeline_stale_after_hours() -> float:
    """Return the validated analysis freshness threshold, in hours.

    Falls back to ``DEFAULT_PIPELINE_STALE_AFTER_HOURS`` for a missing,
    non-numeric, or non-positive value so a bad environment cannot make
    every refresh look permanently stale (or never stale).
    """
    try:
        value = float(
            os.getenv(
                "PIPELINE_STALE_AFTER_HOURS",
                str(DEFAULT_PIPELINE_STALE_AFTER_HOURS),
            )
        )
    except ValueError:
        return DEFAULT_PIPELINE_STALE_AFTER_HOURS
    return value if value > 0 else DEFAULT_PIPELINE_STALE_AFTER_HOURS


def trailing_stop_pct() -> float | None:
    """Return the portfolio-wide trailing-stop fraction, or None if disabled.

    Unset falls back to ``DEFAULT_TRAILING_STOP_PCT``. A non-numeric or
    out-of-range value (outside the open interval ``0 < pct < 1``) returns
    None so callers skip trailing-stop detection gracefully rather than
    firing false alerts.
    """
    raw = os.getenv("TRAILING_STOP_PCT")
    if raw is None:
        return DEFAULT_TRAILING_STOP_PCT
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 < value < 1 else None


def imported_file_max_bytes() -> int:
    """Return the max size (bytes) an uploaded import file may be archived at.

    Falls back to ``DEFAULT_IMPORTED_FILE_MAX_BYTES`` for a missing,
    non-numeric, or non-positive value so a bad environment cannot silently
    disable the size cap.
    """
    try:
        value = int(
            os.getenv("IMPORTED_FILE_MAX_BYTES", str(DEFAULT_IMPORTED_FILE_MAX_BYTES))
        )
    except ValueError:
        return DEFAULT_IMPORTED_FILE_MAX_BYTES
    return value if value > 0 else DEFAULT_IMPORTED_FILE_MAX_BYTES
