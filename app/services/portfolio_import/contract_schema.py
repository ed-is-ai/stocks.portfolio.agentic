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

import hashlib
import json
import re
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Mapping

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


class TextExtractionRule(_ContractModel):
    """Regex-based extraction of canonical fields from one free-text column
    (e.g. IG's ``MarketName``, which packs security/quantity/price into one
    string rather than separate columns).

    ``pattern``'s named groups must all be valid :class:`CanonicalField`
    values -- validated (and the regex itself compiled) at contract-load
    time, so a typo in the JSON fails contract loading loudly rather than
    silently never matching at import time.

    ``required_marker``, when set, is a substring gate on the raw source
    value: absent -> the row falls through as a cash flow exactly as
    today (unmatched text is not necessarily an error, e.g. a dividend
    description); present but ``pattern`` still fails to match -> the row
    is rejected, not silently reclassified as a cash flow, because the
    marker means this row unambiguously claims to be the kind of row
    ``pattern`` describes (e.g. a trade). Left unset (the default), a
    non-match always falls through as it did before this field existed --
    so an unrelated contract that never sets it is unaffected.
    """

    source_column: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    required_marker: str | None = None

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        """Compile ``pattern`` and check its named groups are all valid
        ``CanonicalField`` values -- both checked once here, at contract-
        load time, rather than failing (or silently never matching) the
        first time a real CSV row is normalized."""
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern {value!r}: {exc}") from exc
        for name in compiled.groupindex:
            try:
                CanonicalField(name)
            except ValueError as exc:
                raise ValueError(
                    f"pattern named group {name!r} is not a valid CanonicalField"
                ) from exc
        return value


class SignedAmountRule(_ContractModel):
    """Splits one signed-amount column (e.g. IG's ``PL Amount``) into
    canonical ``debit``/``credit`` by sign -- for a provider that has no
    separate Debit/Credit columns at all.

    ``credit_when``/``debit_when`` are schema-enforced constants (not a
    free string) by explicit design decision -- confirmed with the user
    during spec design -- since every known provider shape uses this same
    sign convention and a free string would only add configuration
    surface with no real use case yet.
    """

    source_column: str = Field(min_length=1)
    credit_when: Literal["positive"] = "positive"
    debit_when: Literal["negative"] = "negative"


