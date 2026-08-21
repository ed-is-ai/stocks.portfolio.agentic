---
kind: backtest-strategy
name: duplicate-strategy-one
display_name: Shared Display Name
description: First of two folders whose canonicalized display identity collides.
api_version: 1
runtime_files:
  - scripts/strategy.py
strategy_universe:
  schema_version: strategy_universe.v1
  mode: selected-securities
  parameter: selected_securities
---

# Duplicate Strategy One (discovery fixture)
