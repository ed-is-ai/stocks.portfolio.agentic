"""Process-wide singleton loader for the shipped :class:`ContractRegistry`.

Mirrors ``app.api.dependencies.get_portfolio_service``'s ``@lru_cache``
singleton pattern: one immutable registry snapshot is built the first
time it's needed and reused for the rest of the process.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.services.portfolio_import.contract_registry import ContractRegistry

#: The package-local directory shipping every contract data file.
CONTRACTS_DIR = Path(__file__).parent / "contracts"


@lru_cache
def get_contract_registry() -> ContractRegistry:
    """Return the process-wide, lazily-built import contract registry."""
    return ContractRegistry.load(CONTRACTS_DIR)


__all__ = ["CONTRACTS_DIR", "get_contract_registry"]
