"""Tests for :class:`ContractNormalizer` (GH-310).

Covers the normalizer in isolation: normalizing one raw Interactive
Investor row into canonical keys, and passing through a row where an
optional column (``Sedol``) is absent without error.
"""

from __future__ import annotations

from app.services.portfolio_import.normalizer import ContractNormalizer
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
