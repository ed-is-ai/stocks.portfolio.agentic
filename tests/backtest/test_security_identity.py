from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services.backtest.security_identity import (
    AliasEntryV1,
    IdentityAmbiguousError,
    SecurityAliasManifestV1,
    SecurityAliasResolver,
    SecurityIdentityV1,
    SecurityIdentityRegistryV1,
)


def _identity(security_id: str, symbol: str = "AAPL") -> SecurityIdentityV1:
    return SecurityIdentityV1(
        security_id=security_id,
        mic="XNAS",
        provider_symbol=symbol,
        evidence_digest="e" * 64,
    )


def _alias(
    security_id: str,
    *,
    start: date | None,
    end: date | None,
) -> AliasEntryV1:
    return AliasEntryV1(
        security_id=security_id,
        provider="yfinance",
        mic="XNAS",
        observed_symbol=" old ",
        effective_from=start,
        effective_to=end,
        evidence_source="manual-review",
        evidence_digest="a" * 64,
        provenance="manual_override",
    )


def test_registry_revision_is_stable_and_security_ids_are_not_symbol_derived() -> None:
    created = datetime(2026, 8, 10, tzinfo=timezone.utc)
    first = _identity("7d16e313-2dd2-45a8-8a33-7b61b7df3fc8")
    second = _identity("435d3ca4-cbbb-4da1-a486-292beb19125a", "MSFT")

    left = SecurityIdentityRegistryV1.build((second, first), created_at=created)
    right = SecurityIdentityRegistryV1.build((first, second), created_at=created)

    assert left.revision == right.revision
    assert left.identities == right.identities
    assert "AAPL" not in first.security_id
    assert left.evidence_digest != ""
    assert SecurityIdentityV1(
        security_id="1b4ee6bb-c697-4a9c-b909-8d55fae47640",
        mic="BATS",
        provider_symbol="CBOE",
        evidence_digest="b" * 64,
    ).mic == "BATS"


def test_alias_resolution_uses_provider_mic_symbol_and_half_open_date() -> None:
    first = "7d16e313-2dd2-45a8-8a33-7b61b7df3fc8"
    second = "435d3ca4-cbbb-4da1-a486-292beb19125a"
    manifest = SecurityAliasManifestV1.build(
        (
            _alias(first, start=None, end=date(2020, 1, 1)),
            _alias(second, start=date(2020, 1, 1), end=None),
        ),
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    resolver = SecurityAliasResolver(manifest)

    assert resolver.resolve("yfinance", "XNAS", "OLD", date(2019, 12, 31)) == first
    assert resolver.resolve("yfinance", "XNAS", " old ", date(2020, 1, 1)) == second
    assert resolver.resolve("yfinance", "XNYS", "OLD", date(2020, 1, 1)) is None


def test_alias_manifest_rejects_overlap_and_does_not_fuzzy_join() -> None:
    first = "7d16e313-2dd2-45a8-8a33-7b61b7df3fc8"
    second = "435d3ca4-cbbb-4da1-a486-292beb19125a"
    with pytest.raises(IdentityAmbiguousError, match="overlap"):
        SecurityAliasManifestV1.build(
            (
                _alias(first, start=None, end=date(2021, 1, 1)),
                _alias(second, start=date(2020, 1, 1), end=None),
            ),
            created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

    manifest = SecurityAliasManifestV1.build(
        (_alias(first, start=None, end=None),),
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    resolver = SecurityAliasResolver(manifest)
    assert resolver.resolve("yfinance", "XNAS", "OLD.L", date(2020, 1, 1)) is None
    assert resolver.resolve("yfinance", "XNAS", "OLD-", date(2020, 1, 1)) is None
