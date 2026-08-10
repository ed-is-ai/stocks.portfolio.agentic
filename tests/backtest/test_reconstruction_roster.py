from __future__ import annotations

from datetime import datetime, timezone
import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.services.backtest.reconstruction_roster import (
    DataHubRosterSourceAdapter,
    ReconstructionRosterCaptureService,
    MarketIdentityEvidence,
    ReconstructionRosterPolicyV1,
    RosterCaptureError,
    RosterSource,
    RosterSourcePayloadV1,
    TradingViewRosterSourceAdapter,
    YFinanceMarketIdentityResolver,
)
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.security_identity import SecurityAliasManifestV1
from app.integrations.tv_screener import TradingViewRosterEvidence
from app.schemas.source_health import SourceName, SourceResult


NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def _payload(
    source: RosterSource, rows: Sequence[Mapping[str, object]]
) -> RosterSourcePayloadV1:
    return RosterSourcePayloadV1.build(
        source=source,
        rows=rows,
        retrieved_at=NOW,
        source_version=f"{source.value}-v1",
        package_version="test",
        config_version="ReconstructionRosterPolicyV1",
    )


def test_policy_requires_fixed_sources_and_normalizes_union() -> None:
    calls: list[str] = []

    def resolve_datahub(symbol: str, row: dict[str, object]) -> MarketIdentityEvidence:
        calls.append(symbol)
        return MarketIdentityEvidence(
            mic="XNAS",
            currency="USD",
            quote_unit="USD",
            evidence_source="yfinance_metadata",
            evidence_digest="i" * 64,
        )

    payloads = (
        _payload(
            RosterSource.DATAHUB_SP500,
            [
                {"symbol": "  aapl  ", "name": "Apple"},
                {"symbol": "HELD", "source_class": "portfolio_only"},
            ],
        ),
        _payload(
            RosterSource.TRADINGVIEW_US,
            [{"symbol": "NASDAQ:ＡＡＰＬ", "exchange": "NASDAQ", "currency": "USD"}],
        ),
        _payload(
            RosterSource.TRADINGVIEW_UK,
            [{"symbol": "LSE:ulvr", "exchange": "LSE", "currency": "GBp"}],
        ),
    )

    members = ReconstructionRosterPolicyV1().normalize(payloads, resolve_datahub)

    assert calls == ["AAPL"]
    assert [(m.mic, m.provider_symbol) for m in members] == [
        ("XLON", "ULVR.L"),
        ("XNAS", "AAPL"),
    ]
    aapl = next(member for member in members if member.provider_symbol == "AAPL")
    assert aapl.source_memberships == ("datahub_sp500", "tradingview_us")
    assert aapl.calendar == "XNYS"


def test_policy_fails_on_wrong_order_empty_payload_or_identity_conflict() -> None:
    policy = ReconstructionRosterPolicyV1()
    us = _payload(
        RosterSource.TRADINGVIEW_US,
        [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"}],
    )
    datahub = _payload(RosterSource.DATAHUB_SP500, [{"symbol": "AAPL"}])
    uk = _payload(
        RosterSource.TRADINGVIEW_UK,
        [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}],
    )
    resolver = lambda _symbol, _row: MarketIdentityEvidence(  # noqa: E731
        "XNYS", "USD", "USD", "test", "e" * 64
    )

    with pytest.raises(RosterCaptureError, match="fixed source order"):
        policy.normalize((us, datahub, uk), resolver)
    with pytest.raises(RosterCaptureError, match="empty"):
        policy.normalize(
            (
                _payload(RosterSource.DATAHUB_SP500, []),
                us,
                uk,
            ),
            resolver,
        )

    conflicting = _payload(
        RosterSource.TRADINGVIEW_US,
        [{"symbol": "NYSE:AAPL", "exchange": "NYSE", "currency": "GBP"}],
    )
    with pytest.raises(RosterCaptureError, match="currency"):
        policy.normalize((datahub, conflicting, uk), resolver)


def test_payload_digest_is_stable_but_retrieval_time_is_retained() -> None:
    rows = [
        {"symbol": "AAPL", "name": "Apple"},
        {"symbol": "MSFT", "name": "Microsoft"},
    ]
    left = _payload(RosterSource.DATAHUB_SP500, rows)
    right = RosterSourcePayloadV1.build(
        source=RosterSource.DATAHUB_SP500,
        rows=list(reversed(rows)),
        retrieved_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        source_version="datahub_sp500-v1",
        package_version="test",
        config_version="ReconstructionRosterPolicyV1",
    )
    assert left.payload_digest == right.payload_digest
    assert left.retrieved_at != right.retrieved_at


