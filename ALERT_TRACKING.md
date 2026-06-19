# Alert Email Tracking

Email alert send history is now tracked in the `app/agents/alert/alerts.db` database for future analysis.

## Database Schema

### `email_sends` Table
Records every email sent by the alert system:
- `id` — Unique send ID
- `sent_at` — ISO timestamp when email was sent (UTC)
- `email_type` — Type of email: `'summary'`, `'entry_triggered'`, or `'stop_loss_hit'`
- `subject` — Email subject line
- `stocks_included` — JSON array of ticker symbols included in the email
- `buy_count` — Number of buy/breakout alerts in this email
- `sell_count` — Number of sell/stop-loss alerts in this email

### `alerts` Table (Updated)
New column added:
- `emailed_in_send_id` — Foreign key reference to the email_send record if this alert was included in a summary email

## Tracked Email Types

1. **summary** — Daily consolidated summary email containing portfolio snapshot and multiple alerts
2. **entry_triggered** — Individual alert when a watched position crosses its entry price
3. **stop_loss_hit** — Individual alert when a position hits its stop-loss level

## Query Examples

### View recent email sends
```sql
SELECT sent_at, email_type, subject, stocks_included, buy_count, sell_count
FROM email_sends
ORDER BY sent_at DESC
LIMIT 10;
```

### Count emails by type
```sql
SELECT email_type, COUNT(*) as count, SUM(buy_count) as total_buy_alerts, SUM(sell_count) as total_sell_alerts
FROM email_sends
GROUP BY email_type;
```

### Find all emails that included a specific ticker
```sql
SELECT sent_at, email_type, subject
FROM email_sends
WHERE stocks_included LIKE '%"AAPL"%'
ORDER BY sent_at DESC;
```

### Alert effectiveness analysis (track which alerts became trades)
```sql
SELECT 
    a.ticker,
    a.alerted_at,
    a.entry_price,
    a.status,
    es.email_type
FROM alerts a
LEFT JOIN email_sends es ON a.emailed_in_send_id = es.id
WHERE a.status IN ('entered', 'stopped')
ORDER BY a.alerted_at DESC;
```

## Usage

### Initialize email tracking (one-time setup)
```bash
python init_email_tracking.py
```

### Query alert history
```bash
python query_alert_history.py
```

This shows:
- Recent email sends with details
- Summary statistics by email type
- Recent alerts and their status
