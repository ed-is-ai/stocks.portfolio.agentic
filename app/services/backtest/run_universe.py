"""Canonical selected-security universe identity for one Backtest Run.

A Run's universe is whatever set of securities the host selected for it.
Selection arrives in whatever order a caller happened to build it in, so
this module is the one authority that turns that raw selection into a
sorted, deduplicated, immutable tuple and derives
:func:`run_universe_digest` from *that canonical tuple only* -- two
selections that resolve to the same set always produce the same digest,
regardless of order or repeats.

Deliberately dependency-free apart from the shared canonical-JSON/SHA-256
helpers in ``canonical_manifest``: Skill discovery, ``MarketView``
construction, and a future Run-universe selector all reuse this one
canonicalizer rather than each normalizing a list of IDs themselves.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from app.services.backtest.canonical_manifest import manifest_digest

#: Versioned digest payload identity -- changing the canonical tuple's
#: serialization shape means changing this string too.
RUN_UNIVERSE_VERSION = "run_universe.v1"


class RunUniverseErrorCode(StrEnum):
    """Stable, machine-readable canonicalization failure codes."""

    EMPTY_UNIVERSE = "empty_universe"
    INVALID_SECURITY_ID = "invalid_security_id"


class RunUniverseError(ValueError):
    """One typed canonicalization failure with a stable ``.code``."""

    def __init__(self, code: RunUniverseErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def canonical_run_universe(security_ids: Iterable[object]) -> tuple[str, ...]:
    """Return ``security_ids`` as a sorted, deduplicated, immutable tuple.

    A duplicate ID is collapsed rather than rejected -- two UI selection
    orders of the same set, one of which repeats an ID, must both
    canonicalize to the identical tuple. An empty selection, a non-string
    element, or an element with leading/trailing whitespace is rejected
    with a stable :class:`RunUniverseErrorCode`: whitespace variants would
    otherwise canonicalize to two different tuples for what a user means
    as one security.
    """
    unique: set[str] = set()
    for value in security_ids:
        if not isinstance(value, str) or not value or value != value.strip():
            raise RunUniverseError(
                RunUniverseErrorCode.INVALID_SECURITY_ID,
                f"security ID must be a non-empty, unpadded string, got {value!r}",
            )
        unique.add(value)
    if not unique:
        raise RunUniverseError(
            RunUniverseErrorCode.EMPTY_UNIVERSE,
            "a Run universe must select at least one security",
        )
    return tuple(sorted(unique))


def run_universe_digest(
    security_ids: Iterable[object],
    *,
    universe_schema: str = "strategy_universe.v1",
    mode: str = "selected-securities",
    parameter: str = "security_ids",
    profile_hash: str = "0" * 64,
) -> str:
    """Return the SHA-256 digest of ``security_ids``' canonical tuple.

    A pure function of :func:`canonical_run_universe`'s output, so
    selection order and duplicates can never change the digest.
    """
    for value in (universe_schema, mode, parameter, profile_hash):
        if not isinstance(value, str) or not value or value != value.strip():
            raise RunUniverseError(
                RunUniverseErrorCode.INVALID_SECURITY_ID,
                "universe identity is malformed",
            )
    return manifest_digest(
        {
            "schema_version": RUN_UNIVERSE_VERSION,
            "universe_schema": universe_schema,
            "mode": mode,
            "parameter": parameter,
            "profile_hash": profile_hash,
            "selected_security_ids": list(canonical_run_universe(security_ids)),
        }
    )


__all__ = [
    "RUN_UNIVERSE_VERSION",
    "RunUniverseError",
    "RunUniverseErrorCode",
    "canonical_run_universe",
    "run_universe_digest",
]
