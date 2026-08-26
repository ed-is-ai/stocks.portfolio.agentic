"""Immutable-snapshot registry loading and detecting import contracts.

Mirrors ``app.services.backtest.skill_discovery``'s fail-soft-per-item /
fail-closed-on-structural-problem pattern, but at process-startup
granularity rather than per-scan: :meth:`ContractRegistry.load` reads
every contract JSON file in a directory once into one immutable
snapshot, and a malformed or duplicate contract fails registry
construction outright (naming the offending file) rather than being
silently skipped -- the app must never start serving imports with a
broken registry.

``ContractRegistryError`` is defined locally to this module.
``app.services.portfolio_import`` must never import from
``app.agents.trader.trader_agent`` -- that module already imports
*services*, so importing the reverse direction here would risk a
circular import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from app.services.portfolio_import.contract_schema import (
    CanonicalField,
    PortfolioImportContractV1,
)


class ContractRegistryError(ValueError):
    """Raised when contract loading or detection fails closed."""


def _load_contract_file(path: Path) -> PortfolioImportContractV1:
    """Parse and validate one contract JSON file, or raise naming ``path``."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractRegistryError(
            f"{path}: could not read contract file: {exc}"
        ) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ContractRegistryError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ContractRegistryError(f"{path}: contract file must contain a JSON object")

    try:
        field_mapping = {
            str(column): CanonicalField(canonical)
            for column, canonical in data.get("field_mapping", {}).items()
        }
        contract = PortfolioImportContractV1(
            contract_id=data["contract_id"],
            version=str(data["version"]),
            provider_id=data["provider_id"],
            provider_name=data["provider_name"],
            account_type_ids=tuple(data.get("account_type_ids", ())),
            priority=data.get("priority", 0),
            encoding=data.get("encoding", "utf-8-sig"),
            delimiter=data.get("delimiter", ","),
            required_columns=tuple(data["required_columns"]),
            optional_columns=tuple(data.get("optional_columns", ())),
            header_aliases=dict(data.get("header_aliases", {})),
            field_mapping=field_mapping,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ContractRegistryError(f"{path}: invalid contract: {exc}") from exc

    return contract


class ContractRegistry:
    """One immutable snapshot of every loaded import contract."""

    def __init__(self, contracts: tuple[PortfolioImportContractV1, ...]) -> None:
        self._contracts = contracts

    @classmethod
    def load(cls, contracts_dir: Path) -> "ContractRegistry":
        """Load every ``*.json`` contract file in ``contracts_dir``.

        Raises :class:`ContractRegistryError` naming the offending file
        for a malformed contract or a duplicate ``(contract_id,
        version)`` pair -- never a silent skip. Also raises if
        ``contracts_dir`` is missing or contains zero contracts: a
        registry with nothing loaded is exactly the "broken registry"
        this module's docstring says the app must never start serving
        imports with -- ``Path.glob`` on a missing/empty directory
        raises nothing on its own, so this must be checked explicitly.
        """
        if not contracts_dir.is_dir():
            raise ContractRegistryError(
                f"{contracts_dir}: contracts directory does not exist"
            )
        contracts: list[PortfolioImportContractV1] = []
        seen: dict[tuple[str, str], Path] = {}
        for path in sorted(contracts_dir.glob("*.json")):
            contract = _load_contract_file(path)
            key = (contract.contract_id, contract.version)
            if key in seen:
                raise ContractRegistryError(
                    f"{path}: duplicate contract (contract_id={key[0]!r}, "
                    f"version={key[1]!r}) already loaded from {seen[key]}"
                )
            seen[key] = path
            contracts.append(contract)
        if not contracts:
            raise ContractRegistryError(f"{contracts_dir}: no contract files found")
        return cls(tuple(contracts))

    def detect(self, header_row: Sequence[str]) -> PortfolioImportContractV1:
        """Return the one contract whose required columns are all present.

        A contract matches when every one of its ``required_columns`` is
        present in ``header_row`` after normalizing ``header_row``
        through the contract's own ``header_aliases``. Zero matches
        raises with a "missing required columns" message (naming the
        best partial match's missing columns, matching the importer's
        historical error text); more than one equally-specific match
        (same count of required columns) raises an ambiguous-match error.
        Fails closed in both cases -- no plan is built.
        """
        scored: list[tuple[int, PortfolioImportContractV1]] = []
        best_missing: list[str] | None = None

        for contract in self._contracts:
            aliased_header = {
                contract.header_aliases.get(column, column) for column in header_row
            }
            missing = [
                column
                for column in contract.required_columns
                if column not in aliased_header
            ]
            if not missing:
                scored.append((len(contract.required_columns), contract))
            elif best_missing is None or len(missing) < len(best_missing):
                best_missing = missing

        if not scored:
            if best_missing is not None:
                raise ContractRegistryError(
                    "CSV is missing required columns: " + ", ".join(best_missing)
                )
            raise ContractRegistryError(
                "no import contract matches this CSV header row"
            )

        max_specificity = max(score for score, _ in scored)
        best = [contract for score, contract in scored if score == max_specificity]
        if len(best) > 1:
            candidates = ", ".join(
                f"{contract.contract_id}@{contract.version}" for contract in best
            )
            raise ContractRegistryError(
                f"ambiguous import contract match: {candidates} all match "
                "this header equally"
            )
        return best[0]


__all__ = ["ContractRegistry", "ContractRegistryError"]