def test_capture_commits_manifest_once_per_lineage_and_refresh_reuses_identities(
    tmp_path,
) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    calls: list[str] = []
    payloads = {
        RosterSource.DATAHUB_SP500: _payload(
            RosterSource.DATAHUB_SP500, [{"symbol": "AAPL", "name": "Apple"}]
        ),
        RosterSource.TRADINGVIEW_US: _payload(
            RosterSource.TRADINGVIEW_US,
            [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"}],
        ),
        RosterSource.TRADINGVIEW_UK: _payload(
            RosterSource.TRADINGVIEW_UK,
            [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}],
        ),
    }

    def fetch(source: RosterSource):
        def _fetch() -> RosterSourcePayloadV1:
            calls.append(source.value)
            return payloads[source]

        return _fetch

    resolver = lambda _symbol, _row: MarketIdentityEvidence(  # noqa: E731
        "XNAS", "USD", "USD", "yfinance_metadata", "i" * 64
    )
    ids = iter(
        (
            "7d16e313-2dd2-45a8-8a33-7b61b7df3fc8",
            "435d3ca4-cbbb-4da1-a486-292beb19125a",
        )
    )
    empty_aliases = SecurityAliasManifestV1.build((), created_at=NOW)
    fetchers = (
        fetch(RosterSource.DATAHUB_SP500),
        fetch(RosterSource.TRADINGVIEW_US),
        fetch(RosterSource.TRADINGVIEW_UK),
    )
    service = ReconstructionRosterCaptureService(
        repo,
        fetchers,
        resolver,
        id_generator=lambda: next(ids),
        clock=lambda: NOW,
    )

    first = service.capture("lineage-1", empty_aliases)
    again = service.capture("lineage-1", empty_aliases)
    refreshed = service.capture("lineage-2", empty_aliases)

    assert first.roster_digest == again.roster_digest
    assert calls == [source.value for source in RosterSource] * 2
    assert [member.security_id for member in first.members] == [
        member.security_id for member in refreshed.members
    ]
    manifest = json.loads(first.canonical_manifest_json)
    assert manifest["expected_count"] == 2
    assert manifest["members"][0]["identity_evidence"][0]["evidence_source"]
    assert manifest["provenance"]["universe_basis"] == "captured_configured_roster"
    assert manifest["provenance"]["point_in_time_universe"] is False
    assert "not a point-in-time market universe" in manifest["provenance"]["warning"]


