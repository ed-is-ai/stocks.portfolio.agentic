"""Pinned sector reference for sector-rotation backtests.

One authority for resolving, hashing and validating the host-bound
``sector_assignments`` parameter. The mapping is a current sector snapshot
(see ``config/sector_reference.v1.json``), resolved at launch to
``{security_id: sector}`` for the selected universe and sealed in the run
manifest parameters so a replay never re-reads the file.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from pathlib import Path

from app.core.config import ROOT_DIR
from app.services.backtest.canonical_manifest import manifest_digest

SECTOR_REFERENCE_VERSION = "sector_reference.v1"
SECTOR_REFERENCE_VERSION_PARAM = "sector_reference_version"
SECTOR_ASSIGNMENTS_PARAM = "sector_assignments"
SECTOR_DIGEST_PARAM = "sector_reference_digest"
SECTOR_HOST_PARAMS = (SECTOR_ASSIGNMENTS_PARAM, SECTOR_DIGEST_PARAM)
SECTOR_REFERENCE_PATH = ROOT_DIR / "config" / "sector_reference.v1.json"


def load_sector_reference(path: Path = SECTOR_REFERENCE_PATH) -> dict[str, str]:
    """Return the ``{TICKER: sector}`` reference, dropping blank entries."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != SECTOR_REFERENCE_VERSION:
        raise ValueError("unsupported sector reference version")
    return {
        str(ticker).upper(): sector
        for ticker, sector in payload["sectors"].items()
        if isinstance(sector, str) and sector.strip()
    }


def sector_digest(assignments: Mapping[str, str]) -> str:
    """Return the canonical digest of a resolved assignment mapping."""
    return manifest_digest(dict(assignments))


def build_sector_pin(
    security_ids: Collection[str],
    symbols_by_id: Mapping[str, str],
    reference: Mapping[str, str],
) -> dict[str, object]:
    """Resolve the universe to sector assignments; unmapped ids are excluded."""
    assignments = {
        security_id: reference[symbols_by_id[security_id].upper()]
        for security_id in sorted(security_ids)
        if symbols_by_id.get(security_id, "").upper() in reference
    }
    return {
        SECTOR_ASSIGNMENTS_PARAM: assignments,
        SECTOR_DIGEST_PARAM: sector_digest(assignments),
    }


def validate_sector_pin(
    parameters: Mapping[str, object], universe: Collection[str]
) -> None:
    """Raise ``ValueError`` unless the sealed pin is complete and untampered.

    Parameters without ``sector_reference_version`` carry no pin and must not
    carry stray pin keys either.
    """
    assignments = parameters.get(SECTOR_ASSIGNMENTS_PARAM)
    digest = parameters.get(SECTOR_DIGEST_PARAM)
    if SECTOR_REFERENCE_VERSION_PARAM not in parameters:
        if assignments is not None or digest is not None:
            raise ValueError("sector pin supplied without a sector reference")
        return
    if parameters[SECTOR_REFERENCE_VERSION_PARAM] != SECTOR_REFERENCE_VERSION:
        raise ValueError("unsupported sector reference version")
    if (
        not isinstance(assignments, Mapping)
        or not assignments
        or not all(
            isinstance(key, str) and isinstance(value, str) and value
            for key, value in assignments.items()
        )
    ):
        raise ValueError("sector assignments are missing or malformed")
    if not set(assignments) <= set(universe):
        raise ValueError("sector assignment outside the selected universe")
    if digest != sector_digest(assignments):
        raise ValueError("sector reference digest does not match assignments")
