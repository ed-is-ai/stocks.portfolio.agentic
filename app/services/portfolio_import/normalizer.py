"""Pure raw-CSV-row-to-canonical-field mapping step.

``ContractNormalizer`` performs no money parsing, classification, or
validation -- those stay exactly where they are today in
``TraderAgent.import_sipp``, just reading canonical keys instead of raw
provider column names.
"""

from __future__ import annotations

import re

from app.services.portfolio_import.contract_schema import (
    CanonicalField,
    PortfolioImportContractV1,
)

#: Sentinel key ``normalize_row`` uses to flag a row for whole-plan
#: rejection -- the same mechanism ``TraderAgent.import_sipp`` already
#: reads its per-row ``failed_rows`` entries from (see its early check of
#: this key, right after computing each row's display label), never a
#: second, parallel rejection channel.
REJECTED_REASON_KEY = "_rejected_reason"


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

        Story 3.4 applies three further, generic capabilities -- keyed
        only on their *presence* on ``contract``, never on
        ``contract.provider_id`` -- after the ``field_mapping`` pass
        above:

        - ``reject_unless_column_equals``: if the named source column's
          raw value isn't the one allowed value, the row is marked for
          whole-plan rejection via :data:`REJECTED_REASON_KEY` and
          returned immediately (e.g. IG's CFD/spread-bet rows, which this
          app's canonical portfolio domain does not model).
        - ``text_extraction``: extracts canonical fields from one
          free-text column via a regex with named groups. When
          ``required_marker`` is set: absent from the raw value -> no
          extraction is attempted, the row falls through as a cash flow
          exactly as before; present but the full pattern still fails to
          match -> the row is rejected (not silently reclassified as a
          cash flow), since the marker means the row claims to be
          exactly the kind of row the pattern describes. Unset (the
          default) -> a non-match always falls through as today.
        - ``signed_amount_column``: splits one signed-amount column into
          canonical ``debit``/``credit`` by sign, as a plain numeric
          string (matching every other canonical field's string typing,
          so downstream ``Money`` parsing needs no changes). A value that
          fails to parse as a number is rejected, never silently dropped
          or zeroed.
        """
        dealiased = {
            contract.header_aliases.get(column, column): value
            for column, value in raw_row.items()
        }
        normalized: dict[str, str] = {}
        for source_column, canonical_field in contract.field_mapping.items():
            if source_column in dealiased:
                normalized[str(canonical_field)] = dealiased[source_column]

        if contract.reject_unless_column_equals is not None:
            rule = contract.reject_unless_column_equals
            raw_value = dealiased.get(rule.source_column, "")
            if raw_value != rule.allowed_value:
                normalized[REJECTED_REASON_KEY] = (
                    f"{rule.source_column} was {raw_value!r}, expected "
                    f"{rule.allowed_value!r}"
                )
                return normalized

        if contract.text_extraction is not None:
            rule = contract.text_extraction
            raw_value = dealiased.get(rule.source_column, "")
            # Word-boundary, case-insensitive match -- a naive substring
            # test would treat a legitimate security/description merely
            # containing the marker as a run of letters (e.g. a company
            # named "Consol Energy Inc", or a "Client Consideration"
            # summary) as if it were the marker token itself, wrongly
            # rejecting a perfectly good cash-flow row instead of letting
            # it fall through. ``\b`` on both sides means "CONS" matches
            # the standalone token in "XPeng Inc CONS 60@2945" but not the
            # "CONS" prefix inside "Consol" or "Consideration".
            marker_present = rule.required_marker is not None and re.search(
                r"\b" + re.escape(rule.required_marker) + r"\b",
                raw_value,
                re.IGNORECASE,
            )
            marker_absent = rule.required_marker is not None and not marker_present
            if not marker_absent:
                match = re.match(rule.pattern, raw_value)
                if match:
                    normalized.update(match.groupdict())
                elif rule.required_marker is not None:
                    normalized[REJECTED_REASON_KEY] = (
                        f"{rule.source_column} value {raw_value!r} contains "
                        f"{rule.required_marker!r} but did not match the "
                        "expected extraction pattern"
                    )
                    return normalized

        if contract.signed_amount_column is not None:
            rule = contract.signed_amount_column
            raw_value = dealiased.get(rule.source_column, "").strip()
            if raw_value and raw_value.lower() != "n/a":
                try:
                    numeric = float(raw_value.replace(",", ""))
                except ValueError:
                    normalized[REJECTED_REASON_KEY] = (
                        f"{rule.source_column} value {raw_value!r} is not a "
                        "valid signed amount"
                    )
                    return normalized
                if numeric < 0:
                    normalized[CanonicalField.DEBIT.value] = str(abs(numeric))
                elif numeric > 0:
                    normalized[CanonicalField.CREDIT.value] = str(numeric)

        return normalized


__all__ = ["ContractNormalizer", "REJECTED_REASON_KEY"]
