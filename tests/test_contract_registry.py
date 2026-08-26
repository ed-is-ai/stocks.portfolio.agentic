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

_IG_HEADER = ["TextDate", "MarketName", "Reference", "PL Amount", "Period"]


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


@pytest.mark.parametrize(
    ("header", "expected_contract_id"),
    [
        pytest.param(_II_HEADER, "interactive_investor", id="interactive_investor"),
        pytest.param(_IG_HEADER, "ig", id="ig"),
    ],
)
def test_shipped_contract_detects_from_its_real_header(
    header: list[str], expected_contract_id: str
) -> None:
    """Story 3.4: II and IG both auto-detect from their own real header --
    proves the registry treats them identically, with no provider-specific
    detection logic (AC4)."""
    registry = get_contract_registry()
    contract = registry.detect(header)
    assert contract.contract_id == expected_contract_id
    assert contract.version == "1"


def test_shipped_ig_contract_declares_its_three_capabilities() -> None:
    """Sanity check that the shipped IG contract actually uses all three
    Story 3.4 capabilities -- the whole point of adding IG."""
    registry = get_contract_registry()
    contract = registry.detect(_IG_HEADER)
    assert contract.text_extraction is not None
    assert contract.signed_amount_column is not None
    assert contract.reject_unless_column_equals is not None


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
    """Story 3.4: with IG also loaded, the "best partial match" reported
    is whichever contract has the fewest missing columns -- so this
    header (all of II's required columns but one) must remain closer to
    II than to IG's five entirely-different required columns, to keep
    testing II's own missing-column error text specifically (rather than
    accidentally asserting on IG's)."""
    registry = get_contract_registry()
    with pytest.raises(ContractRegistryError) as exc:
        registry.detect(
            [
                "Date",
                "Symbol",
                "Sedol",
                "Quantity",
                "Price",
                "Description",
                "Reference",
                "Debit",
                "Credit",
            ]
        )
    assert "missing required columns" in str(exc.value)
    assert "Running Balance" in str(exc.value)


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


# --- Story 3.4: fail-closed capability validation --------------------------


