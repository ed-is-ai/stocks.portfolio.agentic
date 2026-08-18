"""Discovery fixture: deliberately never importable.

Discovery is metadata-only and must never import or execute this file. Any
accidental import raises loudly so a regression is caught immediately by
``tests/backtest/test_skill_discovery.py``.
"""

raise RuntimeError(
    "discover_strategies must never import scripts/strategy.py -- "
    "discovery is metadata-only"
)
