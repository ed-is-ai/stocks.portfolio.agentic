"""Strict, versioned schema for a provider's SIPP/portfolio import contract.

A :class:`PortfolioImportContractV1` encodes one broker's CSV dialect --
its column names, optional aliases, and the mapping from each raw source
column to a provider-neutral :class:`CanonicalField` -- as data, so a new
provider can be added by shipping a new contract file rather than editing
importer business logic (GH-310).

``CanonicalField`` is a closed vocabulary, not a free string, so a
contract mapping a column to an unknown canonical field fails to
validate at load time rather than silently no-op-ing at import time.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CanonicalField(StrEnum):
    """Closed, provider-neutral vocabulary a contract's columns map onto."""

    DATE = "date"
    SECURITY_SYMBOL = "security_symbol"
    SECURITY_IDENTIFIER = "security_identifier"
    QUANTITY = "quantity"
    PRICE = "price"
    DESCRIPTION = "description"
    REFERENCE = "reference"
    DEBIT = "debit"
    CREDIT = "credit"
    RUNNING_BALANCE = "running_balance"
    CURRENCY = "currency"


class _ContractModel(BaseModel):
    """Frozen, strict, extra-forbidding base for every contract model.

    Mirrors the ``extra="forbid", frozen=True, strict=True,
    allow_inf_nan=False`` convention used by
    ``app.services.backtest.skill_discovery._DiscoveryModel`` and
    ``app.services.backtest.strategy_protocol``'s strict-model precedent
    -- no field may hold executable code, an arbitrary regex, or a
    callable.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class PortfolioImportContractV1(_ContractModel):
    """One provider/version's complete, versioned import contract.

    ``required_columns`` and ``optional_columns`` name raw source CSV
    column headers (post-``header_aliases`` normalization).
    ``header_aliases`` maps an alternate raw header spelling onto the
    canonical raw column name used elsewhere in this contract.
    ``field_mapping`` is keyed by that same canonical raw column name and
    maps it to the :class:`CanonicalField` it fills -- direction matters:
    :class:`~app.services.portfolio_import.normalizer.ContractNormalizer`
    iterates ``field_mapping`` to know which canonical key each raw
    column's value belongs under, never the reverse.
    """

    contract_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    account_type_ids: tuple[str, ...] = ()
    priority: int = 0
    encoding: str = Field(min_length=1)
    delimiter: str = Field(min_length=1, max_length=1)
    required_columns: tuple[str, ...] = Field(min_length=1)
    optional_columns: tuple[str, ...] = ()
    header_aliases: Mapping[str, str] = Field(default_factory=dict)
    field_mapping: Mapping[str, CanonicalField]

    @field_validator("header_aliases", "field_mapping", mode="after")
    @classmethod
    def _freeze_mapping(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        """Wrap in ``MappingProxyType`` -- ``frozen=True`` only blocks
        reassigning a field, it does not stop mutating a mutable object
        (e.g. a plain ``dict``) a field already points to. Without this,
        a caller mutating one contract's mapping would silently corrupt
        the shared, ``@lru_cache``-singleton registry for the rest of the
        process (verified live during review)."""
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def _validate_field_mapping_shape(self) -> "PortfolioImportContractV1":
        """Every required column must have a mapping, and no two columns
        may silently collide on the same canonical field -- both would
        otherwise fail only at import time (or not at all, just losing
        data), not at contract-load time."""
        unmapped_required = [
            column
            for column in self.required_columns
            if column not in self.field_mapping
        ]
        if unmapped_required:
            raise ValueError(
                "required_columns missing from field_mapping: "
                + ", ".join(unmapped_required)
            )
        seen: dict[CanonicalField, str] = {}
        for column, canonical in self.field_mapping.items():
            if canonical in seen:
                raise ValueError(
                    f"field_mapping columns {seen[canonical]!r} and {column!r} "
                    f"both map to canonical field {canonical.value!r}"
                )
            seen[canonical] = column
        return self


__all__ = ["CanonicalField", "PortfolioImportContractV1"]
