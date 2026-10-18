"""Pinned sector reference for sector-rotation backtests.

One authority for resolving, hashing and validating the host-bound
``sector_assignments`` parameter. The mapping is a current sector snapshot
(see ``config/sector_reference.v1.json``), resolved at launch to
``{security_id: sector}`` for the selected universe and sealed in the run
manifest parameters so a replay never re-reads the file.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import NamedTuple

from app.core.config import ROOT_DIR

SECTOR_REFERENCE_VERSION = "sector_reference.v1"
SECTOR_REFERENCE_VERSION_PARAM = "sector_reference_version"
SECTOR_ASSIGNMENTS_PARAM = "sector_assignments"
SECTOR_DIGEST_PARAM = "sector_reference_digest"
SECTOR_HOST_PARAMS = (SECTOR_ASSIGNMENTS_PARAM, SECTOR_DIGEST_PARAM)
SECTOR_REFERENCE_PATH = ROOT_DIR / "config" / "sector_reference.v1.json"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class SectorReference(NamedTuple):
    """The ``{TICKER: sector}`` reference and the SHA-256 of its file bytes."""

    sectors: dict[str, str]
    digest: str


def load_sector_reference(path: Path = SECTOR_REFERENCE_PATH) -> SectorReference:
    """Read the reference once, dropping blank entries.

    Raises ``ValueError`` for any unreadable, malformed or unsupported file.
    """
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sector reference is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("sector reference must be a JSON object")
    if payload.get("version") != SECTOR_REFERENCE_VERSION:
        raise ValueError("unsupported sector reference version")
    sectors = payload.get("sectors")
    if not isinstance(sectors, dict):
        raise ValueError("sector reference 'sectors' is missing or invalid")
    return SectorReference(
        {
            str(ticker).upper(): sector
            for ticker, sector in sectors.items()
            if isinstance(sector, str) and sector.strip()
        },
        hashlib.sha256(raw).hexdigest(),
    )


def build_sector_pin(
    security_ids: Collection[str],
    symbols_by_id: Mapping[str, str],
    reference: SectorReference,
) -> dict[str, object]:
    """Resolve the universe to sector assignments; unmapped ids are excluded."""
    assignments = {
        security_id: reference.sectors[symbols_by_id[security_id].upper()]
        for security_id in sorted(security_ids)
        if symbols_by_id.get(security_id, "").upper() in reference.sectors
    }
    return {
        SECTOR_ASSIGNMENTS_PARAM: assignments,
        SECTOR_DIGEST_PARAM: reference.digest,
    }


def validate_sector_pin(
    parameters: Mapping[str, object], universe: Collection[str]
) -> None:
    """Raise ``ValueError`` unless the sealed pin is complete and well formed.

    Parameters without ``sector_reference_version`` carry no pin and must not
    carry stray pin keys either. The digest is the sealed SHA-256 of the
    reference file the assignments were resolved from; the manifest digest
    seals both, so a replay never re-reads the file.
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
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        raise ValueError("sector reference digest is missing or malformed")
