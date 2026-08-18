---
kind: backtest-strategy
name: invalid-default
description: Declares a numeric parameter default outside its own bounds.
api_version: 1
parameters:
  - name: threshold
    type: integer
    default: 999
    description: Declared default (999) exceeds the declared maximum (10).
    required: false
    minimum: 1
    maximum: 10
---

# Invalid declared default (discovery fixture)
