"""Tests for :class:`ContractNormalizer` (GH-310, Story 3.4).

Covers the normalizer in isolation: normalizing one raw Interactive
Investor row into canonical keys, passing through a row where an
optional column (``Sedol``) is absent without error, and the three
Story 3.4 capabilities (text extraction, signed-amount splitting,
reject-unless-column-equals) exercised via small inline contract
fixtures -- never the full IG contract, so these stay true unit tests.
"""

from __future__ import annotations

from typing import Any

from app.services.portfolio_import.contract_schema import (
    CanonicalField,
    PortfolioImportContractV1,
    RejectedRowValueRule,
    SignedAmountRule,
    TextExtractionRule,
)
from app.services.portfolio_import.normalizer import (
    REJECTED_REASON_KEY,
    ContractNormalizer,
)
from app.services.portfolio_import.registry_loader import get_contract_registry

_II_HEADER = [
    "Date",
    "Symbol",
    "Sedol",
    "Quantity",
    "Price",
    "Description",
    "Reference",
    "Debit",
    "Credit",
    "Running Balance",
]


def _ii_contract():
    return get_contract_registry().detect(_II_HEADER)


def test_normalize_row_maps_raw_ii_row_to_canonical_keys() -> None:
    contract = _ii_contract()
    raw_row = {
        "Date": "01/02/2024",
        "Symbol": "AAPL",
        "Sedol": "B123",
        "Quantity": "10",
        "Price": "100.00",
        "Description": "Buy AAPL",
        "Reference": "REF-AAPL-1",
        "Debit": "1000.00",
        "Credit": "",
        "Running Balance": "5000.00",
    }

    normalized = ContractNormalizer.normalize_row(contract, raw_row)

    assert normalized == {
        "date": "01/02/2024",
        "security_symbol": "AAPL",
        "security_identifier": "B123",
        "quantity": "10",
        "price": "100.00",
        "description": "Buy AAPL",
        "reference": "REF-AAPL-1",
        "debit": "1000.00",
        "credit": "",
        "running_balance": "5000.00",
    }


def test_normalize_row_passes_through_absent_optional_column() -> None:
    """A row from a header with no ``Sedol`` column omits that key.

    No error is raised -- the caller already defaults a missing
    canonical key via ``.get(key, "")`` exactly as it did for the raw
    ``Sedol`` column before this seam existed.
    """
    header = [c for c in _II_HEADER if c != "Sedol"]
    contract = get_contract_registry().detect(header)
    raw_row = {
        "Date": "02/02/2024",
        "Symbol": "MSFT",
        "Quantity": "5",
        "Price": "50.00",
        "Description": "Buy MSFT",
        "Reference": "REF-MSFT-1",
        "Debit": "250.00",
        "Credit": "",
        "Running Balance": "4750.00",
    }

    normalized = ContractNormalizer.normalize_row(contract, raw_row)

    assert "security_identifier" not in normalized
    assert normalized["security_symbol"] == "MSFT"


# --- Story 3.4 capabilities, via small inline contract fixtures ------------


def _make_contract(**overrides: Any) -> PortfolioImportContractV1:
    data: dict[str, Any] = {
        "contract_id": "inline_test",
        "version": "1",
        "provider_id": "inline_test",
        "provider_name": "Inline Test",
        "encoding": "utf-8-sig",
        "delimiter": ",",
        "required_columns": ("Date",),
        "optional_columns": ("Text", "Amount", "Marker"),
        "field_mapping": {"Date": CanonicalField.DATE},
    }
    data.update(overrides)
    return PortfolioImportContractV1(**data)


def test_text_extraction_populates_canonical_fields_on_match() -> None:
    contract = _make_contract(
        text_extraction=TextExtractionRule(
            source_column="Text",
            pattern=r"^(?P<security_symbol>.+?) CONS (?P<quantity>[0-9.]+)"
            r"@(?P<price>[0-9.]+)",
        )
    )
    row = {
        "Date": "01/01/2024",
        "Text": "Widget Co CONS 10@250",
        "Amount": "",
        "Marker": "",
    }

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert normalized["security_symbol"] == "Widget Co"
    assert normalized["quantity"] == "10"
    assert normalized["price"] == "250"
    assert REJECTED_REASON_KEY not in normalized


def test_text_extraction_no_match_without_marker_falls_through() -> None:
    """No ``required_marker`` set: a non-match is a benign cash-flow
    fallthrough, exactly like today -- no fields populated, no rejection."""
    contract = _make_contract(
        text_extraction=TextExtractionRule(
            source_column="Text",
            pattern=r"^(?P<security_symbol>.+?) CONS (?P<quantity>[0-9.]+)"
            r"@(?P<price>[0-9.]+)",
        )
    )
    row = {"Date": "01/01/2024", "Text": "Dividend payment", "Amount": "", "Marker": ""}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert "security_symbol" not in normalized
    assert REJECTED_REASON_KEY not in normalized


