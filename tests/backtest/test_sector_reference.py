"""Sector reference pin: build, digest and validation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.services.backtest.backtest_launch_service as launch_module
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchService,
    LaunchFieldError,
)
from app.services.backtest.sector_reference import (
    SECTOR_ASSIGNMENTS_PARAM,
    SECTOR_DIGEST_PARAM,
    SECTOR_REFERENCE_PATH,
    SECTOR_REFERENCE_VERSION,
    SECTOR_REFERENCE_VERSION_PARAM,
    SectorReference,
    build_sector_pin,
    load_sector_reference,
    validate_sector_pin,
)

UNIVERSE = ("s1", "s2", "s3")
FILE_DIGEST = "f" * 64


def _parameters() -> dict[str, object]:
    pin = build_sector_pin(
        UNIVERSE,
        {"s1": "aapl", "s2": "XOM", "s3": "ZZZ"},
        SectorReference({"AAPL": "Technology", "XOM": "Energy"}, FILE_DIGEST),
    )
    return {SECTOR_REFERENCE_VERSION_PARAM: SECTOR_REFERENCE_VERSION, **pin}


def test_reference_file_loads_uppercase_tickers_and_hashes_raw_bytes() -> None:
    reference = load_sector_reference()

    assert reference.sectors["AAPL"] == "Technology"
    assert all(k == k.upper() and v for k, v in reference.sectors.items())
    expected = hashlib.sha256(SECTOR_REFERENCE_PATH.read_bytes()).hexdigest()
    assert reference.digest == expected


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "unreadable"),
        ("{not json", "unreadable"),
        ("[]", "JSON object"),
        ('{"version": "sector_reference.v0", "sectors": {}}', "version"),
        ('{"version": "sector_reference.v1"}', "sectors"),
        ('{"version": "sector_reference.v1", "sectors": []}', "sectors"),
    ],
)
def test_bad_reference_file_raises_value_error(
    tmp_path: Path, content: str | None, match: str
) -> None:
    path = tmp_path / "reference.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        load_sector_reference(path)


def test_pin_excludes_unmapped_and_validates() -> None:
    parameters = _parameters()

    assert parameters[SECTOR_ASSIGNMENTS_PARAM] == {
        "s1": "Technology",
        "s2": "Energy",
    }
    validate_sector_pin(parameters, UNIVERSE)


def test_pin_seals_the_reference_file_digest() -> None:
    assert _parameters()[SECTOR_DIGEST_PARAM] == FILE_DIGEST


@pytest.mark.parametrize("digest", [None, "", "F" * 64, "f" * 63, 7])
def test_missing_or_malformed_digest_rejected(digest: object) -> None:
    parameters = {**_parameters(), SECTOR_DIGEST_PARAM: digest}

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


# ---------------------------------------------------------------------------
# BacktestLaunchService._bind_sector_pin
# ---------------------------------------------------------------------------

_STRATEGY = SimpleNamespace(
    parameters=(SimpleNamespace(name=SECTOR_REFERENCE_VERSION_PARAM),)
)
_IDENTITIES = (
    ("s1", "XNAS", "AAPL", "e"),
    ("s2", "XNAS", "MSFT", "e"),
    ("s3", "XNYS", "XOM", "e"),
    ("s4", "XNYS", None, "e"),
)


def _bind(
    parameters: dict[str, Any], selected: tuple[str, ...] = ("s1", "s2", "s3", "s4")
) -> tuple[dict[str, Any], list[LaunchFieldError]]:
    service = BacktestLaunchService.__new__(BacktestLaunchService)
    service._backtest_repo = cast(  # type: ignore[assignment]
        Any, SimpleNamespace(identity_rows=lambda: _IDENTITIES)
    )
    command = SimpleNamespace(
        universe_selection=SimpleNamespace(canonical_security_ids=selected)
    )
    errors: list[LaunchFieldError] = []
    bound = service._bind_sector_pin(
        cast(Any, _STRATEGY), cast(Any, command), parameters, errors
    )
    return bound, errors


@pytest.fixture
def _reference(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SectorReference(
        {"AAPL": "Technology", "MSFT": "Technology", "XOM": "Energy", "NONE": "X"},
        FILE_DIGEST,
    )
    monkeypatch.setattr(launch_module, "load_sector_reference", lambda: reference)


@pytest.mark.usefixtures("_reference")
def test_bind_seals_pin_and_skips_null_provider_symbol() -> None:
    bound, errors = _bind({"min_sector_members": 2})

    assert errors == []
    assert bound[SECTOR_ASSIGNMENTS_PARAM] == {
        "s1": "Technology",
        "s2": "Technology",
        "s3": "Energy",
    }
    assert bound[SECTOR_DIGEST_PARAM] == FILE_DIGEST


@pytest.mark.usefixtures("_reference")
def test_bind_rejects_when_no_sector_reaches_min_members() -> None:
    _bound, errors = _bind({"min_sector_members": 3})

    assert len(errors) == 1
    assert "No sector reaches 3 mapped members" in errors[0].message
    assert "3 of 4 selected securities" in errors[0].message


@pytest.mark.usefixtures("_reference")
def test_bind_rejects_selection_without_any_mapped_security() -> None:
    _bound, errors = _bind({"min_sector_members": 1}, selected=("s4",))

    assert errors and "0 of 1 selected securities" in errors[0].message


def test_bind_reports_unusable_reference_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(
        launch_module, "load_sector_reference", lambda: load_sector_reference(missing)
    )

    bound, errors = _bind({"min_sector_members": 1})

    assert SECTOR_ASSIGNMENTS_PARAM not in bound
    assert errors and errors[0].field == "form"
    assert "Sector reference is unusable" in errors[0].message
