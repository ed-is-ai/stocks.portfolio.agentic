# Extension Guide: How to Add a New Alert Channel

Adding a new alert channel (SMS, Slack, webhook, Discord, etc.) extends how alerts reach the user without changing core alerting logic.

## Architecture Overview

Alert Agent uses a pluggable notification architecture:
- **Core Logic**: Signal detection, cooldown tracking, deduplication (independent of channel)
- **Channels**: Email, SMS, Slack, etc. (swap-in implementations)
- **Database**: SQLite for cooldown and history (persistent across channels)

Each channel is a separate module with a send() method. Core logic calls all enabled channels; channels fail independently.

## Steps to Add a New Alert Channel

### 1. Create a Channel Notifier Module

Create: `agents/alert/<channel>_notifier.py`

```python
class <ChannelName>Notifier:
    """Send alerts via <channel>."""
    
    def __init__(self, config: <ChannelConfig>):
        """Initialize with channel-specific config."""
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def send(self, alert: AlertMessage) -> bool:
        """
        Send alert via <channel>.
        
        Args:
            alert: AlertMessage with ticker, score, entry_zone, summary, etc.
        
        Returns:
            bool: True if sent successfully, False if failed.
        
        IMPORTANT: Never crash on failure. Log and return False.
        Alert Agent will retry or log if multiple channels fail.
        """
        try:
            # Format message for channel
            message = self._format_message(alert)
            
            # Send via API/service
            self._send_via_<channel>(message)
            
            self.logger.info(f"Alert sent via {channel}: {alert.ticker}")
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to send via {channel}: {e}")
            return False
    
    def _format_message(self, alert: AlertMessage) -> str:
        """Format alert for <channel> (plain text, Slack JSON, etc.)."""
        return f"""
        Buy Signal: {alert.ticker}
        Score: {alert.score}/10
        Entry Zone: {alert.entry_zone}
        Summary: {alert.summary}
        """
    
    def _send_via_<channel>(self, message: str) -> None:
        """Send message via <channel> API/service."""
        # Implementation specific to channel
        pass
```

**Example: Slack Notifier**
```python
class SlackNotifier:
    def __init__(self, config: SlackConfig):
        self.webhook_url = config.webhook_url
    
    def send(self, alert: AlertMessage) -> bool:
        try:
            payload = {
                "text": f"🚀 Buy Alert: {alert.ticker}",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*{alert.ticker}*\nScore: {alert.score}/10"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": alert.summary}},
                ]
            }
            response = requests.post(self.webhook_url, json=payload)
            response.raise_for_status()
            return True
        except Exception as e:
            self.logger.error(f"Slack send failed: {e}")
            return False
```

### 2. Define Channel Configuration

In `models.py`, add config class for new channel:

```python
class <ChannelName>Config(BaseModel):
    """Configuration for <channel> notifications."""
    enabled: bool = False
    <param_1>: str  # e.g., webhook_url, phone_number, api_key
    <param_2>: str  # e.g., api_token
    # ... other params ...
```

Example:
```python
class SlackConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    
class SMSConfig(BaseModel):
    enabled: bool = False
    phone_number: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
```

### 3. Add Channel Instantiation to Alert Agent

In `agents/alert/alert_agent.py`, instantiate at module level:

```python
from <channel>_notifier import <ChannelName>Notifier

_<channel>_notifier = <ChannelName>Notifier(
    <ChannelConfig>(
        enabled=os.getenv("<CHANNEL>_ENABLED", "false").lower() == "true",
        <param_1>=os.getenv("<CHANNEL>_<PARAM_1>", ""),
        <param_2>=os.getenv("<CHANNEL>_<PARAM_2>", ""),
    )
)
```

### 4. Integrate into Alert Sending Logic

In the `send_alert()` method, call all enabled channels:

```python
def send_alert(self, alert: AlertMessage) -> bool:
    """Send alert via all enabled channels."""
    channels_ok = []
    
    # Existing channels
    if _email_notifier.config.enabled:
        channels_ok.append(_email_notifier.send(alert))
    
    # NEW CHANNEL
    if _<channel>_notifier.config.enabled:
        channels_ok.append(_<channel>_notifier.send(alert))
    
    # Return True if at least one channel succeeded
    return any(channels_ok) if channels_ok else False
```

### 5. Add Environment Configuration

In `.env`:

