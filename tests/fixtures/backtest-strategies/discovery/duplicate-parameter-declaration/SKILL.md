---
kind: backtest-strategy
name: duplicate-parameter-declaration
description: Declares two parameters sharing the same name.
api_version: 1
parameters:
  - name: threshold
    type: integer
    default: 1
    description: First declaration of "threshold".
    required: false
  - name: threshold
    type: integer
    default: 2
    description: Second, colliding declaration of "threshold".
    required: false
---

# Duplicate parameter declaration (discovery fixture)

Test-only fixture proving `validate_strategy_parameters`'s
`DUPLICATE_PARAMETER_DECLARATION` check is reachable through
`discover_strategies` end-to-end, isolating this Strategy with an
`invalid_parameter_schema` warning rather than only being covered at the
`validate_strategy_parameters` unit-test layer.
