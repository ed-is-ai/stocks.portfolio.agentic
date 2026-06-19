# Agents.Stocks

A multi-agent stock portfolio management system for identifying, analyzing, and trading promising growth stocks using CANSLIM and Weinstein Stage analysis.

## Quick Start

### For New Team Members: Onboarding

Start here to understand the system:
1. Read [System Architecture Spec](openspec/specs/system-architecture/) — Overview of the 5 agents and how they work together
2. Read [Data Models Spec](openspec/specs/data-models/) — Understand the key data structures
3. Choose an agent to focus on:
   - [Scanner Agent](openspec/specs/scanner-agent/) — Data collection
   - [Analyst Agent](openspec/specs/analyst-agent/) — Stock scoring
   - [Alert Agent](openspec/specs/alert-agent/) — Notifications
   - [Trader Agent](openspec/specs/trader-agent/) — Trade execution
   - [Extraction Agent](openspec/specs/extraction-agent/) — Watchlist sourcing

Each spec includes design decisions, constraints, and extension points.

### For Feature Development

When adding new capabilities, follow the extension guides:
- **Add a new data source?** See [Scanner Extension Guide](openspec/specs/scanner-agent/extension-guide.md)
- **Add a new scoring framework?** See [Analyst Extension Guide](openspec/specs/analyst-agent/extension-guide.md)
- **Add a new alert channel (Slack, SMS)?** See [Alert Extension Guide](openspec/specs/alert-agent/extension-guide.md)
- **Integrate a new broker?** See [Trader Extension Guide](openspec/specs/trader-agent/extension-guide.md)
- **Add a new watchlist source?** See [Extraction Extension Guide](openspec/specs/extraction-agent/extension-guide.md)

## System Architecture

```
Orchestrator (schedules agents on market hours)
    ↓
Extraction Agent → Scanner Agent → Analyst Agent → Alert Agent → Trader Agent
    ↓                ↓               ↓               ↓             ↓
extraction_results  scan_results    analysis_       email alerts  trades
.json               .json           results.json    alerts.db     positions.db
```

**Data Flow:**
1. **Extraction**: Sources watchlist from multiple screeners (TradingView, StockTwits, WhaleWisdom)
2. **Scanner**: Fetches price/volume, computes technicals, enriches with fundamentals
3. **Analyst**: Scores stocks using CANSLIM + Weinstein Stage analysis
4. **Alert**: Sends notifications for actionable setups
5. **Trader**: Executes buy/sell orders, manages portfolio

## Development

### Setup

```bash
# Install dependencies (resolves from pyproject.toml / uv.lock)
uv sync

# Run tests
uv run pytest

# Type checking
uv run pyrefly check

# Code formatting
uv run ruff format .
uv run ruff check . --fix
```

See [CLAUDE.md](.claude/CLAUDE.md) for detailed development guidelines.

### Key Files

- `app/schemas/` — Pydantic data models (StockRecord, StockAnalysis, Position, etc.)
- `app/orchestration/orchestrator.py` — Main scheduler, wires agents together
- `app/agents/` — Individual agent implementations
- `app/api/` — FastAPI web app (factory, routes, templates)
- `app/main.py` — Entry point (`serve` for the web UI, `run-pipeline` for one run)
- `skills/` — Reusable scoring/trading skill libraries
- `openspec/specs/` — Formal specifications for all agents

## Deployment

Orchestrator runs on market schedule (9:30 AM - 4:00 PM ET, weekdays).

```bash
# Run single pipeline execution
uv run python -m app.main run-pipeline

# Or run the scheduler directly (APScheduler, market-hours)
uv run python -m app.orchestration.orchestrator

# Serve the web UI
uv run python -m app.main serve
```

Output files:
- `agents/scanner/scan_results.json` — Raw stock data
- `agents/analyst/analysis_results.json` — Scores and recommendations
- `pipeline_runs.csv` — Execution log and metrics
- `portfolio_value.csv` — Portfolio snapshots
- `agents/alert/alerts.db` — Alert cooldown history
- `agents/trader/positions.db` — Trade history

