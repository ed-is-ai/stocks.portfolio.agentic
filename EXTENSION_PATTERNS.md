# Extension Patterns: How to Add Features to Agents.Stocks

This document indexes all extension guides and common patterns for extending the system.

Each agent has a detailed, step-by-step extension guide. **Start with the guide for your use case**, then reference the detailed spec if you need clarification.

---

## Quick Reference

| What You Want to Do | Agent | Guide |
|---|---|---|
| Add a new data source (API, screener, dataset) | Scanner | [extension-guide.md](openspec/specs/scanner-agent/extension-guide.md) |
| Add a new scoring framework (quality, dividend, ESG) | Analyst | [extension-guide.md](openspec/specs/analyst-agent/extension-guide.md) |
| Add a new alert channel (Slack, SMS, Discord) | Alert | [extension-guide.md](openspec/specs/alert-agent/extension-guide.md) |
| Integrate a new broker (Interactive Brokers, TD Ameritrade) | Trader | [extension-guide.md](openspec/specs/trader-agent/extension-guide.md) |
| Add a new watchlist source (Finviz, custom screener) | Extraction | [extension-guide.md](openspec/specs/extraction-agent/extension-guide.md) |

---

## Agent-Specific Guides

### Scanner Agent: Add a New Data Source

**Guide**: [openspec/specs/scanner-agent/extension-guide.md](openspec/specs/scanner-agent/extension-guide.md)

**Use case**: You want to enrich StockRecord with additional data (earnings surprises, sentiment scores, supply chain risk, custom metrics).

**Pattern**:
1. Create `<source>_client.py` with fetch_data() method
2. Handle API failures gracefully (try-except, return empty dict)
3. Add client instantiation to scanner_agent.py
4. Add fetch call to scan() loop
5. Add new fields to StockRecord model in models.py
6. Add environment variables to .env
7. Test integration
8. Update spec if Analyst should use new fields

**Examples**:
- Add earnings surprise data from Alpha Vantage
- Add sentiment score from Twitter API
- Add supply chain risk from custom source
- Add company news relevance from NLP model

**Key Concepts**:
- Graceful degradation (missing data = None, not 0)
- API rate limits (add backoff, cache if needed)
- Data consistency (always return same fields or None)

**Checklist**: 8 items in extension guide

---

### Analyst Agent: Add a New Scoring Framework

**Guide**: [openspec/specs/analyst-agent/extension-guide.md](openspec/specs/analyst-agent/extension-guide.md)

**Use case**: You want to score stocks using a new methodology (quality score, dividend metrics, growth profiling, ML model).

**Pattern**:
1. Create `<framework>_calculator.py` with calculate() method
2. Return scores in 0-10 range (for weighting consistency)
3. Handle missing data gracefully
4. Add calculator instantiation to analyst_agent.py
5. Add call to analyze() loop
6. Create <FrameworkScore> model in models.py
7. Update overall score weighting (if material)
8. Update entry zone / recommended action logic (if needed)
9. Update spec
10. Test downstream impact (Alert, Trader agents)

**Examples**:
- Add balance sheet quality score (ROE, debt, margins)
- Add dividend yield and growth
- Add ML model predictions (earnings, price target)
- Add industry momentum
- Add ESG rating

**Key Concepts**:
- Score normalization (0-10 range)
- Weighting (how much does this framework influence overall score?)
- Vetoing (can framework block trades? Or just inform?)

**Checklist**: 10 items in extension guide

---

### Alert Agent: Add a New Alert Channel

**Guide**: [openspec/specs/alert-agent/extension-guide.md](openspec/specs/alert-agent/extension-guide.md)

**Use case**: You want to send alerts via a new channel (Slack, SMS, Discord, webhook, push notification).