def test_load_rejects_text_extraction_source_column_not_declared(
    tmp_path: Path,
) -> None:
    """A ``text_extraction.source_column`` typo'd/missing from
    ``required_columns``/``optional_columns`` must fail contract loading
    outright, not silently never fire at import time."""
    _write_contract(
        tmp_path,
        required_columns=["Date", "Amount"],
        field_mapping={"Date": "date", "Amount": "debit"},
        text_extraction={
            "source_column": "NotAColumn",
            "pattern": r"^(?P<security_symbol>.+)$",
        },
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_rejects_signed_amount_column_source_not_declared(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        required_columns=["Date"],
        field_mapping={"Date": "date"},
        signed_amount_column={"source_column": "NotAColumn"},
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_rejects_reject_unless_column_equals_source_not_declared(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        required_columns=["Date"],
        field_mapping={"Date": "date"},
        reject_unless_column_equals={
            "source_column": "NotAColumn",
            "allowed_value": "-",
        },
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_rejects_text_extraction_group_colliding_with_field_mapping(
    tmp_path: Path,
) -> None:
    """A ``text_extraction`` named group that collides with a canonical
    field already produced by ``field_mapping`` must fail contract
    loading -- one capability silently overwriting another's value would
    otherwise fail only at import time (or not at all, just losing
    data)."""
    _write_contract(
        tmp_path,
        required_columns=["Date", "Symbol", "Text"],
        optional_columns=["Text"],
        field_mapping={
            "Date": "date",
            "Symbol": "security_symbol",
        },
        text_extraction={
            "source_column": "Text",
            "pattern": r"^(?P<security_symbol>.+)$",
        },
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_rejects_signed_amount_column_colliding_with_field_mapping(
    tmp_path: Path,
) -> None:
    _write_contract(
        tmp_path,
        required_columns=["Date", "Debit", "Amount"],
        field_mapping={"Date": "date", "Debit": "debit"},
        signed_amount_column={"source_column": "Amount"},
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_rejects_text_extraction_group_colliding_with_signed_amount_column(
    tmp_path: Path,
) -> None:
    """Cross-capability collision (review pass 2 correction): a
    ``text_extraction`` named group targeting ``credit`` collides with
    ``signed_amount_column``'s own implicit ``debit``/``credit`` output --
    neither capability maps through ``field_mapping``, so the original
    collision check (which only compared against ``field_mapping``) would
    have missed this and let one capability silently clobber the other's
    value at runtime."""
    _write_contract(
        tmp_path,
        required_columns=["Date", "Text", "Amount", "Marker"],
        field_mapping={"Date": "date"},
        text_extraction={
            "source_column": "Text",
            "pattern": r"^(?P<credit>[0-9.]+)$",
        },
        signed_amount_column={"source_column": "Amount"},
    )

    with pytest.raises(ContractRegistryError):
        ContractRegistry.load(tmp_path)


def test_load_accepts_valid_capability_columns(tmp_path: Path) -> None:
    """The positive case: a well-formed capability set with declared
    columns and no collisions loads cleanly."""
    _write_contract(
        tmp_path,
        required_columns=["Date", "Text", "Amount", "Marker"],
        field_mapping={"Date": "date"},
        text_extraction={
            "source_column": "Text",
            "pattern": r"^(?P<security_symbol>.+?) CONS "
            r"(?P<quantity>[0-9.]+)@(?P<price>[0-9.]+)",
            "required_marker": "CONS",
        },
        signed_amount_column={"source_column": "Amount"},
        reject_unless_column_equals={
            "source_column": "Marker",
            "allowed_value": "-",
        },
    )

    registry = ContractRegistry.load(tmp_path)
    contract = registry.detect(["Date", "Text", "Amount", "Marker"])
    assert contract.text_extraction is not None
    assert contract.signed_amount_column is not None
    assert contract.reject_unless_column_equals is not None


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


# --- list_providers() (Story 3.3) -------------------------------------------


def test_list_providers_single_contract(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        contract_id="solo",
        version="1",
        provider_id="solo_provider",
        provider_name="Solo Provider",
        account_type_ids=["sipp"],
    )
    registry = ContractRegistry.load(tmp_path)

    options = registry.list_providers()

    assert len(options) == 1
    assert options[0].provider_id == "solo_provider"
    assert options[0].provider_name == "Solo Provider"
    assert options[0].account_type_ids == ("sipp",)


def test_list_providers_unions_account_types_across_same_provider(
    tmp_path: Path,
) -> None:
    """Two contracts sharing a ``provider_id`` (e.g. one per account type)
    surface as one dropdown entry whose ``account_type_ids`` is the union
    of both."""
    _write_contract(
        tmp_path,
        contract_id="acme_sipp",
        version="1",
        provider_id="acme",
        provider_name="Acme Broker",
        account_type_ids=["sipp"],
    )
    _write_contract(
        tmp_path,
        contract_id="acme_isa",
        version="1",
        provider_id="acme",
        provider_name="Acme Broker",
        account_type_ids=["isa"],
        required_columns=["Date", "Balance"],
        field_mapping={"Date": "date", "Balance": "debit"},
    )
    registry = ContractRegistry.load(tmp_path)

    options = registry.list_providers()

    assert len(options) == 1
    assert options[0].provider_id == "acme"
    assert set(options[0].account_type_ids) == {"sipp", "isa"}


def test_list_providers_sorted_by_provider_id(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        contract_id="zeta",
        version="1",
        provider_id="zeta_provider",
        provider_name="Zeta",
    )
    _write_contract(
        tmp_path,
        contract_id="alpha",
        version="1",
        provider_id="alpha_provider",
        provider_name="Alpha",
        required_columns=["Date", "Balance"],
        field_mapping={"Date": "date", "Balance": "debit"},
    )
    registry = ContractRegistry.load(tmp_path)

    options = registry.list_providers()

    assert [opt.provider_id for opt in options] == ["alpha_provider", "zeta_provider"]


def test_load_fails_closed_on_provider_name_conflict(tmp_path: Path) -> None:
    """Two contracts sharing a ``provider_id`` but disagreeing on
    ``provider_name`` must fail closed at construction time, rather than
    let load order silently pick a winner (mirrors the duplicate-
    ``(contract_id, version)`` fail-closed convention) or defer the failure
    to the first live call to ``list_providers()``."""
    _write_contract(
        tmp_path,
        contract_id="dupe_a",
        version="1",
        provider_id="dupe",
        provider_name="Dupe One",
    )
    _write_contract(
        tmp_path,
        contract_id="dupe_b",
        version="1",
        provider_id="dupe",
        provider_name="Dupe Two",
        required_columns=["Date", "Balance"],
        field_mapping={"Date": "date", "Balance": "debit"},
    )

    with pytest.raises(ContractRegistryError) as exc:
        ContractRegistry.load(tmp_path)

    assert "dupe" in str(exc.value)
    assert "Dupe One" in str(exc.value)
    assert "Dupe Two" in str(exc.value)
