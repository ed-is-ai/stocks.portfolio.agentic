"""Versioned import-contract seam for provider SIPP/portfolio CSV imports.

Home for the data-driven contract schema (:mod:`.contract_schema`), the
immutable-snapshot registry that loads and detects contracts
(:mod:`.contract_registry`), and the pure raw-row-to-canonical-field
mapping step (:mod:`.normalizer`). Deliberately isolated from
``app.agents.trader.trader_agent``: this package must never import from
it, since ``trader_agent`` is the caller of this seam, not the other way
around.
"""