class RejectedRowValueRule(_ContractModel):
    """A generic "this row's activity-type marker column must equal
    ``allowed_value`` or the row is unsupported" gate (e.g. IG's
    ``Period`` column, which is ``"-"`` for share-dealing rows and
    something else for CFD/spread-bet activity this app's canonical
    portfolio domain does not model)."""

    source_column: str = Field(min_length=1)
    allowed_value: str


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
    # Story 3.4: three small, provider-agnostic capabilities beyond a plain
    # 1:1 column mapping -- all default ``None`` so an existing contract
    # (Interactive Investor) that never sets them is byte-for-byte
    # unaffected.
    text_extraction: TextExtractionRule | None = None
    signed_amount_column: SignedAmountRule | None = None
    reject_unless_column_equals: RejectedRowValueRule | None = None

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
        data), not at contract-load time.

        Story 3.4: a required column consumed by one of the three
        capabilities (e.g. IG's ``PL Amount``, read by
        ``signed_amount_column``, or ``Period``, read by
        ``reject_unless_column_equals``) is exempt from needing its own
        ``field_mapping`` entry too -- a capability is itself a
        (generic, data-driven) way of consuming a required column, not
        only ``field_mapping``.
        """
        capability_columns = {
            rule.source_column
            for rule in (
                self.text_extraction,
                self.signed_amount_column,
                self.reject_unless_column_equals,
            )
            if rule is not None
        }
        unmapped_required = [
            column
            for column in self.required_columns
            if column not in self.field_mapping and column not in capability_columns
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

    @model_validator(mode="after")
    def _validate_capability_columns_and_collisions(
        self,
    ) -> "PortfolioImportContractV1":
        """Fail-closed validation for the three Story 3.4 capabilities,
        mirroring ``contract_registry.py``'s existing fail-closed
        convention (e.g. its duplicate-``(contract_id, version)`` check):

        - every capability's ``source_column`` must actually be declared
          in ``required_columns``/``optional_columns`` -- a typo in the
          JSON must never silently degrade into "this capability never
          fires" (it would otherwise just never find the column in a raw
          row and no-op forever, with no error anywhere).
        - no capability's output canonical field (a ``text_extraction``
          named group, or ``signed_amount_column``'s implicit
          ``debit``/``credit`` targets) may collide with a canonical field
          already produced by ``field_mapping`` **or by another
          capability** -- else one capability could silently overwrite
          another's value with no error (``normalize_row`` applies
          capabilities in a fixed order, so a collision is a silent,
          order-dependent clobber, not a visible failure).
        """
        known_columns = set(self.required_columns) | set(self.optional_columns)
        mapped_canonicals = set(self.field_mapping.values())
        # Accumulates every capability's output fields as they're
        # validated, so each subsequent capability is checked against
        # field_mapping AND every capability already claimed above --
        # order here must match normalize_row's own application order.
        claimed_canonicals: set[CanonicalField] = set(mapped_canonicals)

        def _require_known_column(source_column: str, capability: str) -> None:
            if source_column not in known_columns:
                raise ValueError(
                    f"{capability}.source_column {source_column!r} is not "
                    "declared in required_columns or optional_columns"
                )

        def _claim(canonical: CanonicalField, capability: str) -> None:
            if canonical in claimed_canonicals:
                raise ValueError(
                    f"{capability}'s output canonical field {canonical.value!r} "
                    "collides with a canonical field already produced by "
                    "field_mapping or another capability"
                )
            claimed_canonicals.add(canonical)

        if self.text_extraction is not None:
            _require_known_column(self.text_extraction.source_column, "text_extraction")
            compiled = re.compile(self.text_extraction.pattern)
            for name in compiled.groupindex:
                _claim(CanonicalField(name), "text_extraction")

        if self.signed_amount_column is not None:
            _require_known_column(
                self.signed_amount_column.source_column, "signed_amount_column"
            )
            _claim(CanonicalField.DEBIT, "signed_amount_column")
            _claim(CanonicalField.CREDIT, "signed_amount_column")

        if self.reject_unless_column_equals is not None:
            _require_known_column(
                self.reject_unless_column_equals.source_column,
                "reject_unless_column_equals",
            )

        return self


def contract_content_digest(contract: PortfolioImportContractV1) -> str:
    """Stable content digest over every field that affects how this
    contract parses a CSV.

    A small, self-contained SHA-256 -- mirroring
    ``gbp_valuation_service._quote_digest``'s style (GH-311) -- over
    ``contract_id``, ``version``, ``encoding``, ``delimiter``, sorted
    ``required_columns``, sorted ``optional_columns``, sorted
    ``header_aliases`` items, and sorted ``field_mapping`` items.
    Deliberately not the Backtest epic's shared canonicalizer
    (``canonical_manifest.py``), which would be exactly the reverse
    cross-epic coupling AD-10 forbids.

    Covers every parsing-relevant field deliberately -- a contract edit to
    ``header_aliases``/``optional_columns``/``delimiter``/``encoding``
    changes how a CSV is actually read, so it must also change this
    digest even without a version bump (review finding: an earlier
    version only covered ``required_columns``/``field_mapping``, silently
    missing edits to the other four fields).

    Serializes via ``json.dumps`` of a fully-sorted, fully-typed
    structure (not a naive ``"|"``-joined string) so no field value can
    collide with a differently-shaped contract by containing a separator
    character (review finding).

    Story 3.4: also covers ``text_extraction``, ``signed_amount_column``,
    and ``reject_unless_column_equals`` -- editing any of these three
    changes how a CSV is actually parsed exactly as much as editing
    ``field_mapping`` does, so a contract edit limited to one of them must
    still change this digest even without a version bump.
    """
    canonical_form = {
        "contract_id": contract.contract_id,
        "version": contract.version,
        "encoding": contract.encoding,
        "delimiter": contract.delimiter,
        "required_columns": sorted(contract.required_columns),
        "optional_columns": sorted(contract.optional_columns),
        "header_aliases": sorted(contract.header_aliases.items()),
        "field_mapping": sorted(
            (column, canonical.value)
            for column, canonical in contract.field_mapping.items()
        ),
        "text_extraction": (
            {
                "source_column": contract.text_extraction.source_column,
                "pattern": contract.text_extraction.pattern,
                "required_marker": contract.text_extraction.required_marker,
            }
            if contract.text_extraction is not None
            else None
        ),
        "signed_amount_column": (
            {
                "source_column": contract.signed_amount_column.source_column,
                "credit_when": contract.signed_amount_column.credit_when,
                "debit_when": contract.signed_amount_column.debit_when,
            }
            if contract.signed_amount_column is not None
            else None
        ),
        "reject_unless_column_equals": (
            {
                "source_column": (contract.reject_unless_column_equals.source_column),
                "allowed_value": (contract.reject_unless_column_equals.allowed_value),
            }
            if contract.reject_unless_column_equals is not None
            else None
        ),
    }
    raw = json.dumps(canonical_form, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "CanonicalField",
    "PortfolioImportContractV1",
    "RejectedRowValueRule",
    "SignedAmountRule",
    "TextExtractionRule",
    "contract_content_digest",
]
