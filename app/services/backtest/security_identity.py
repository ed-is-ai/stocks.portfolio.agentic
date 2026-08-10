"""Immutable security identities and effective-dated provider aliases."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from app.services.backtest.canonical_manifest import manifest_digest

IDENTITY_REGISTRY_VERSION = "SecurityIdentityRegistryV1"
ALIAS_MANIFEST_VERSION = "SecurityAliasManifestV1"
_SUPPORTED_MICS = {"XNAS", "XNYS", "XLON"}


class IdentityAmbiguousError(ValueError):
    """Alias evidence cannot identify exactly one security."""

    code = "identity_ambiguous"


def normalize_symbol(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    if not normalized:
        raise ValueError("symbol is empty")
    return normalized


@dataclass(frozen=True, order=True)
class SecurityIdentityV1:
    security_id: str
    mic: str
    provider_symbol: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.mic not in _SUPPORTED_MICS:
            raise ValueError(f"unsupported MIC: {self.mic}")
        object.__setattr__(
            self, "provider_symbol", normalize_symbol(self.provider_symbol)
        )
        if not self.security_id or self.security_id in self.provider_symbol:
            raise ValueError("security_id must be opaque")


@dataclass(frozen=True)
class SecurityIdentityRegistryV1:
    revision: str
    evidence_digest: str
    identities: tuple[SecurityIdentityV1, ...]
    created_at: datetime
    schema_version: str = IDENTITY_REGISTRY_VERSION

    @classmethod
    def build(
        cls, identities: tuple[SecurityIdentityV1, ...], *, created_at: datetime
    ) -> SecurityIdentityRegistryV1:
        ordered = tuple(sorted(identities, key=lambda item: item.security_id))
        if len({item.security_id for item in ordered}) != len(ordered):
            raise ValueError("duplicate security_id")
        keys = {(item.mic, item.provider_symbol) for item in ordered}
        if len(keys) != len(ordered):
            raise IdentityAmbiguousError("canonical identity collision")
        evidence_digest = manifest_digest(
            [
                {
                    "security_id": item.security_id,
                    "mic": item.mic,
                    "provider_symbol": item.provider_symbol,
                    "evidence_digest": item.evidence_digest,
                }
                for item in ordered
            ]
        )
        revision = manifest_digest(
            {
                "schema_version": IDENTITY_REGISTRY_VERSION,
                "evidence_digest": evidence_digest,
                "identities": ordered,
            }
        )
        return cls(revision, evidence_digest, ordered, created_at)


@dataclass(frozen=True, order=True)
class AliasEntryV1:
    security_id: str
    provider: str
    mic: str
    observed_symbol: str
    effective_from: date | None
    effective_to: date | None
    evidence_source: str
    evidence_digest: str
    provenance: Literal["provider_evidence", "manual_override"]

    def __post_init__(self) -> None:
        if self.mic not in _SUPPORTED_MICS:
            raise ValueError(f"unsupported MIC: {self.mic}")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(
            self, "observed_symbol", normalize_symbol(self.observed_symbol)
        )
        if self.effective_from and self.effective_to:
            if self.effective_from >= self.effective_to:
                raise ValueError("alias effective interval is empty")
        if not self.evidence_source or not self.evidence_digest:
            raise ValueError("alias evidence is required")

    def contains(self, requested_date: date) -> bool:
        return (
            self.effective_from is None or self.effective_from <= requested_date
        ) and (self.effective_to is None or requested_date < self.effective_to)


@dataclass(frozen=True)
class SecurityAliasManifestV1:
    revision: str
    evidence_digest: str
    entries: tuple[AliasEntryV1, ...]
    created_at: datetime
    schema_version: str = ALIAS_MANIFEST_VERSION

    @classmethod
    def build(
        cls, entries: tuple[AliasEntryV1, ...], *, created_at: datetime
    ) -> SecurityAliasManifestV1:
        ordered = tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.provider,
                    item.mic,
                    item.observed_symbol,
                    item.effective_from or date.min,
                    item.effective_to or date.max,
                    item.security_id,
                ),
            )
        )
        previous_by_key: dict[tuple[str, str, str], AliasEntryV1] = {}
        for entry in ordered:
            key = (entry.provider, entry.mic, entry.observed_symbol)
            previous = previous_by_key.get(key)
            if previous is not None and _overlap(previous, entry):
                raise IdentityAmbiguousError("alias intervals overlap")
            previous_by_key[key] = entry
        evidence_digest = manifest_digest(
            [
                {
                    "evidence_source": entry.evidence_source,
                    "evidence_digest": entry.evidence_digest,
                    "provenance": entry.provenance,
                }
                for entry in ordered
            ]
        )
        revision = manifest_digest(
            {
                "schema_version": ALIAS_MANIFEST_VERSION,
                "evidence_digest": evidence_digest,
                "entries": ordered,
            }
        )
        return cls(revision, evidence_digest, ordered, created_at)


def _overlap(left: AliasEntryV1, right: AliasEntryV1) -> bool:
    left_end = left.effective_to or date.max
    right_end = right.effective_to or date.max
    left_start = left.effective_from or date.min
    right_start = right.effective_from or date.min
    return left_start < right_end and right_start < left_end


class SecurityAliasResolver:
    def __init__(self, manifest: SecurityAliasManifestV1) -> None:
        self.manifest = manifest

    def resolve(
        self, provider: str, mic: str, observed_symbol: str, requested_date: date
    ) -> str | None:
        key = (provider.strip().lower(), mic, normalize_symbol(observed_symbol))
        matches = {
            entry.security_id
            for entry in self.manifest.entries
            if (entry.provider, entry.mic, entry.observed_symbol) == key
            and entry.contains(requested_date)
        }
        if len(matches) > 1:
            raise IdentityAmbiguousError("alias resolves to multiple securities")
        return next(iter(matches), None)