```
<CHANNEL>_ENABLED=true
<CHANNEL>_<PARAM_1>=value1
<CHANNEL>_<PARAM_2>=value2
```

In `.env.example`:

```
<CHANNEL>_ENABLED=false
<CHANNEL>_<PARAM_1>=
<CHANNEL>_<PARAM_2>=
```

### 6. Handle Channel-Specific Failures Gracefully

Channels fail independently—if Slack is down, email still sends:

```python
def send_alert(self, alert: AlertMessage) -> bool:
    """Send via all enabled channels, track which succeed."""
    results = {}
    
    if _email_notifier.config.enabled:
        results["email"] = _email_notifier.send(alert)
    if _slack_notifier.config.enabled:
        results["slack"] = _slack_notifier.send(alert)
    if _sms_notifier.config.enabled:
        results["sms"] = _sms_notifier.send(alert)
    
    # Log which channels succeeded/failed
    for channel, ok in results.items():
        status = "✓" if ok else "✗"
        self.logger.info(f"{status} {channel}")
    
    # Overall success if at least one channel sent
    return any(results.values())
```

### 7. Test the Channel Integration

```python
# Test notifier directly
config = <ChannelConfig>(enabled=True, <param_1>="test_value")
notifier = <ChannelName>Notifier(config)

alert = AlertMessage(
    ticker="AAPL",
    score=9,
    entry_zone="approaching",
    summary="Strong breakout setup"
)

success = notifier.send(alert)
assert success, "Notification should send"

# Test Alert Agent with new channel
alert_agent = AlertAgent()
# (Assuming analysis_results.json has BUY signal)
alert_agent.run(analysis_results_path)
# Verify message received on channel
```

### 8. Update Spec (Optional)

If channel is significant (beyond simple implementation), document in `alert-agent/spec.md`:

```
### Requirement: Alert Agent SHALL support Slack notifications
When Slack is configured, buy/sell alerts are posted to Slack channel...
```

### 9. Document Configuration for Users

Create `docs/CHANNELS.md` explaining setup for new channel:

```markdown
## Slack Notifications

### Setup
1. Create Slack app: https://api.slack.com/apps
2. Get webhook URL from "Incoming Webhooks"
3. Set in .env:
   ```
   SLACK_ENABLED=true
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```
4. Restart alert agent

### Testing
```bash
python -c "from agents.alert.slack_notifier import SlackNotifier; ..."
```
```

## Checklist

- [ ] Created `<channel>_notifier.py` with send() method
- [ ] Handles failures gracefully (try-except, returns bool)
- [ ] Added <ChannelConfig> model to models.py
- [ ] Added notifier instantiation to alert_agent.py
- [ ] Integrated into send_alert() method
- [ ] Added environment variables to .env/.env.example
- [ ] Tested notifier and Alert Agent integration
- [ ] Verified channel fails independently (doesn't crash other channels)
- [ ] Created user documentation (CHANNELS.md)
- [ ] Updated spec if channel is significant

## Common Pitfalls

**Pitfall:** Crashing Alert Agent if channel send fails
- Reason: One broken channel blocks all alerts
- Fix: Always wrap send() in try-except, return False on failure, continue

**Pitfall:** Blocking Alert Agent on slow channel API
- Reason: Slack/SMS/etc. may be slow; holds up pipeline
- Fix: Add timeout to channel API calls, fail fast if slow

**Pitfall:** Not handling channel-specific config missing
- Reason: Agent crashes if webhook_url empty but enabled=true
- Fix: Validate config at init time, disable channel if incomplete

**Pitfall:** Changing AlertMessage schema without updating channels
- Reason: Old channels expect certain fields and break
- Fix: Keep AlertMessage stable; if adding fields, make optional

**Pitfall:** Sending too much detail in channel (API limits)
- Reason: Slack messages truncated, SMS rate limited, webhook payload huge
- Fix: Tailor message size to channel (SMS ≤160 chars, Slack ≤4000)

## Rate Limiting Considerations

Some channels have rate limits:
- **Slack**: ~60 messages/minute per webhook (OK for typical alerts)
- **SMS (Twilio)**: Billed per message ($0.01-0.02), consider batching
- **Discord**: ~10 messages/10 seconds per webhook (OK)
- **Email**: Depends on SMTP provider (Gmail: ~300 msgs/day)

Implement cooldown globally (not per-channel) in Alert Agent to avoid triggering limits.
