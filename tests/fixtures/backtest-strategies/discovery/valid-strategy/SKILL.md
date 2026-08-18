---
kind: backtest-strategy
name: valid-strategy
display_name: Valid Strategy
description: A minimal fixture Strategy proving successful discovery end to end.
api_version: 1
parameters:
  - name: watch_security_id
    type: string
    default: sec-aapl
    description: Security to watch.
    required: true
  - name: fixed_shares
    type: integer
    default: 1
    description: Fixed share count to buy.
    required: false
    minimum: 1
    maximum: 100
---

# Valid Strategy (discovery fixture)

Test-only fixture proving `discover_strategies` end to end. Never a real
trading rule and never discoverable outside `tests/backtest/test_skill_discovery.py`.