def test_capture_failure_writes_nothing(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    calls: list[str] = []

    def good() -> RosterSourcePayloadV1:
        calls.append("datahub")
        return _payload(RosterSource.DATAHUB_SP500, [{"symbol": "AAPL"}])

    def fail() -> RosterSourcePayloadV1:
        calls.append("us")
        raise RosterCaptureError("TradingView unavailable", code="provider_unavailable")

    def should_not_run() -> RosterSourcePayloadV1:
        calls.append("uk")
        raise AssertionError("later source must not run")

    resolver = lambda _symbol, _row: MarketIdentityEvidence(  # noqa: E731
        "XNAS", "USD", "USD", "test", "e" * 64
    )
    service = ReconstructionRosterCaptureService(
        repo, (good, fail, should_not_run), resolver, clock=lambda: NOW
    )
    aliases = SecurityAliasManifestV1.build((), created_at=NOW)

    with pytest.raises(RosterCaptureError, match="unavailable"):
        service.capture("failed-lineage", aliases)
    assert calls == ["datahub", "us"]
    assert repo.roster_digest_for_lineage("failed-lineage") is None


def test_strict_source_adapters_preserve_evidence_and_reject_fallback_states() -> None:
    datahub = DataHubRosterSourceAdapter(
        lambda: [{"symbol": "AAPL", "name": "Apple", "sector": "Technology"}],
        clock=lambda: NOW,
    )()
    assert datahub.source is RosterSource.DATAHUB_SP500
    assert datahub.rows[0]["symbol"] == "AAPL"

    result = SourceResult.from_items(
        SourceName.TRADINGVIEW_US, ["AAPL"], started_at=NOW
    )
    evidence = TradingViewRosterEvidence(
        result,
        ({"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"},),
    )
    tv = TradingViewRosterSourceAdapter("US", fetch=lambda **_kwargs: evidence)()
    assert tv.source is RosterSource.TRADINGVIEW_US
    assert tv.rows[0]["exchange"] == "NASDAQ"

    with pytest.raises(RosterCaptureError, match="DataHub"):
        DataHubRosterSourceAdapter(lambda: None, clock=lambda: NOW)()
    with pytest.raises(RosterCaptureError, match="malformed"):
        DataHubRosterSourceAdapter(
            lambda: [{"symbol": "AAPL", "name": "Apple"}], clock=lambda: NOW
        )()


def test_datahub_market_identity_resolver_requires_explicit_exchange_and_currency() -> (
    None
):
    class Ticker:
        def __init__(self, metadata):
            self.metadata = metadata

        def get_history_metadata(self, repair=False):
            assert repair is False
            return self.metadata

    resolver = YFinanceMarketIdentityResolver(
        lambda _symbol: Ticker({"exchangeName": "NMS", "currency": "USD"})
    )
    identity = resolver("AAPL", {})
    assert (identity.mic, identity.currency) == ("XNAS", "USD")

    ambiguous = YFinanceMarketIdentityResolver(
        lambda _symbol: Ticker({"exchangeName": "unknown", "currency": "USD"})
    )
    with pytest.raises(RosterCaptureError, match="unsupported"):
        ambiguous("AAPL", {})


def test_concurrent_identical_capture_returns_one_lineage_winner(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    aliases = SecurityAliasManifestV1.build((), created_at=NOW)
    barrier = Barrier(2)
    payloads = (
        _payload(
            RosterSource.DATAHUB_SP500,
            [{"symbol": "AAPL", "name": "Apple", "sector": "Technology"}],
        ),
        _payload(
            RosterSource.TRADINGVIEW_US,
            [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"}],
        ),
        _payload(
            RosterSource.TRADINGVIEW_UK,
            [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}],
        ),
    )

    def make_service(id_prefix: str) -> ReconstructionRosterCaptureService:
        ids = iter((f"{id_prefix}-1", f"{id_prefix}-2"))

        def last() -> RosterSourcePayloadV1:
            barrier.wait()
            return payloads[2]

        resolver = lambda _symbol, _row: MarketIdentityEvidence(  # noqa: E731
            "XNAS", "USD", "USD", "test", "i" * 64
        )
        return ReconstructionRosterCaptureService(
            repo,
            (lambda: payloads[0], lambda: payloads[1], last),
            resolver,
            id_generator=lambda: next(ids),
            clock=lambda: NOW,
        )

    left = make_service("left")
    right = make_service("right")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda service: service.capture("race", aliases), (left, right))
        )
    assert results[0].roster_digest == results[1].roster_digest


def test_concurrent_capture_rejects_different_resolved_market_evidence(
    tmp_path,
) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    aliases = SecurityAliasManifestV1.build((), created_at=NOW)
    barrier = Barrier(2)
    payloads = (
        _payload(
            RosterSource.DATAHUB_SP500,
            [{"symbol": "AAPL", "name": "Apple", "sector": "Technology"}],
        ),
        _payload(
            RosterSource.TRADINGVIEW_US,
            [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"}],
        ),
        _payload(
            RosterSource.TRADINGVIEW_UK,
            [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}],
        ),
    )

    def service_for(mic: str, prefix: str) -> ReconstructionRosterCaptureService:
        ids = iter((f"{prefix}-1", f"{prefix}-2", f"{prefix}-3"))

        def last() -> RosterSourcePayloadV1:
            barrier.wait()
            return payloads[2]

        resolver = lambda _symbol, _row: MarketIdentityEvidence(  # noqa: E731
            mic, "USD", "USD", "test", f"{prefix}" * 16
        )
        return ReconstructionRosterCaptureService(
            repo,
            (lambda: payloads[0], lambda: payloads[1], last),
            resolver,
            id_generator=lambda: next(ids),
            clock=lambda: NOW,
        )

    left = service_for("XNAS", "a")
    right = service_for("XNYS", "b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.capture, "race", aliases) for service in (left, right)
        ]
        outcomes = [future.exception() for future in futures]

    assert sum(outcome is None for outcome in outcomes) == 1
    failure = next(outcome for outcome in outcomes if outcome is not None)
    assert isinstance(failure, RosterCaptureError)
    assert failure.code == "integrity_error"
