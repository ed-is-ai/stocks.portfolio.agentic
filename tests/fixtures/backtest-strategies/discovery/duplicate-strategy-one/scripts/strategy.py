"""Discovery fixture: deliberately never importable (see valid-strategy)."""

raise RuntimeError("discover_strategies must never import scripts/strategy.py")
