"""Pure raw-CSV-row-to-canonical-field mapping step.

``ContractNormalizer`` performs no money parsing, classification, or
validation -- those stay exactly where they are today in
``TraderAgent.import_sipp``, just reading canonical keys instead of raw
provider column names.
"""

from __future__ import annotations

from app.services.portfolio_import.contract_schema import PortfolioImportContractV1


class ContractNormalizer:
    """Maps one raw CSV row into canonical field keys via a contract."""

    @staticmethod
    def normalize_row(
        contract: PortfolioImportContractV1, raw_row: dict[str, str]
    ) -> dict[str, str]:
        """Return ``raw_row``'s canonical-field view under ``contract``.

        ``raw_row``'s keys are first normalized through the contract's
        ``header_aliases`` (an alternate header spelling maps onto the
        canonical raw column name), then each column named in
        ``field_mapping`` copies its raw string value under its
        canonical field key. A column declared in ``field_mapping`` but
        absent from this particular row (e.g. an optional column not
        present in this CSV's header) is simply omitted -- callers
        already default a missing canonical key via ``.get(key, "")``
        exactly as they did for the raw column before this seam existed.
        """
        dealiased = {
            contract.header_aliases.get(column, column): value
            for column, value in raw_row.items()
        }
        normalized: dict[str, str] = {}
        for source_column, canonical_field in contract.field_mapping.items():
            if source_column in dealiased:
                normalized[str(canonical_field)] = dealiased[source_column]
        return normalized


__all__ = ["ContractNormalizer"]
