## Why

The Agents.Stocks system is a sophisticated multi-agent orchestration for stock portfolio management, but lacks formal specification documentation. As the system grows and new team members onboard, there's no single source of truth for agent capabilities, design decisions, or extension points. This creates friction in planning new features, refactoring with confidence, and understanding architecture constraints. Specs will serve three critical functions: onboarding clarity, feature planning guidance, and refactoring safety.

## What Changes

- Document all 5 agents (Scanner, Analyst, Alert, Trader, Extraction) with formal specifications
- Create system-level architecture specification establishing data flow and orchestration patterns
- Define data model contracts (StockRecord, Analysis, Position, etc.)
- For each agent, document design decisions, extension points, constraints, and known issues
- Establish patterns for future feature development (e.g., "to add a new data source, follow scanner-agent extension points")
- Create clear dependency graph showing what breaks if agents change

## Capabilities

### New Capabilities

- `system-architecture`: Overall system design, data flow between agents, orchestration patterns, market schedule integration, and run lifecycle
- `data-models`: Formal specification of core data contracts (StockRecord, StockAnalysis, Position, CANSLIMScore, EmailConfig, etc.)
- `scanner-agent`: Stock data collection: price/volume fetching, technical indicators, institutional data integration, data source management
- `analyst-agent`: Stock analysis & scoring: CANSLIM methodology, Weinstein Stage analysis, VCP pattern detection, actionable entry signals
- `alert-agent`: Notification system: email alerting, alert cooldown/deduplication, portfolio summary reporting, threshold configuration
- `trader-agent`: Order execution: trade placement, position sizing, risk management, broker integration (Alpaca), order tracking
- `extraction-agent`: Watchlist sourcing: multi-source aggregation (TradingView, StockTwits, WhaleWisdom), filtering, source tracking

### Modified Capabilities

(None - this is a documentation-only change with no requirement changes to existing code)

## Impact

- **Code**: No code changes; purely documentation
- **Architecture**: Documents existing architecture; does not change it
- **Team**: Onboarding becomes self-service; feature planning becomes more systematic
- **Future Development**: Extension points guide where new features plug in without breaking contracts
- **Risk**: Minimal - documentation only; no backward compatibility concerns
