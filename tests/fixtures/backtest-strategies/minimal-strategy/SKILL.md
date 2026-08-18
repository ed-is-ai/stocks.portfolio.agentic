---
name: minimal-strategy
description: >
  Test-only fixture Strategy proving StrategyProtocolV1 conformance. Not a
  real trading rule -- exists solely to exercise the Story 2.1 protocol
  boundary end to end. Never discoverable as a live Skill.
---

# Minimal Strategy (test fixture)

Deterministic reference implementation of `StrategyProtocolV1` used by
`tests/backtest/test_strategy_protocol.py` and
`tests/backtest/test_strategy_runtime_import_boundary.py`.

- `entry_signals` emits one `BUY` for the parameterized `watch_security_id`.
- `exit_signals` emits one `SELL` per held position in the supplied
  `PortfolioView`.
- `position_size` returns the parameterized `fixed_shares` (default `1`).

See `docs/strategy-manager/strategy-authoring-v1.md` for the authoring
convention this fixture follows.

## Contract tests

```bash
uv run pytest tests/fixtures/backtest-strategies/minimal-strategy/scripts/tests -q
```
