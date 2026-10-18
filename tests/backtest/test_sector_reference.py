"""Sector reference pin: build, digest and validation."""

from __future__ import annotations

import pytest

from app.services.backtest.sector_reference import (
    SECTOR_ASSIGNMENTS_PARAM,
    SECTOR_DIGEST_PARAM,
    SECTOR_REFERENCE_VERSION,
    SECTOR_REFERENCE_VERSION_PARAM,
    build_sector_pin,
    load_sector_reference,
    validate_sector_pin,
)

UNIVERSE = ("s1", "s2", "s3")


def _parameters() -> dict[str, object]:
    pin = build_sector_pin(
        UNIVERSE,
        {"s1": "aapl", "s2": "XOM", "s3": "ZZZ"},
        {
            "AAPL": "Technology",
            "XOM": "Energy",
        },
    )
    return {SECTOR_REFERENCE_VERSION_PARAM: SECTOR_REFERENCE_VERSION, **pin}


def test_reference_file_loads_uppercase_tickers() -> None:
    reference = load_sector_reference()

    assert reference["AAPL"] == "Technology"
    assert all(key == key.upper() and value for key, value in reference.items())


def test_pin_excludes_unmapped_and_validates() -> None:
    parameters = _parameters()

    assert parameters[SECTOR_ASSIGNMENTS_PARAM] == {
        "s1": "Technology",
        "s2": "Energy",
    }
    validate_sector_pin(parameters, UNIVERSE)


def test_tampered_assignments_fail_digest() -> None:
    parameters = _parameters()
    assert isinstance(parameters[SECTOR_ASSIGNMENTS_PARAM], dict)
    parameters[SECTOR_ASSIGNMENTS_PARAM]["s1"] = "Energy"

    with pytest.raises(ValueError, match="digest"):
        validate_sector_pin(parameters, UNIVERSE)


def test_assignment_outside_universe_rejected() -> None:
    with pytest.raises(ValueError, match="outside"):
        validate_sector_pin(_parameters(), ("s1",))


def test_declared_reference_without_pin_rejected() -> None:
    with pytest.raises(ValueError, match="missing"):
        validate_sector_pin(
            {SECTOR_REFERENCE_VERSION_PARAM: SECTOR_REFERENCE_VERSION}, UNIVERSE
        )


def test_stray_pin_without_reference_rejected() -> None:
    parameters = _parameters()
    del parameters[SECTOR_REFERENCE_VERSION_PARAM]

    with pytest.raises(ValueError, match="without"):
        validate_sector_pin(parameters, UNIVERSE)
    validate_sector_pin({}, UNIVERSE)
    assert SECTOR_DIGEST_PARAM in _parameters()
