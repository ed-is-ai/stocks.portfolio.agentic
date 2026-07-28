# Agents.Stocks

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Purpose: Research](https://img.shields.io/badge/Purpose-Research-6f42c1)
![Execution: Manual](https://img.shields.io/badge/Execution-Manual-orange)

A multi-agent research and portfolio-tracking application for growth and momentum traders. It combines CANSLIM, Weinstein Stage Analysis, and Mark Minervini-style Volatility Contraction Pattern (VCP) analysis to turn a broad stock universe into a smaller, ranked list of setups worth reviewing.

> This is a decision-support tool, not an automated trading bot. It does not connect to a broker or place orders. You remain responsible for validating every setup, sizing risk, and executing trades.

## Who this project is for

Agents.Stocks is aimed at self-directed swing and position traders who:

- focus on US and UK growth or momentum stocks;
- use price, volume, trend, and fundamental quality to narrow a watchlist;
- want repeatable scoring instead of manually checking every chart;
- want market context alongside individual-stock signals; and
- keep a manually managed portfolio and want one place to review positions, P&L, alerts, and scan history.

It helps traders by automating the repetitive research loop:

1. Build a candidate universe from institutional, momentum, VCP, and TradingView sources.
2. Fetch price, volume, sector, and fundamental data.
3. Score candidates with deterministic technical rules.
4. Identify pivots, entry zones, stops, execution state, and risk multiples.
5. Surface actionable setups in the web dashboard and optional email alerts.
6. Track manually entered trades and portfolio performance separately from the research pipeline.

The result is a prioritised research queue, not a buy or sell recommendation. The repository includes no backtest or independently verified performance record.

## How it works

```text
Extraction ──> Scanner ──> Analyst ──> Alert
                 │
                 └──> market context and source-health reporting

Trader ──> manual trades, positions, cash flows, and P&L
```

| Agent | Responsibility |
| --- | --- |
| Extraction | Builds and deduplicates the starting watchlist. |
| Scanner | Adds VCP and TradingView candidates, fetches market data, and computes technical and fundamental fields. |
| Analyst | Scores each setup using deterministic CANSLIM, Weinstein, SEPA, and VCP rules; an optional local LLM can provide a second opinion. |
| Alert | Sends actionable setups by email and applies a per-ticker cooldown. |
| Trader | Records trades entered by the user, imports SIPP activity, and calculates positions and P&L. |

The deterministic analysis includes:

- Weinstein Stage 1–4 classification using SMA50/150/200 levels and slope;
- the Minervini seven-point trend template;
- VCP contraction, tightness, pivot, and volume analysis;
- relative strength, distance from 52-week highs and lows, and moving-average alignment;
- execution states such as pre-breakout, breakout, extended, and damaged; and
- proposed entry, stop, worst-case price, and 1R/2R/3R levels.

When full OHLCV history is unavailable, the engine uses conservative weekly-data approximations. The optional analyst LLM is supplementary and is never the system of record.

## Data sources and why they are used

Most sources are public third-party services and can be delayed, incomplete, rate-limited, or changed without notice. The application records source health so a missing feed is distinguishable from a genuinely empty result.

| Source | Purpose | Key required? |
| --- | --- | --- |
| Yahoo Finance via `yfinance` | Daily price/volume history, quotes, sectors, selected fundamentals, SPY trend context, and portfolio price refreshes. | No |
| WhaleWisdom public heat map | Institutional-interest candidates and per-ticker filer context. | No |
| Curated StockTwits watchlist | Quarterly momentum candidates stored in `config/stocktwits_watchlist.json`. This is local curated input, not a live StockTwits API feed. | No |
| VCP screener | S&P 500 universe screening and VCP candidates; its current pipeline uses DataHub constituent data and Yahoo Finance prices. | No |
| TradingView screener | Pre-filtered US and UK candidate universes through the unofficial scanner endpoint. | No |
| Alpha Vantage | Fills missing fundamentals from Yahoo Finance and supplies NEWS_SENTIMENT items for an LLM-generated market narrative. | Optional |
| TraderMonty market-breadth CSV | Percentage of S&P 500 members above their 200-day moving average. | No |
| QuiverQuant public pages | Recent congressional and Senate buy/sell activity, cached locally. | No |
| GDELT DOC 2.0 | Sector and macro headlines used by the optional market narrative. | No |
| Anthropic Claude | Writes a short narrative grounded in computed sector, breadth, congressional, cycle, and news inputs. A deterministic summary is used without it. | Optional |
| Financial Modeling Prep (FMP) | Used by some standalone skills, including institutional-flow and CANSLIM tooling; it is not required by the main VCP pipeline. | Optional, skill-dependent |
| SIPP CSV export | User-supplied trades, cash movements, and statement balance for portfolio tracking. | User file |

The project stores generated analysis, scan history, source-health status, alerts, trades, and cash flows locally in JSON, CSV, Excel, and SQLite files. It does not send orders to a brokerage.

## Setup

### Requirements

- Python 3.12 or newer
- Git
- [`uv`](https://docs.astral.sh/uv/) (recommended), or `pip`
- Internet access for live market-data sources

### 1. Clone and install

With `uv`:

```bash
git clone https://github.com/ed-is-ai/Agents.stocks.git
cd Agents.stocks
uv sync
```

Or with a standard virtual environment:

```bash
git clone https://github.com/ed-is-ai/Agents.stocks.git
cd Agents.stocks
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

`pyproject.toml` is the source of truth for dependencies. `requirements.txt` is an exported, pinned snapshot and may lag behind it.

### 2. Configure optional integrations

```bash
cp .env.example .env
```

The app runs without paid API keys. Add only the integrations you need:

| Variable | Enables | Required? |
| --- | --- | --- |
| `ALPHA_VANTAGE_API_KEY` | Fundamental-data fallback and Alpha Vantage news. | Optional |
| `ANTHROPIC_API_KEY` | Claude-generated market narrative. | Optional |
| `ANALYST_LLM_SCORING_ENABLED` | Per-ticker scoring through the local OpenAI-compatible endpoint at `http://localhost:5272/v1`. Defaults to `false`. | Optional |
| `EMAIL_USER`, `EMAIL_PASSWORD`, `EMAIL_TO` | Email alerts. Gmail users should use an app password. | Required only for alerts |
| `EMAIL_HOST`, `EMAIL_PORT` | SMTP server; defaults to `smtp.gmail.com:587`. | Optional |
| `APP_AUTH_TOKEN` | Shared secret for trade- and portfolio-mutating requests made from outside localhost. | Recommended for network exposure |
| `FMP_API_KEY` | Standalone skills that still use Financial Modeling Prep. | Skill-dependent |

API keys can also be entered through the dashboard settings where supported. Do not commit `.env`.

### 3. Start the dashboard

```bash
uv run python -m app.main serve
```

Open <http://127.0.0.1:8000>. Local requests may mutate portfolio data; remote mutation requests require `APP_AUTH_TOKEN`.

### 4. Run a scan

From the dashboard, start the pipeline with or without refreshing extraction sources. From the command line:

```bash
# Scan the existing extracted watchlist
uv run python -m app.main run-pipeline

# Refresh WhaleWisdom and the curated StockTwits input first
uv run python -m app.main run-pipeline --extract
```

If using the pip-created environment, omit `uv run` from these commands.

## Main library dependencies

Runtime dependencies are declared in `pyproject.toml`.

| Library | Role in the project |
| --- | --- |
| `pydantic` | Validates agent payloads, API schemas, analysis results, and persisted state. |
| `pandas` | Manipulates OHLCV history, indicators, portfolio data, and reports. |
| `yfinance` | Retrieves Yahoo Finance market and fundamental data. |
| `fastapi`, `uvicorn` | Provide and serve the web application and API. |
| `jinja2`, `python-multipart` | Render the dashboard and process HTML form submissions. |
| `requests` | Calls HTTP-based market and news sources. |
| `tradingview-screener` | Queries TradingView's scanner for US and UK candidates. |
| `tenacity` | Adds bounded retries and backoff around fragile external sources. |
| `apscheduler` | Schedules pipeline runs around US market hours. |
| `openpyxl` | Produces formatted Excel analysis reports. |
| `openai` | Connects the optional analyst second opinion to an OpenAI-compatible local endpoint. |
| `anthropic` | Generates the optional grounded market narrative. |
| `python-dotenv` | Loads local settings and secrets from `.env`. |
| `playwright` | Supports browser-based external-source tooling used by project skills. |

Development dependencies include `pytest`, `pytest-asyncio`, `pytest-json-report`, `ruff`, and `pyrefly`.

## Trading and portfolio tracking

There is no brokerage integration. The Trader agent:

- stores manually entered buy and sell trades in SQLite;
- calculates open positions and average-cost P&L;
- tracks cash flows separately from trades; and
- imports quarterly SIPP CSV exports.

For a SIPP import, export a CSV with these columns:

```text
Date, Symbol, Sedol, Quantity, Price, Description,
Reference, Debit, Credit, Running Balance
```

Save it as `data/processed/SIPP/merged.csv`, then import it from the portfolio tab or in Python:

```python
from app.agents.trader.trader_agent import TraderAgent

agent = TraderAgent()
cash_balance = agent.import_sipp("data/processed/SIPP/merged.csv")
```

Only rows with a valid symbol become trades. Other activity is stored as cash flow, trades are replayed chronologically, and the final running balance is treated as the authoritative cash position. Always compare imported positions, cash, and P&L with the original statement.

## Output and local state

Common generated files include:

| Path | Contents |
| --- | --- |
| `app/agents/extraction/extraction_results.json` | Source-grouped extracted watchlist. |
| `app/agents/scanner/scan_results.json` | Raw enriched scan output. |
| `app/agents/analyst/analysis_results.json` | Structured scores and recommendations. |
| `app/agents/analyst/analysis_results.xlsx` | Human-readable analysis report. |
| `app/agents/alert/alerts.db` | Alert cooldown history. |
| `app/agents/trader/trades.db` | Trades and portfolio cash flows. |
| `logs/pipeline_runs.csv` | Per-run metrics. |
| `data/portfolio_value.csv` | Portfolio value snapshots. |

## Architecture

```text
app/
├── agents/          extraction, scanner, analyst, alert, trader
├── api/             FastAPI routes, templates, and static assets
├── core/            configuration and security
├── integrations/    external data and LLM clients
├── orchestration/   pipeline wiring and scheduling
├── repositories/    SQLite and artifact access
├── schemas/         validated domain models
├── services/        application business logic
└── workflows/       pipeline assembly

config/              ticker aliases and curated watchlists
skills/              standalone screening and analysis packages
tests/               automated test suite
```

Data access is isolated behind repositories, filesystem and environment configuration is centralised in `app/core/config.py`, and Pydantic models validate data at system boundaries.

## Skills and their place in the pipeline

The `skills/` directory contains reusable research packages. Some supply code directly to the application pipeline; others are optional tools for deeper research before or after a scan. They are not autonomous services, and the pipeline does not run every skill on every ticker.

```text
Candidate discovery
├── vcp-screener ───────────────────────────────┐
├── finviz-screener (optional, manual)          │
├── canslim-screener (optional, standalone)     │
└── institutional-flow-tracker (optional)       │
                                                v
Extraction sources ──> Scanner ──> Analyst ──> Alert
                              │         │
                              │         ├── vcp-screener calculators
                              │         ├── technical-analyst criteria
                              │         └── breakout-trade-planner pricing
                              │
                              └── market-narrative ──> dashboard and email context

market-top-detector (optional) ──> separate portfolio-risk context
```

### Skills used directly by the application

| Skill | Pipeline stage | How it is used |
| --- | --- | --- |
| `vcp-screener` | Scanner and Analyst | The Scanner runs its S&P 500 screener to add VCP candidates to the universe. The Analyst imports its calculators directly to evaluate the Minervini trend template, contraction structure, volume dry-up, pivot proximity, and execution state. This is the most deeply integrated skill. |
| `breakout-trade-planner` | Analyst | Once a valid pivot and final contraction low are available, the Analyst imports its risk calculator to derive signal entry, worst-case entry, stop loss, risk percentage, and 1R/2R/3R prices. The app uses the pricing logic, not the skill's optional broker-order templates. |
| `technical-analyst` | Analyst methodology | Three chart-quality checks from this framework are implemented in the Analyst: higher-high/higher-low structure, up-volume versus down-volume confirmation, and SMA50/SMA150 compression. The image-analysis workflow itself is not run by the pipeline. |
| `market-narrative` | Post-scan context | Packages the rules for turning sector allocation, high-conviction prevalence, multi-year breakouts, market breadth, congressional activity, FOMC-cycle position, portfolio weights, and recent headlines into the dashboard/email summary. Claude is used when configured; a deterministic builder is the fallback, and citation guardrails constrain model-written claims. |

### Optional and standalone skills

| Skill | Where it fits | How to use the output |
| --- | --- | --- |
| `canslim-screener` | Candidate discovery or fundamental validation before the main scan. | Produces a separate ranked report across all seven CANSLIM components. The main Analyst applies CANSLIM-style scoring, but it does not invoke this standalone FMP-based screener. |
| `finviz-screener` | Manual candidate discovery. | Converts screening criteria into a Finviz URL. Candidates can then be reviewed or added to a curated watchlist; its browser workflow is not called automatically. |
| `institutional-flow-tracker` | Thesis validation and quarterly idea generation. | Uses 13F data to identify institutional accumulation or distribution. Its reports complement WhaleWisdom extraction, but are not automatically merged into a pipeline run. |
| `market-top-detector` | Portfolio-level risk review alongside the pipeline. | Produces a tactical market-top risk score using distribution days, leadership deterioration, breadth, sentiment, and defensive rotation. It is broader than the pipeline's market narrative and runs separately. |

In practical terms, the default automated path relies on `vcp-screener`, selected `technical-analyst` rules, `breakout-trade-planner`, and `market-narrative`. The other skills extend candidate discovery, confirmation, or market-risk analysis when a trader wants a deeper manual review.

Each skill has a `SKILL.md` containing its inputs, prerequisites, commands, methodology, and output format. Some standalone skills require additional dependencies or `FMP_API_KEY`; consult that file before running one independently.

## Testing and development

```bash
uv run pytest
uv run pyrefly check
uv run ruff check .
uv run ruff format --check .
```

The root Pyrefly command checks `app/` and `tests/`. Standalone skills have
package-local configurations so their generic module names (such as
`calculators` and `scorer`) resolve within the correct package:

```bash
(cd skills/vcp-screener && uv run pyrefly check)
(cd skills/breakout-trade-planner && uv run pyrefly check)
(cd skills/canslim-screener && uv run pyrefly check)
(cd skills/finviz-screener && uv run pyrefly check)
(cd skills/institutional-flow-tracker && uv run pyrefly check)
(cd skills/market-top-detector && uv run pyrefly check)
```

The tests cover the scoring engine, historical pivots, market context, source health, repositories, security, portfolio accounting, alerts, pipeline orchestration, and web routes.

## Limitations

- This is a personal project and has not been externally validated.
- No backtest or performance evidence is included.
- Scores can be wrong and should be checked against the underlying chart and data.
- Third-party data sources can be stale, unavailable, throttled, or structurally changed.
- The TradingView and QuiverQuant integrations use unofficial/public web interfaces that may change.
- Execution is entirely manual; there is no broker connection.

This software is for research and informational use only and is not financial advice.

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 Ed Yau.
