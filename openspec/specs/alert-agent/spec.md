## ADDED Requirements

### Requirement: Alert Agent SHALL send email notifications for actionable buy signals
Alert Agent reads analysis_results.json and sends email alerts when a stock meets buy criteria: recommended_action="BUY" and score above threshold.

#### Scenario: Send buy alert for high-conviction setup
- **WHEN** Analyst recommends "BUY" with score 8-10
- **THEN** Alert Agent:
  - Composes HTML email with ticker, score, stage, entry zone, summary, and actionable entry details
  - Sends via SMTP (Gmail or configured server)
  - Logs alert to alerts.db with ticker, timestamp, score
  - Email includes link to Yahoo Finance for quick reference

#### Scenario: Suppress low-conviction buy alerts
- **WHEN** Analyst recommends "BUY" but score is 5-6 (marginal)
- **THEN** Alert Agent:
  - Checks threshold setting (default: score ≥ 7 for alert)
  - Does not send email
  - Logs to summary only if requested

### Requirement: Alert Agent SHALL implement alert cooldown to prevent spam
Alert Agent enforces 24-hour cooldown per ticker: if a buy alert was sent for ticker X, no new alert for X is sent for 24 hours, even if conditions remain favorable. Cooldown is tracked in SQLite database (alerts.db).

#### Scenario: First buy alert on AAPL
- **WHEN** AAPL triggers buy signal on Day 1
- **THEN** Alert Agent:
  - Checks alerts.db: no previous alert for AAPL
  - Sends email alert
  - Records: ticker="AAPL", alert_type="BUY", timestamp=Day1_09:45, next_alert_time=Day2_09:45

#### Scenario: Follow-up alert suppressed by cooldown
- **WHEN** AAPL triggers buy signal again on Day 1 at 14:00 (same day)
- **THEN** Alert Agent:
  - Checks alerts.db: last alert for AAPL was Day1_09:45, cooldown until Day2_09:45
  - Suppresses email alert (cooldown active)
  - Logs to debug: "AAPL alert suppressed by 24h cooldown"

#### Scenario: Follow-up alert sent after cooldown expires
- **WHEN** AAPL triggers buy signal on Day 2 at 10:00 (>24h after first alert)
- **THEN** Alert Agent:
  - Checks alerts.db: cooldown expired
  - Sends email alert (new signal cycle)
  - Updates alerts.db with new timestamp

### Requirement: Alert Agent SHALL send summary email with portfolio snapshot
Regardless of buy signals, Alert Agent sends a daily summary email at market close (or end of pipeline run) containing: number of signals generated today, portfolio value, open positions, net P&L, and high-conviction opportunities.

#### Scenario: Daily portfolio summary sent
- **WHEN** pipeline completes, Alert Agent prepares summary
- **THEN** composes email with:
  - Portfolio snapshot: total value, number of open positions, net realized/unrealized P&L
  - Today's activity: X buy signals, Y sell signals, Z updates
  - Top opportunities: 3 highest-conviction setups approaching entry
  - Market context: SPY trend, sector leaders
- **AND** sends to configured recipient(s)

### Requirement: Alert Agent SHALL sort and highlight highest-conviction buys
Alert Agent prioritizes buy signals by conviction (score × entry_zone proximity), so trader sees highest-probability setups first. Highest-conviction buys are highlighted in summary and get individual alerts.

#### Scenario: Multi-signal day with priority ranking
- **WHEN** 5 stocks trigger buy signals on same day
- **THEN** Alert Agent:
  - Ranks by conviction: (AAPL 9/10 approaching), (MSFT 8/10 broken_out), (TSLA 7/10 approaching), etc.
  - Sorts summary: highest conviction first
  - Sends individual alerts for top 2-3 (configurable)
  - Includes rest in summary only

### Requirement: Alert Agent SHALL track alert history and deduplication
All alerts (sent or suppressed) are logged to alerts.db with: ticker, alert_type (BUY/SELL), timestamp, conviction_score, reason (if suppressed). This enables analysis of alert patterns and prevents duplicates within cooldown window.

#### Scenario: Query alert history
- **WHEN** trader reviews past week of alerts
- **THEN** can query alerts.db to see:
  - Which tickers fired alerts and how many times
  - Cooldown reasons (why alert was suppressed)
  - Conviction scores for each alert
  - Success rate (did signal lead to profitable trade?)

### Requirement: Alert Agent SHALL support multi-channel alerting (email extensible)
Default is email (SMTP). Architecture allows extension to SMS, Slack, webhook, etc. Current implementation supports email only; new channels can be added following extension pattern.

#### Scenario: Extension point for new alert channel
- **WHEN** developer wants to add Slack notifications
- **THEN** creates slack_notifier.py with send_alert(message) method
- **AND** adds call to Alert Agent: `if slack_enabled: slack_notifier.send(...)`
- **AND** no changes to cooldown logic, deduplication, or summary generation (remains channel-agnostic)

### Requirement: Alert Agent SHALL handle buy and sell signals separately
Alert Agent processes both buy and sell recommendations. Buy alerts trigger entry candidates; sell alerts flag exit signals for open positions. Sell alerts bypass cooldown for risk management (exit shouldn't be delayed).

#### Scenario: Sell signal for open position
- **WHEN** Analyst recommends "SELL" for position with open P&L
- **THEN** Alert Agent:
  - Sends immediate sell alert (no cooldown)
  - Includes exit reasoning and suggested exit price
  - Marks as high-priority (red highlight)

#### Scenario: Sell signal vs. "HOLD" signal difference
- **WHEN** Analyst recommends "HOLD" (good stock, poor timing)
- **THEN** Alert Agent:
  - Does not send alert (no action needed)
  - May include in summary as "monitor for pullback"

### Requirement: Alert Agent SHALL format alerts with links and trading data
Each alert email includes: ticker symbol, company name (if available), entry price/zone, recommended position size (if provided), stop-loss level, target price, rationale summary, and clickable links (Yahoo Finance, TradingView for chart).

#### Scenario: Detailed alert email format
- **WHEN** Alert Agent composes buy alert for NVDA
- **THEN** email contains:
  - Subject: "Buy Alert: NVDA (Score 9/10)"
  - Body:
    - Ticker & Company: "NVDA - NVIDIA Corporation"
    - Current Price: $850
    - Entry Zone: $840-850 (high_base to breakout level)
    - Stop Loss: $820 (handle_low)
    - Target: $950+ (next resistance)
    - Rationale: "Stage 2 breakout, institutional buying, earnings accelerating"
    - Links: [Yahoo Finance](link), [TradingView](link)

### Requirement: Alert Agent architecture constraints
Alert Agent depends on Analyst Agent (reads analysis_results.json). Constraints: (1) Input MUST be valid StockAnalysis objects with score and recommended_action fields (Analyst dependency), (2) Cooldown MUST be enforced per ticker (skipping causes alert spam), (3) Email credentials MUST be properly configured (alerts fail silently if missing), (4) alerts.db MUST persist across runs (enable continuity across days).

#### Scenario: Constraint: analysis_results.json dependency
- **WHEN** Analyst changes StockAnalysis.score field (e.g., to float with decimal precision)
- **THEN** Alert Agent's threshold checks may behave unexpectedly
- **AND** must coordinate schema change with Alert team

#### Scenario: Constraint: cooldown persistence
- **WHEN** alerts.db is accidentally deleted or not committed
- **THEN** cooldown is reset, all alerts re-fire
- **AND** user receives duplicate alerts for same signals
- **AND** database persistence is non-negotiable
