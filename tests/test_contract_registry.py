"""Tests for the versioned import-contract registry (GH-310).

Covers the I/O matrix's registry-level scenarios: loading the shipped
Interactive Investor contract, rejecting a malformed contract file
(naming it), detecting II from a real header, raising on a zero-match
header, and raising on a synthetic two-contract ambiguous-match fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.portfolio_import.contract_registry import (
    ContractRegistry,
    ContractRegistryError,
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


def _write_contract(path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "contract_id": "test_provider",
        "version": "1",
        "provider_id": "test_provider",
        "provider_name": "Test Provider",
        "encoding": "utf-8-sig",
        "delimiter": ",",
        "required_columns": ["Date", "Amount"],
        "field_mapping": {"Date": "date", "Amount": "debit"},
    }
    data.update(overrides)
    file_path = path / f"{data['contract_id']}_{data['version']}.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")
    return file_path


def test_shipped_interactive_investor_contract_loads() -> None:
    """The real ``contracts/`` directory loads without error."""
    registry = get_contract_registry()
    contract = registry.detect(_II_HEADER)
    assert contract.contract_id == "interactive_investor"
    assert contract.version == "1"


def test_load_rejects_malformed_contract_file_naming_it(tmp_path: Path) -> None:
    bad_file = tmp_path / "broken.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ContractRegistryError) as exc:
        ContractRegistry.load(tmp_path)

    assert str(bad_file) in str(exc.value)


def test_load_rejects_contract_with_unknown_canonical_field(tmp_path: Path) -> None:
    bad_file = _write_contract(
        tmp_path, field_mapping={"Date": "date", "Amount": "not_a_real_field"}
    )

    with pytest.raises(ContractRegistryError) as exc:
        ContractRegistry.load(tmp_path)

    assert str(bad_file) in str(exc.value)


def test_load_rejects_duplicate_contract_id_and_version(tmp_path: Path) -> None:
    _write_contract(tmp_path, contract_id="dup", version="1")
    second_path = tmp_path / "dup_1_again.json"
    second_path.write_text(
        json.dumps(
            {
                "contract_id": "dup",
                "version": "1",
                "provider_id": "dup",
                "provider_name": "Dup",
                "encoding": "utf-8-sig",
                "delimiter": ",",
                "required_columns": ["Date"],
                "field_mapping": {"Date": "date"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractRegistryError) as exc:
        ContractRegistry.load(tmp_path)

    assert "duplicate contract" in str(exc.value)


def test_detect_finds_interactive_investor_from_real_header() -> None:
    registry = get_contract_registry()
    contract = registry.detect(_II_HEADER)
    assert contract.contract_id == "interactive_investor"


def test_detect_raises_on_zero_match_header() -> None:
    registry = get_contract_registry()
    with pytest.raises(ContractRegistryError) as exc:
        registry.detect(["Date", "Symbol"])
    assert "missing required columns" in str(exc.value)
    assert "Quantity" in str(exc.value)


def test_detect_raises_no_contract_matches_on_empty_registry() -> None:
    """An empty registry (bypassing ``load``'s own empty-registry guard,
    via direct construction) hits ``detect``'s other zero-match message --
    unreachable through ``load`` today with only one shipped contract,
    but this is the only way to exercise it directly (review finding)."""
    registry = ContractRegistry(())
    with pytest.raises(ContractRegistryError) as exc:
        registry.detect(["Date", "Symbol"])
    assert "no import contract matches" in str(exc.value)


def test_load_raises_on_missing_contracts_directory(tmp_path: Path) -> None:
    with pytest.raises(ContractRegistryError) as exc:
        ContractRegistry.load(tmp_path / "does-not-exist")
    assert "does not exist" in str(exc.value)


def test_load_raises_on_empty_contracts_directory(tmp_path: Path) -> None:
    with pytest.raises(ContractRegistryError) as exc:
        ContractRegistry.load(tmp_path)
    assert "no contract files found" in str(exc.value)


def test_load_rejects_contract_missing_a_required_column_mapping(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        required_columns=["Date", "Amount", "Unmapped"],
        field_mapping={"Date": "date", "Amount": "debit"},
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_rejects_field_mapping_collision(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        required_columns=["Date", "Amount", "Cost"],
        field_mapping={"Date": "date", "Amount": "debit", "Cost": "debit"},
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_contract_mappings_are_immutable() -> None:
    """A contract's ``field_mapping``/``header_aliases`` must not be
    mutable in place -- ``frozen=True`` alone only blocks reassigning the
    field, not mutating a dict object it points to, which would silently
    corrupt the ``@lru_cache``-singleton registry for the rest of the
    process (review finding, verified live before this fix)."""
    registry = get_contract_registry()
    contract = registry.detect(_II_HEADER)
    with pytest.raises(TypeError):
        contract.field_mapping["Date"] = "debit"  # type: ignore[index]


def test_detect_raises_on_ambiguous_equally_specific_match(tmp_path: Path) -> None:
    """Synthetic two-contract fixture: both match a shared header equally.

    Not shipped in the real contracts directory -- only exercised via a
    direct ``ContractRegistry`` unit test (spec Design Notes).
    """
    _write_contract(tmp_path, contract_id="alpha", version="1")
    _write_contract(tmp_path, contract_id="beta", version="1")

    registry = ContractRegistry.load(tmp_path)

    with pytest.raises(ContractRegistryError) as exc:
        registry.detect(["Date", "Amount"])
    assert "ambiguous" in str(exc.value)


def test_detect_prefers_more_specific_contract_over_less_specific(
    tmp_path: Path,
) -> None:
    """A contract requiring more of the present columns wins, not a tie."""
    _write_contract(
        tmp_path,
        contract_id="broad",
        version="1",
        required_columns=["Date"],
        field_mapping={"Date": "date"},
    )
    _write_contract(
        tmp_path,
        contract_id="narrow",
        version="1",
        required_columns=["Date", "Amount"],
        field_mapping={"Date": "date", "Amount": "debit"},
    )

    registry = ContractRegistry.load(tmp_path)

    contract = registry.detect(["Date", "Amount"])
    assert contract.contract_id == "narrow"
