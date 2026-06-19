# Onboarding Guide: Understanding Agents.Stocks

Welcome to the Agents.Stocks team! This guide walks you through understanding the system architecture and codebase. **Budget 2-3 hours** to go from zero to productive.

## Phase 1: System Overview (30 minutes)

Start with the **System Architecture Spec**: [openspec/specs/system-architecture/spec.md](openspec/specs/system-architecture/spec.md)

**What to understand:**
- How the 5 agents connect in a pipeline
- Data flow: extraction → scanning → analysis → alerts → trading
- Orchestrator runs on market schedule (9:30 AM - 4:00 PM ET)
- Run logging (pipeline_runs.csv), portfolio tracking (portfolio_value.csv)
- Error handling: if one agent fails, subsequent agents don't execute

**After reading, you should know:**
- The sequence of agents and what each produces
- How tickers flow through the system
- Where to find output files (scan_results.json, analysis_results.json, etc.)

---

## Phase 2: Data Models (20 minutes)

Read **Data Models Spec**: [openspec/specs/data-models/spec.md](openspec/specs/data-models/spec.md)

**What to understand:**
- **StockRecord**: The output of Scanner (price, volume, technicals, fundamentals)
- **StockAnalysis**: The output of Analyst (score, stage, entry_zone, recommendation)
- **Position**: A single open or closed trade
- **CANSLIMScore & MomentumScore**: Component scores feeding overall recommendation

**Field Meanings Matter:**
- `price_history` is oldest→newest order (important for technical calculations)
- `rel_volume` is decimal (1.5 = 150% of average)
- Optional fields are None (not 0, not empty string)
- All scores are 0-10 range

**After reading, you should know:**
- What fields each model has
- Why each field exists and what it represents
- How models connect (Scanner → StockRecord, Analyst → StockAnalysis)

---

## Phase 3: Choose Your Agent (1-2 hours)

Pick ONE agent to focus on and read its specification deeply. Based on your interests:

### **Option A: Data Collection**
Read **Scanner Agent Spec**: [openspec/specs/scanner-agent/spec.md](openspec/specs/scanner-agent/spec.md)

**Focus Areas:**
- How price/volume data flows from yfinance
- Technical indicators (SMA, RSI, ATR) and why they matter
- Enrichment from external APIs (Alpha Vantage, Congress, WhaleWisdom)
- Error handling when APIs fail

**Then read:**
- Extension guide: [How to add a new data source](openspec/specs/scanner-agent/extension-guide.md)

**Real code to explore:**
- `agents/scanner/scanner_agent.py` — Main scan loop
- `agents/scanner/alpha_vantage_client.py` — Example API client
- `models.py` — StockRecord definition

---

### **Option B: Stock Scoring & Analysis**
Read **Analyst Agent Spec**: [openspec/specs/analyst-agent/spec.md](openspec/specs/analyst-agent/spec.md)

**Focus Areas:**
- CANSLIM scoring methodology (C, A, N, S, L, I, M components)
- Weinstein Stage classification (1-4)
- VCP (Volatility Contraction Pattern) detection
- Entry zone determination and recommended actions

**Then read:**
- Extension guide: [How to add new scoring framework](openspec/specs/analyst-agent/extension-guide.md)

**Real code to explore:**
- `agents/analyst/analyst_agent.py` — Main analysis loop
- `skills/vcp-screener/scripts/` — VCP pattern calculators

---

### **Option C: Notifications & Alerting**
Read **Alert Agent Spec**: [openspec/specs/alert-agent/spec.md](openspec/specs/alert-agent/spec.md)

**Focus Areas:**
- Alert filtering (buy/sell signals)
- Cooldown enforcement (24-hour per ticker deduplication)
- Email formatting and sending
- Alert history tracking (SQLite)
- Portfolio summary reporting

**Then read:**
- Extension guide: [How to add new alert channel](openspec/specs/alert-agent/extension-guide.md)

**Real code to explore:**
- `agents/alert/alert_agent.py` — Main alerting logic
- `app/agents/alert/alerts.db` — SQLite alert history

---

### **Option D: Trade Execution**
Read **Trader Agent Spec**: [openspec/specs/trader-agent/spec.md](openspec/specs/trader-agent/spec.md)

**Focus Areas:**
- Order execution (market vs. limit orders)
- Position sizing and risk management
- Portfolio risk limits (max 5% per position, 2% per trade, 10 positions max)
- P&L tracking
- Broker integration (currently Alpaca)

**Then read:**
- Extension guide: [How to integrate new broker](openspec/specs/trader-agent/extension-guide.md)

**Real code to explore:**
- `agents/trader/trader_agent.py` — Main trading logic
- Models: Position class for trade records

---

### **Option E: Watchlist Sourcing**
Read **Extraction Agent Spec**: [openspec/specs/extraction-agent/spec.md](openspec/specs/extraction-agent/spec.md)

**Focus Areas:**
- Multi-source watchlist aggregation
- Quality gates (filtering by volume, price, etc.)
- Source tagging (tracking which source included each ticker)
- Union logic and deduplication
- Context data (e.g., institutional buying strength from WhaleWisdom)

**Then read:**
- Extension guide: [How to add new watchlist source](openspec/specs/extraction-agent/extension-guide.md)