def test_text_extraction_required_marker_present_but_pattern_fails_rejects() -> None:
    """The corrected (review pass 2) behavior: marker present, full
    pattern still fails to match -> rejected, never a silent cash-flow
    fallthrough."""
    contract = _make_contract(
        text_extraction=TextExtractionRule(
            source_column="Text",
            pattern=r"^(?P<security_symbol>.+?) CONS (?P<quantity>[0-9.]+)"
            r"@(?P<price>[0-9.]+)",
            required_marker="CONS",
        )
    )
    row = {
        "Date": "01/01/2024",
        "Text": "Widget Co CONS ten shares",
        "Amount": "",
        "Marker": "",
    }

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert REJECTED_REASON_KEY in normalized
    assert "Widget Co CONS ten shares" in normalized[REJECTED_REASON_KEY]


def test_text_extraction_required_marker_absent_falls_through() -> None:
    """Marker absent from the raw value at all -> no extraction attempted,
    falls through as a cash flow (the "no ticker column value -> cash
    flow" rule, generalized)."""
    contract = _make_contract(
        text_extraction=TextExtractionRule(
            source_column="Text",
            pattern=r"^(?P<security_symbol>.+?) CONS (?P<quantity>[0-9.]+)"
            r"@(?P<price>[0-9.]+)",
            required_marker="CONS",
        )
    )
    row = {"Date": "01/01/2024", "Text": "Dividend payment", "Amount": "", "Marker": ""}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert "security_symbol" not in normalized
    assert REJECTED_REASON_KEY not in normalized


def test_text_extraction_required_marker_is_word_bounded_not_substring() -> None:
    """A legitimate description merely *containing* the marker's letters as
    part of another word (a security literally named "Consol Energy", or a
    "Client Consideration" summary) must fall through as a cash flow, not
    be wrongly rejected as if it were a malformed trade row. This is the
    exact false-positive a naive substring check (rather than a
    word-boundary match) would introduce -- found in review pass 2."""
    contract = _make_contract(
        text_extraction=TextExtractionRule(
            source_column="Text",
            pattern=r"^(?P<security_symbol>.+?) CONS (?P<quantity>[0-9.]+)"
            r"@(?P<price>[0-9.]+)",
            required_marker="CONS",
        )
    )
    row = {
        "Date": "01/01/2024",
        "Text": "Consol Energy Inc Client Consideration",
        "Amount": "",
        "Marker": "",
    }

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert "security_symbol" not in normalized
    assert REJECTED_REASON_KEY not in normalized


def test_signed_amount_column_splits_negative_into_debit() -> None:
    contract = _make_contract(
        signed_amount_column=SignedAmountRule(source_column="Amount")
    )
    row = {"Date": "01/01/2024", "Text": "", "Amount": "-250.00", "Marker": ""}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert normalized["debit"] == "250.0"
    assert "credit" not in normalized


def test_signed_amount_column_splits_positive_into_credit() -> None:
    contract = _make_contract(
        signed_amount_column=SignedAmountRule(source_column="Amount")
    )
    row = {"Date": "01/01/2024", "Text": "", "Amount": "250.00", "Marker": ""}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert normalized["credit"] == "250.0"
    assert "debit" not in normalized


def test_signed_amount_column_zero_populates_neither() -> None:
    contract = _make_contract(
        signed_amount_column=SignedAmountRule(source_column="Amount")
    )
    row = {"Date": "01/01/2024", "Text": "", "Amount": "0.00", "Marker": ""}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert "debit" not in normalized
    assert "credit" not in normalized
    assert REJECTED_REASON_KEY not in normalized


def test_signed_amount_column_unparseable_rejects() -> None:
    contract = _make_contract(
        signed_amount_column=SignedAmountRule(source_column="Amount")
    )
    row = {"Date": "01/01/2024", "Text": "", "Amount": "not-a-number", "Marker": ""}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert REJECTED_REASON_KEY in normalized
    assert "not-a-number" in normalized[REJECTED_REASON_KEY]


def test_reject_unless_column_equals_passes_on_allowed_value() -> None:
    contract = _make_contract(
        reject_unless_column_equals=RejectedRowValueRule(
            source_column="Marker", allowed_value="-"
        )
    )
    row = {"Date": "01/01/2024", "Text": "", "Amount": "", "Marker": "-"}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert REJECTED_REASON_KEY not in normalized


def test_reject_unless_column_equals_rejects_on_mismatch() -> None:
    contract = _make_contract(
        reject_unless_column_equals=RejectedRowValueRule(
            source_column="Marker", allowed_value="-"
        )
    )
    row = {"Date": "01/01/2024", "Text": "", "Amount": "", "Marker": "DFB"}

    normalized = ContractNormalizer.normalize_row(contract, row)

    assert REJECTED_REASON_KEY in normalized
    assert "DFB" in normalized[REJECTED_REASON_KEY]