## Architecture Decisions

### Why 5 Agents?

Separation of concerns:
- **Extraction**: Data sourcing (depends on external screeners)
- **Scanner**: Data collection (depends on yfinance, APIs)
- **Analyst**: Signal generation (depends on methodologies)
- **Alert**: Notification (depends on channels)
- **Trader**: Execution (depends on broker)

Each agent can be independently updated, tested, and deployed.

### Why CANSLIM + Weinstein?

- **CANSLIM**: Proven growth stock framework by William O'Neil
- **Weinstein Stage Analysis**: Identifies stage of market cycle (1-4)
- **VCP Pattern**: Mark Minervini's breakout methodology for entry timing

Combination captures growth + momentum + technicals.

### Why This Data Structure?

Core models (StockRecord, StockAnalysis, Position) are designed for:
- Type safety (Pydantic validation)
- Extensibility (optional fields, new fields added without breaking)
- Serializability (JSON for inter-agent communication)
- Querying (fields enable filtering and analysis)

## Common Tasks

### Run the full pipeline
```bash
uv run python -m app.main run-pipeline
```

### Test a single agent
```bash
uv run python -m app.agents.scanner.scanner_agent
```

### Check recent alerts
```bash
sqlite3 agents/alert/alerts.db "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT 10;"
```

### Review portfolio
```bash
tail portfolio_value.csv
```

### Quarterly SIPP Portfolio Update

The portfolio is maintained through quarterly SIPP (Self-Invested Personal Pension) CSV imports.

**Process:**
1. Export SIPP CSV from your provider (Interactive Investor, AJ Bell, etc.)
   - Expected columns: Date, Symbol, Sedol, Quantity, Price, Description, Reference, Debit, Credit, Running Balance
   - Save as `data/processed/SIPP/merged.csv`

2. Run the import (via web UI portfolio tab or directly):
   ```python
   from app.agents.trader.trader_agent import TraderAgent
   agent = TraderAgent()
   cash_balance = agent.import_sipp('data/processed/SIPP/merged.csv')
   ```

3. Verify results:
   - Check portfolio shows correct number of open positions
   - Confirm cash balance matches account statement
   - Review average cost basis and unrealised P&L

**Import Logic:**
- **Trades**: Only rows with valid Symbol field (not 'n/a') imported as stock trades
- **Special case**: HSBC GLOB funds (Symbol='n/a', description contains "HSBC GLOB") use fixed ticker 'HSFWA'
- **Cash flows**: Non-trade entries (contributions, tax relief, interest, dividends) stored in separate cash_flows table
- **Chronological ordering**: Trades replayed in date order (DD/MM/YYYY format)
- **Cash position**: Final Running Balance used as authoritative cash balance

**Troubleshooting SIPP imports:**
- Too many positions? Ensure Symbol column is filled for all trades (not 'n/a')
- Negative shares? Check for over-sells or missing buy transactions
- Cash mismatch? Verify final Running Balance matches account statement

## Troubleshooting

**Scanner returns 0 tickers:**
- Check extraction_results.json exists
- Verify yfinance is accessible

**No alerts despite high scores:**
- Check alert cooldown (alerts.db)
- Verify email configuration (.env EMAIL_* vars)
- Check ALERT_THRESHOLD in alert_agent.py

**Trader not executing orders:**
- Verify Alpaca API credentials (.env ALPACA_* vars)
- Check portfolio has sufficient cash
- Review trader_agent.py logs

## Contributing

1. Reference relevant spec before starting work
2. Check extension guide if adding new capability
3. Run tests and type checks before opening PR
4. Update spec if behavior changes (use openspec propose/apply)

See [CLAUDE.md](.claude/CLAUDE.md) for detailed workflow.

## License

Internal use only.