**Real code to explore:**
- `agents/extraction/extraction_agent.py` — Main extraction logic
- `agents/extraction/tv_extractor.py` — TradingView source example

---

## Phase 4: Set Up Development Environment

```bash
# Clone repo (if not already done)
git clone https://github.com/your-org/agents-stocks.git
cd agents-stocks

# Install Python dependencies
uv add --dev pytest pyrefly

# Verify setup
uv run pytest
uv run pyrefly check
```

Check `CLAUDE.md` for development guidelines: code style, testing, type hints, etc.

---

## Phase 5: Run the System (Optional)

### Test a single agent
```bash
# Test Scanner Agent
uv run python agents/scanner/scanner_agent.py

# Test Analyst Agent
uv run python agents/analyst/analyst_agent.py
```

### Run full pipeline
```bash
# Orchestrator runs agents in sequence (if market is open)
uv run python orchestrator.py
```

### Check outputs
```bash
# View scan results
cat app/agents/scanner/scan_results.json | head -50

# View analysis results
cat app/agents/analyst/analysis_results.json | head -50

# View pipeline metrics
tail -5 pipeline_runs.csv

# View portfolio value
tail -5 portfolio_value.csv
```

---

## Phase 6: Your First Task

### If You Chose Scanner Agent
**Task**: Add a new data field to StockRecord
1. Identify data source (what API provides this field?)
2. Create/use client to fetch data
3. Add field to StockRecord model in `models.py`
4. Add fetch call to Scanner.scan() loop
5. Test: run scanner, verify new field populated
6. Reference: [Scanner extension guide](openspec/specs/scanner-agent/extension-guide.md)

### If You Chose Analyst Agent
**Task**: Add a new scoring component
1. Create new calculator (see extension guide template)
2. Integrate into Analyst.analyze() loop
3. Add score field to StockAnalysis model
4. Test: verify scores computed and stored
5. Reference: [Analyst extension guide](openspec/specs/analyst-agent/extension-guide.md)

### If You Chose Alert Agent
**Task**: Add a new notification channel (Slack, SMS, Discord)
1. Create channel notifier with send() method
2. Add channel config to models.py
3. Integrate into Alert.send_alert() method
4. Add environment variables to .env
5. Test: send test alert via new channel
6. Reference: [Alert extension guide](openspec/specs/alert-agent/extension-guide.md)

### If You Chose Trader Agent
**Task**: Add broker integration (dry-run mode)
1. Create broker adapter implementing BrokerAdapter interface
2. Add broker config to models.py
3. Update Trader.execute_order() to use adapter
4. Test in dry-run mode (no real orders)
5. Reference: [Trader extension guide](openspec/specs/trader-agent/extension-guide.md)

### If You Chose Extraction Agent
**Task**: Add a new watchlist source
1. Create source fetcher with get_tickers() method
2. Add source config to models.py
3. Integrate into Extraction.extract() loop
4. Test: verify tickers returned and tagged
5. Reference: [Extraction extension guide](openspec/specs/extraction-agent/extension-guide.md)

---

## Quick Reference

### Essential Files
- `models.py` — All Pydantic data models
- `orchestrator.py` — Main scheduler
- `agents/scanner/scanner_agent.py` — Data collection
- `agents/analyst/analyst_agent.py` — Stock scoring
- `agents/alert/alert_agent.py` — Notifications
- `agents/trader/trader_agent.py` — Trade execution
- `agents/extraction/extraction_agent.py` — Watchlist sourcing

### Specs Location
- System: `openspec/specs/system-architecture/spec.md`
- Data Models: `openspec/specs/data-models/spec.md`
- Agents: `openspec/specs/<agent>/spec.md`
- Extension Guides: `openspec/specs/<agent>/extension-guide.md`

### Common Commands
```bash
# Run tests
uv run pytest

# Check types
uv run pyrefly check

# Format code
uv run ruff format .

# Run specific agent test
uv run pytest tests/test_scanner.py -v
```

### Key Concepts
- **Pydantic Models**: Type-safe data structures
- **Agents**: Independent processing steps (Scanner, Analyst, Alert, Trader, Extraction)
- **Pipeline**: Agents execute in sequence (Extraction → Scanner → Analyst → Alert → Trader)
- **Specs**: Formal documentation of agent behavior, design decisions, extension points
- **Extension Guides**: Step-by-step how-tos for common extensions

---

## Getting Help

### Questions?
1. **Check the spec** for your agent — most questions answered there
2. **Read extension guides** for patterns on how to add things
3. **Look at existing code** — patterns already in use
4. **Ask your team lead** — they know context beyond specs

### Found a bug?
1. Document in spec as TODO (under Known Issues)
2. Create issue on GitHub with reproduction steps
3. Reference relevant spec requirement number

### Want to improve onboarding?
1. Submit PRs to this doc
2. Update specs if they're wrong
3. Add examples to extension guides

---

## Next Steps After Onboarding

1. **Explore the codebase** — Pick an agent and read the full implementation
2. **Run tests** — Understand existing test patterns
3. **Complete your first task** — Add a small feature to your chosen agent
4. **Submit PR** — Reference spec requirements in commit message
5. **Join team meetings** — Get context on ongoing work

**Welcome to the team! 🚀**
