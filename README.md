Agents.Stocks
A multi-agent pipeline for screening, scoring, and tracking US growth stocks using CANSLIM, Weinstein Stage Analysis, and Mark Minervini's VCP (Volatility Contraction Pattern) methodology.

What this is: a research and portfolio-tracking tool. It surfaces high-probability swing-trade setups, scores them with a deterministic technical-analysis engine (with an optional LLM second opinion), sends alerts, and tracks a manually-maintained portfolio.

What this is not: an automated trading bot. It does not connect to a broker and does not place live orders. The "trader" component records trades you enter yourself and computes P&L — all execution is manual and out-of-band. See Trading & portfolio.


Table of contents
How it works
The five agents
Scoring methodology
Architecture
Quick start
Configuration
Running the app
Trading & portfolio
Skills
Testing
Project status & limitations
License


How it works
Extraction → Scanner → Analyst → Alert        (the scan pipeline)

                                    │

                          Trader (separate)    (manual portfolio tracking)

The scan pipeline runs end-to-end on demand or on a schedule. It sources a watchlist, fetches market data, scores each candidate, and alerts on actionable setups. The Trader agent is intentionally decoupled from the scan pipeline — it powers the portfolio view in the web UI and is driven by trades you record manually.

Data flow:

Extraction assembles a watchlist from institutional-holdings (WhaleWisdom) and quarterly-curated StockTwits momentum lists, deduplicated to a single ticker set.
Scanner fetches price/volume history (yfinance), computes technicals, and enriches with fundamentals (Alpha Vantage fallback).
Analyst scores each stock with the deterministic SEPA/VCP engine, optionally adding an LLM second opinion.
Alert emails actionable setups (subject to a per-ticker cooldown).
Market narrative — each run summarises the scan's sector allocation and its context at the top of the digest email and the web watchlist header: sector prevalence (including the high-conviction ≥7/10 share of the universe), the week-on-week rotation, which sectors show the most multi-year breakouts, S&P 500 market breadth (% above 200DMA, keyless public feed), the stocks Congress/Senate are net-buying, the FOMC-cycle position, and recent headlines. With ANTHROPIC_API_KEY set, Claude (Sonnet 5) writes it — grounded strictly in the supplied figures and headlines via a hallucination guard; otherwise a deterministic summary is rendered. Informational only, not financial advice.
Trader (separate) records buy/sell trades you enter and computes portfolio P&L.


The five agents
Agent
Responsibility
Depends on
Extraction
Sources & deduplicates the watchlist
WhaleWisdom, StockTwits config
Scanner
Fetches price/volume, computes technicals, enriches fundamentals
yfinance, Alpha Vantage
Analyst
Scores setups (CANSLIM + Weinstein + VCP); optional LLM opinion
scoring skills, optional OpenAI-compatible LLM
Alert
Emails actionable setups with a cooldown
SMTP
Trader
Records manual trades, computes P&L, imports SIPP CSVs
SQLite only


Each agent lives under app/agents/<name>/ and can be run and tested independently.


Scoring methodology
The Analyst is deterministic-first: all scoring is computed in Python from price/volume data. An LLM is an optional layer on top, never the system of record.

Computed signals include:

Weinstein stage classification (Stage 1–4) from SMA50/150/200 levels and slope.
Minervini 7-point trend template — price vs. SMA150/200, SMA alignment, SMA200 rising, distance from 52-week high/low, relative-strength rank.
VCP detection — contraction counting, pivot-price derivation, tightness (not "wide and loose").
Volume analysis — dry-up ratio into the base, up-day vs. down-day volume confirmation, breakout-volume detection.
Pivot proximity & execution state — distance from pivot, stop placement below the last contraction low, and a state label (Pre-breakout / Breakout / Extended / Damaged, etc.).
Risk framing — entry/stop/worst-case prices and 1R/2R/3R multiples via the breakout-trade-planner skill.