**Pattern**:
1. Create `<channel>_notifier.py` with send() method
2. Handle failures gracefully (try-except, return bool)
3. Define <ChannelConfig> in models.py
4. Add notifier instantiation to alert_agent.py
5. Integrate into send_alert() method (all enabled channels)
6. Add environment variables to .env/.env.example
7. Test notifier in isolation
8. Test Alert Agent with new channel
9. Verify channel fails independently (doesn't crash other channels)
10. Create user documentation (CHANNELS.md)
11. Update spec (if significant)

**Examples**:
- Slack: Webhook-based (blocks with colored sections, emoji indicators)
- SMS: Twilio integration (concise format, max 160 chars)
- Discord: Webhook embeds (rich formatting, links)
- Email: Additional SMTP provider (Sendgrid, AWS SES)
- Push notification: Firebase Cloud Messaging
- Webhook: HTTP POST to custom endpoint

**Key Concepts**:
- Channel-specific formatting (SMS ≤160 chars, Slack rich, email HTML)
- Rate limits (some channels throttle)
- Channel independence (if Slack down, email still sends)
- Configuration (per-channel settings in .env)

**Checklist**: 10 items in extension guide

**Rate Limits by Channel**:
- Slack: ~60 msg/min per webhook ✓
- SMS (Twilio): $0.01-0.02 per msg, consider batching
- Discord: ~10 msg/10sec per webhook ✓
- Email: Depends on SMTP (Gmail ~300/day)

---

### Trader Agent: Integrate a New Broker

**Guide**: [openspec/specs/trader-agent/extension-guide.md](openspec/specs/trader-agent/extension-guide.md)

**Use case**: You want to execute trades via a different broker (Interactive Brokers, TD Ameritrade, Robinhood, Polygon, etc.).

**Pattern**:
1. Create `<broker>_adapter.py` implementing BrokerAdapter interface
2. Implement: place_order(), get_positions(), get_cash(), get_order_status(), cancel_order()
3. Handle errors gracefully
4. Define <BrokerConfig> in models.py
5. Create adapter factory in trader_agent.py
6. Update Trader Agent to use _broker adapter
7. Add environment configuration to .env/.env.example
8. Test adapter in isolation
9. Test Trader Agent with new broker
10. Verify risk limits still enforced
11. Create user documentation (BROKERS.md)
12. Update spec if broker introduces constraints

**Examples**:
- Interactive Brokers (TWS, API, complex order types)
- TD Ameritrade (thinkorswim API, advanced options)
- Robinhood (web-based, simple stocks/ETFs)
- Polygon (commission-free, fractional shares)
- Tastytrade (options-focused)

**Key Concepts**:
- Standard interface (all brokers implement same methods)
- Order types (market, limit, stop; some brokers support more)
- Account constraints (margin, day trade rules, minimum deposits)
- Timeouts (API calls should fail fast, not hang pipeline)

**Checklist**: 12 items in extension guide

**Broker Considerations**:
- **Market Hours**: Some brokers allow pre/post-market
- **Order Types**: Not all brokers support all order types
- **Fractional Shares**: Some brokers only allow whole shares
- **Options**: Some focus on equities, some on derivatives
- **Commissions**: Now mostly free, verify fees in spec

---

### Extraction Agent: Add a New Watchlist Source

**Guide**: [openspec/specs/extraction-agent/extension-guide.md](openspec/specs/extraction-agent/extension-guide.md)

**Use case**: You want to include stocks from a new screener or data source (Finviz, FinanceAI, custom screener, your own ranking system).

**Pattern**:
1. Create `<source>_fetcher.py` with get_tickers() method
2. Return list of strings (unique tickers within source)
3. Implement optional get_context() for per-ticker metadata
4. Handle API failures gracefully (return empty list)
5. Define <SourceConfig> in models.py
6. Add fetcher instantiation to extraction_agent.py
7. Integrate into extract() loop
8. Update extraction_results.json format to track source tags
9. Update Scanner.load_watchlist() to parse new format
10. Update Scanner.load_source_map() to extract source tags
11. Add environment configuration
12. Test fetcher and Extraction Agent
13. Test Scanner compatibility
14. Add source health logging
15. Update spec

**Examples**:
- Finviz screener (pre-market movers, breakout patterns)
- FinanceAI model (ML-ranked stocks)
- Custom internal screener (your own rules)
- Earnings announcements (earnings season focus)
- New IPOs (newly public companies)
- Sector rotations (rotation signals)

**Key Concepts**:
- Deduplication (no duplicate tickers within source)
- Quality gates (filter by volume, price, etc.)
- Source tagging (track which source included each ticker)
- Context metadata (optional: rank, confidence, category)
- Error handling (one source failing doesn't break others)

**Checklist**: 13 items in extension guide

**Common Pitfalls to Avoid**:
- Fetcher crashes instead of returning empty list
- High latency blocks entire extraction
- Duplicate tickers inflate watchlist
- Invalid ticker symbols passed to Scanner
- Rate limits cause API 429 responses
- Too many sources cause extraction > 1 minute

---

## Implementation Workflow

### Step 1: Choose Your Extension

Identify what you want to add:
- New data source? → Scanner
- New scoring method? → Analyst
- New notification channel? → Alert
- New broker? → Trader
- New watchlist source? → Extraction

### Step 2: Read the Guide

Navigate to agent's extension guide (links above). Follow step-by-step instructions.

### Step 3: Follow the Template

Each guide provides:
- Code template/example
- Configuration setup
- Testing strategy
- Common pitfalls
- Checklist for verification

### Step 4: Test

Before submitting PR:
- Test your component in isolation
- Test integration with agent
- Test downstream impact (if applicable)
- Run existing tests (ensure no regressions)
- Type check: `uv run pyrefly check`
- Format: `uv run ruff format . && uv run ruff check . --fix`

### Step 5: Update Spec (Optional)

If your extension is significant:
- Add requirement to agent's spec
- Document in spec's "Extension Points" section
- Reference extension guide from spec

### Step 6: Submit PR

- Reference spec requirement number (if changed)
- Link extension guide in commit message
- Include test evidence (screenshots, test output)

---

## Common Patterns

### Pluggable Architecture (Alert Channels, Trader Brokers)

Pattern: Interface + Implementations

```
BrokerAdapter (interface)
    ├── AlpacaAdapter
    ├── InteractiveBrokersAdapter
    └── RobinhoodAdapter

Factory function selects at runtime:
    BROKER=alpaca → AlpacaAdapter
    BROKER=ib → InteractiveBrokersAdapter
```

**Benefits**: Easy to swap implementations, test with mocks, add new without changing core logic

**Where Used**:
- Alert channels (email, Slack, SMS, Discord, etc.)
- Trader brokers (Alpaca, Interactive Brokers, etc.)

### Pipeline Chain (Data Flow)

Pattern: Each agent passes output to next

```
Extraction → extraction_results.json → Scanner
Scanner → scan_results.json → Analyst
Analyst → analysis_results.json → Alert → Trader
```

**Benefits**: Agents operate independently, output is persistent, easy to debug

**Constraint**: Output schema is locked (downstream depends on it)

### Configuration Injection

Pattern: Config classes + environment variables

```python
class AlpacaConfig(BaseModel):
    api_key: str
    api_secret: str
    base_url: str

# Instantiate from .env
config = AlpacaConfig(
    api_key=os.getenv("ALPACA_API_KEY"),
    api_secret=os.getenv("ALPACA_API_SECRET"),
    base_url=os.getenv("ALPACA_BASE_URL"),
)
```

**Benefits**: Secrets not in code, easy to switch environments, testable (inject mock config)

### Graceful Degradation

Pattern: API failures don't crash agent

```python
def fetch_data(ticker):
    try:
        response = call_api(ticker)
        return parse(response)
    except Exception as e:
        logger.warning(f"API failed: {e}")
        return {}  # Empty/None, don't crash
```

**Benefits**: One broken API doesn't block entire scan, helps system resilience

---

## Testing Extensions

### Unit Test

Test your component in isolation:

```python
# Test data source
def test_alpha_vantage_client():
    client = AlphaVantageClient(api_key="test")
    data = client.fetch_data("AAPL")
    assert "eps_growth" in data or data == {}

# Test calculator
def test_quality_calculator():
    calc = QualityCalculator()
    stock = StockRecord(roe=0.15, ...)
    score = calc.calculate(stock)
    assert 0 <= score["roe_quality"] <= 10

# Test notifier
def test_slack_notifier():
    notifier = SlackNotifier(config)
    alert = AlertMessage(ticker="AAPL", score=9, ...)
    success = notifier.send(alert)
    assert isinstance(success, bool)
```

### Integration Test

Test with actual agent:

```python
# Test Scanner with new data source
def test_scanner_with_new_source():
    scanner = ScannerAgent()
    results = scanner.scan(["AAPL", "MSFT"])
    assert results[0].<new_field> is not None or None
    assert isinstance(results[0], StockRecord)

# Test Analyst with new scoring framework
def test_analyst_with_new_framework():
    analyst = AnalystAgent()
    results = analyst.analyze([stock])
    assert results[0].<new_framework>_score is not None
    assert 0 <= results[0].overall_score <= 10

# Test Alert with new channel
def test_alert_with_slack():
    alert_agent = AlertAgent(slack_enabled=True)
    alert_agent.run(analysis_results.json)
    # Verify message received in Slack
```

### Regression Test

Ensure you didn't break existing functionality:

```bash
# Run all tests
uv run pytest

# Run tests for affected agent
uv run pytest tests/test_scanner.py -v
```

---

## Checklist for Extension PR

- [ ] Code follows project style (ruff format, pyrefly check pass)
- [ ] New functionality has unit tests
- [ ] Integration tests pass (agent runs with new feature)
- [ ] Existing tests still pass (no regressions)
- [ ] Extension guide followed (step-by-step)
- [ ] Error handling in place (graceful degradation)
- [ ] Configuration added to .env/.env.example
- [ ] Docstrings added (if new public functions)
- [ ] Spec updated (if behavior changed)
- [ ] Commit message references extension guide

---

## Getting Help

1. **Read the guide** — Most questions answered there with examples
2. **Look at existing code** — Patterns already implemented (e.g., alpha_vantage_client.py)
3. **Check the spec** — More detailed rationale and constraints
4. **Ask team lead** — For context beyond documentation

---

## References

- **Extension Guides**: [openspec/specs/*/extension-guide.md](openspec/specs/)
- **Specifications**: [openspec/specs/*/spec.md](openspec/specs/)
- **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md)
- **Development Guide**: [CLAUDE.md](.claude/CLAUDE.md)
- **Code Examples**: Source code in `agents/`, `skills/` directories