When full OHLCV history is unavailable, the engine falls back to conservative approximations from weekly data rather than failing.

Optional LLM second opinion. If configured, the Analyst can call an OpenAI-compatible endpoint for a structured JSON verdict (score, stage, entry zone, strengths/risks). This is designed to run against a local model (e.g. phi-4-mini via a Foundry Local endpoint at http://localhost:5272/v1) and is supplementary to the deterministic score.

⚠️ No backtest is included. The methodology is faithfully implemented, but this repo does not ship evidence that it is profitable. Treat scores as a research signal, not a recommendation. This is not financial advice.


Architecture
The codebase is organised as a layered app/ package:

app/

├── agents/          # The five agents (extraction, scanner, analyst, alert, trader)

├── api/             # FastAPI app, routes, Jinja2 templating

│   └── routes/      # pipeline, portfolio, trades, views (HTML partials)

├── core/            # config.py (single owner of paths + env), security.py

├── integrations/    # alpha_vantage, congress, tv_screener, gdelt, market_breadth, anthropic_client

├── orchestration/   # orchestrator.py — wires agents, schedules on market hours

├── repositories/    # one repo per SQLite table (trades, alerts, results, cash flows, …)

├── schemas/         # Pydantic models (StockRecord, StockAnalysis, Trade, Position, …)

├── services/        # pipeline / portfolio / trader business logic

└── workflows/       # momentum pipeline assembly

config/              # ticker aliases, StockTwits watchlist

skills/              # standalone calculator skill packages (see Skills)

tests/               # pytest suite (204 tests)

Design notes:

Repository pattern over SQLite — data access is isolated behind per-table repositories.
Centralised config — app/core/config.py is the single owner of all filesystem paths and environment access; modules import resolved paths rather than deriving them.
Pydantic schemas validate data at boundaries and for inter-agent JSON.
Authenticated mutations — money-mutating endpoints are gated by require_local_or_token (see Configuration).


Quick start
Requirements: Python 3.12+. uv is recommended.

# Clone

git clone https://github.com/ed-is-ai/Agents.stocks.git

cd Agents.stocks

# Install (uv reads pyproject.toml / uv.lock)

uv sync

# Copy and fill in environment variables

cp .env.example .env

#   …edit .env…

# Run the web app

uv run python -m app.main serve

# → http://127.0.0.1:8000

Using plain pip instead of uv:

python -m venv .venv && source .venv/bin/activate

pip install -e .          # installs from pyproject (preferred)

# or: pip install -r requirements.txt

Note: requirements.txt is generated from pyproject.toml via uv export and can lag behind it. Installing from pyproject.toml (pip install -e . or uv sync) is the reliable path — it guarantees python-multipart (needed by the web form routes) is present.


Configuration
All configuration is via environment variables (loaded from .env). Copy .env.example and fill in what you need — everything is optional except where a feature you want requires it.

Variable
Used for
Required?
ALPHA_VANTAGE_API_KEY
Fundamental-data fallback; also the NEWS_SENTIMENT feed for the market narrative
Optional (leave blank to disable)
ANTHROPIC_API_KEY
Claude (Sonnet 5) market-narrative summary — set via Settings ▸ Market narrative or .env
Optional (a deterministic summary is used when unset)
APP_AUTH_TOKEN
Shared secret gating money-mutating API endpoints
Recommended if exposing the app beyond localhost
EMAIL_USER / EMAIL_PASSWORD
SMTP sender credentials (Gmail: use an app password)
Required for email alerts
EMAIL_TO
Alert recipient
Required for email alerts
EMAIL_HOST / EMAIL_PORT
SMTP server (defaults to Gmail smtp.gmail.com:587)
Optional


Some screener skills read their own keys (e.g. FMP_API_KEY for the VCP screener) — see .env.example and the individual skill READMEs.

Endpoint auth. Trade- and portfolio-mutating routes depend on require_local_or_token: requests from localhost are allowed, and remote requests must present APP_AUTH_TOKEN. Set the token before exposing the app on a network.


Running the app
The entry point is app/main.py with two sub-commands:

# Serve the FastAPI web UI (uvicorn)

python -m app.main serve [--host 127.0.0.1] [--port 8000] [--reload]

# Run the scan pipeline once

python -m app.main run-pipeline [--extract]

#   --extract  refreshes the watchlist (Extraction) before scanning

Scheduled runs. app/orchestration/orchestrator.py can run the pipeline on a cron schedule (via APScheduler) aligned to US market hours, writing results and a per-run metrics log.

Web UI. The app serves an HTML dashboard at / with live partials for the watchlist, portfolio, run history, and run log. Pipeline and trade actions are exposed as API routes under /pipeline, /trades, and /portfolio.

Output artifacts (paths centralised in app/core/config.py):

app/agents/scanner/scan_results.json — raw scan output
app/agents/analyst/analysis_results.json / .xlsx — scores & recommendations
app/agents/alert/alerts.db — alert cooldown history
app/agents/trader/trades.db — recorded trades & cash flows
logs/pipeline_runs.csv — per-run metrics
data/portfolio_value.csv — portfolio snapshots


Trading & portfolio
There is no brokerage integration and no automated order execution. The Trader agent:

Records buy/sell trades that you enter (via the web UI or programmatically) into SQLite.
Computes P&L on an average-cost basis and maintains open positions.
Imports SIPP CSVs — quarterly Self-Invested Personal Pension exports are parsed, separating stock trades from non-trade cash flows (contributions, dividends, tax relief, interest).
Quarterly SIPP import
Export a SIPP CSV from your provider (e.g. Interactive Investor, AJ Bell). Expected columns: Date, Symbol, Sedol, Quantity, Price, Description, Reference, Debit, Credit, Running Balance. Save as data/processed/SIPP/merged.csv.
Import via the web UI portfolio tab, or directly:

from app.agents.trader.trader_agent import TraderAgent

agent = TraderAgent()

cash_balance = agent.import_sipp("data/processed/SIPP/merged.csv")

Verify open-position count, cash balance vs. your statement, and unrealised P&L.

Import behaviour: only rows with a valid Symbol become trades; non-trade rows are stored as cash flows; trades are replayed in chronological (DD/MM/YYYY) order; and the final Running Balance is treated as the authoritative cash position. Unrecognised date formats are logged and the row is kept rather than aborting the import.


Skills
skills/ contains standalone calculator packages (used by the scanner/analyst, and runnable on their own):

Skill
Purpose
vcp-screener
Trend template, VCP pattern, volume pattern, pivot proximity, execution state
breakout-trade-planner
Entry/stop/target pricing and R-multiples
canslim-screener
CANSLIM growth-criteria screening
technical-analyst
HH/HL structure, volume confirmation, MA compression
vcp-screener, finviz-screener
Candidate screening from external sources
institutional-flow-tracker
Institutional holdings signals
market-top-detector
Broad-market risk context



Testing
uv run pytest                 # run the suite (204 tests)

uv run pytest --cov=app       # with coverage

uv run pyrefly check          # type checking

uv run ruff check .           # lint

uv run ruff format .          # format

The suite covers the analyst scoring engine, historical pivots, scan history, repositories, the security/auth layer, the trader, and the web routes.


Project status & limitations
Personal project, not externally validated. Built and maintained by one author for their own use.
No backtest / performance data. The methodology is implemented faithfully but its profitability is unproven here. Not financial advice.
Manual execution only. No broker connection; you place trades yourself.
Data-source fragility. Watchlist and fundamental sources are third-party (yfinance/WhaleWisdom/StockTwits/Alpha Vantage) and can change or rate-limit.
Some screener skills require their own API keys (e.g. FMP).


License
MIT — see LICENSE. Copyright (c) 2026 Ed Yau.

