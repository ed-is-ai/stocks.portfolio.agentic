"""Strict captured-roster policy for historical scan reconstruction."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from importlib.metadata import version
import json
import sqlite3
from typing import Any, Protocol
from uuid import uuid4

import yfinance as yf

from app.repositories.backtest_repo import BacktestRepository, RosterCaptureCommit
from app.integrations.tv_screener import (
    TradingViewRosterEvidence,
    fetch_tv_screener_roster_evidence,
)
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.historical_price_evidence import canonical_provider_metadata
from app.services.backtest.security_identity import (
    AliasEntryV1,
    SecurityAliasManifestV1,
    SecurityAliasResolver,
    SecurityIdentityRegistryV1,
    SecurityIdentityV1,
    normalize_symbol,
)
from app.services.backtest.strategy_job import WorkerLeaseFenceV1
from app.services.backtest.trading_calendar import TradingCalendar

ROSTER_POLICY_VERSION = "ReconstructionRosterPolicyV1"
ROSTER_MANIFEST_VERSION = "ReconstructionRosterManifestV1"
DATAHUB_SP500_SOURCE_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)
SURVIVORSHIP_WARNING = (
    "Survivorship-biased reconstruction; not a point-in-time market universe."
)


class RosterCaptureError(ValueError):
    """A required current roster or market identity cannot be trusted."""

    def __init__(self, message: str, *, code: str = "provider_contract_error"):
        super().__init__(message)
        self.code = code


class RosterSource(StrEnum):
    DATAHUB_SP500 = "datahub_sp500"
    TRADINGVIEW_US = "tradingview_us"
    TRADINGVIEW_UK = "tradingview_uk"


REQUIRED_SOURCE_ORDER = (
    RosterSource.DATAHUB_SP500,
    RosterSource.TRADINGVIEW_US,
    RosterSource.TRADINGVIEW_UK,
)


@dataclass(frozen=True)
class MarketIdentityEvidence:
    mic: str
    currency: str
    quote_unit: str
    evidence_source: str
    evidence_digest: str


@dataclass(frozen=True)
class RosterSourcePayloadV1:
    source: RosterSource
    rows: tuple[Mapping[str, object], ...]
    retrieved_at: datetime
    source_version: str
    package_version: str
    config_version: str
    payload_digest: str

    @classmethod
    def build(
        cls,
        *,
        source: RosterSource,
        rows: Sequence[Mapping[str, object]],
        retrieved_at: datetime,
        source_version: str,
        package_version: str,
        config_version: str,
    ) -> RosterSourcePayloadV1:
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("retrieval timestamp must be timezone-aware")
        original_rows = tuple(dict(row) for row in rows)
        canonical_rows = tuple(
            sorted(original_rows, key=lambda row: canonical_json(row))
        )
        payload_digest = manifest_digest(
            {
                "schema_version": "RosterSourcePayloadV1",
                "source": source,
                "source_version": source_version,
                "package_version": package_version,
                "config_version": config_version,
                "rows": canonical_rows,
            }
        )
        return cls(
            source,
            original_rows,
            retrieved_at,
            source_version,
            package_version,
            config_version,
            payload_digest,
        )

    @property
    def original_payload_json(self) -> str:
        return canonical_json(self.rows)


@dataclass(frozen=True)
class NormalizedRosterMemberV1:
    mic: str
    calendar: str
    provider_symbol: str
    currency: str
    quote_unit: str
    source_memberships: tuple[str, ...]
    source_evidence_digests: tuple[str, ...]
    identity_evidence: tuple[MarketIdentityEvidence, ...]
    evidence_digest: str


IdentityResolver = Callable[[str, dict[str, object]], MarketIdentityEvidence]


class ReconstructionRosterPolicyV1:
    """Normalize the exact configured current scanner-roster union."""

    version = ROSTER_POLICY_VERSION

    def __init__(
        self,
        calendar: TradingCalendar | None = None,
        provider_symbol_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._calendar = calendar or TradingCalendar()
        self._provider_symbol_aliases = dict(provider_symbol_aliases or {})

    def normalize(
        self,
        payloads: Sequence[RosterSourcePayloadV1],
        datahub_identity_resolver: IdentityResolver,
    ) -> tuple[NormalizedRosterMemberV1, ...]:
        if tuple(payload.source for payload in payloads) != REQUIRED_SOURCE_ORDER:
            raise RosterCaptureError("roster payloads are not in fixed source order")
        for payload in payloads:
            if not payload.rows:
                raise RosterCaptureError(
                    f"required roster source is empty: {payload.source}"
                )

        accumulated: dict[tuple[str, str], dict[str, Any]] = {}
        seen_per_source: dict[RosterSource, set[tuple[str, str]]] = {}
        for payload in payloads:
            source_seen = seen_per_source.setdefault(payload.source, set())
            for raw_row in payload.rows:
                row = dict(raw_row)
                if row.get("source_class") in {
                    "institutional",
                    "email",
                    "portfolio_only",
                }:
                    continue
                source_symbol = row.get("symbol")
                if not isinstance(source_symbol, str):
                    raise RosterCaptureError(f"{payload.source} row has no symbol")
                normalized_source = normalize_symbol(source_symbol)
                if payload.source is RosterSource.DATAHUB_SP500:
                    provider_symbol = self._provider_symbol_aliases.get(
                        normalized_source, normalized_source
                    )
                    identity = datahub_identity_resolver(provider_symbol, row)
                else:
                    provider_symbol, identity = self._tradingview_identity(
                        payload.source, normalized_source, row
                    )
                self._validate_identity(identity)
                key = (identity.mic, provider_symbol)
                if key in source_seen:
                    raise RosterCaptureError(
                        f"duplicate member inside {payload.source}: {identity.mic}/{provider_symbol}"
                    )
                source_seen.add(key)
                evidence_digest = manifest_digest(
                    {
                        "source": payload.source,
                        "source_payload_digest": payload.payload_digest,
                        "source_symbol": normalized_source,
                        "provider_symbol": provider_symbol,
                        "market_identity": identity,
                        "row": row,
                    }
                )
                existing = accumulated.get(key)
                if existing is None:
                    accumulated[key] = {
                        "mic": identity.mic,
                        "calendar": self._calendar.calendar_name(identity.mic),
                        "provider_symbol": provider_symbol,
                        "currency": identity.currency,
                        "quote_unit": identity.quote_unit,
                        "memberships": [payload.source.value],
                        "evidence": [evidence_digest],
                        "identity_evidence": [identity],
                    }
                    continue
                if (
                    existing["currency"] != identity.currency
                    or existing["quote_unit"] != identity.quote_unit
                ):
                    raise RosterCaptureError(
                        f"conflicting currency identity for {identity.mic}/{provider_symbol}"
                    )
                existing["memberships"].append(payload.source.value)
                existing["evidence"].append(evidence_digest)
                existing["identity_evidence"].append(identity)

        members: list[NormalizedRosterMemberV1] = []
        for key in sorted(accumulated):
            item = accumulated[key]
            evidence = tuple(item["evidence"])
            members.append(
                NormalizedRosterMemberV1(
                    mic=item["mic"],
                    calendar=item["calendar"],
                    provider_symbol=item["provider_symbol"],
                    currency=item["currency"],
                    quote_unit=item["quote_unit"],
                    source_memberships=tuple(item["memberships"]),
                    source_evidence_digests=evidence,
                    identity_evidence=tuple(item["identity_evidence"]),
                    evidence_digest=manifest_digest(evidence),
                )
            )
        return tuple(members)

    def _tradingview_identity(
        self,
        source: RosterSource,
        source_symbol: str,
        row: dict[str, object],
    ) -> tuple[str, MarketIdentityEvidence]:
        prefix, separator, bare_symbol = source_symbol.partition(":")
        exchange = row.get("exchange")
        if not separator or not isinstance(exchange, str):
            raise RosterCaptureError(f"{source} row lacks exchange-qualified symbol")
        exchange = normalize_symbol(exchange)
        if prefix != exchange:
            raise RosterCaptureError(f"{source} symbol/exchange conflict")
        expected = {
            "NASDAQ": ("XNAS", "USD", "USD"),
            "NYSE": ("XNYS", "USD", "USD"),
            "LSE": ("XLON", "GBP", "GBp"),
        }.get(exchange)
        if expected is None:
            raise RosterCaptureError(f"unsupported TradingView exchange: {exchange}")
        if source is RosterSource.TRADINGVIEW_UK and exchange != "LSE":
            raise RosterCaptureError("TradingView UK returned a non-LSE member")
        if source is RosterSource.TRADINGVIEW_US and exchange not in {"NASDAQ", "NYSE"}:
            raise RosterCaptureError("TradingView US returned an unsupported member")
        raw_currency = row.get("currency")
        raw_unit = row.get("quote_unit", raw_currency)
        accepted_currency = expected[1]
        if exchange == "LSE" and raw_currency == "GBp":
            raw_currency = "GBP"
        if raw_currency != accepted_currency or raw_unit != expected[2]:
            raise RosterCaptureError(f"conflicting currency for {source_symbol}")
        provider_symbol = normalize_symbol(bare_symbol)
        if exchange == "LSE" and not provider_symbol.endswith(".L"):
            provider_symbol = f"{provider_symbol}.L"
        identity = MarketIdentityEvidence(
            mic=expected[0],
            currency=expected[1],
            quote_unit=expected[2],
            evidence_source=source.value,
            evidence_digest=manifest_digest(row),
        )
        return provider_symbol, identity

    def _validate_identity(self, identity: MarketIdentityEvidence) -> None:
        try:
            self._calendar.calendar_name(identity.mic)
        except ValueError as exc:
            raise RosterCaptureError(str(exc), code="calendar_error") from exc
        expected_currency = (
            "USD" if identity.mic in {"BATS", "XNAS", "XNYS"} else "GBP"
        )
        expected_units = {"USD"} if expected_currency == "USD" else {"GBP", "GBp"}
        if (
            identity.currency != expected_currency
            or identity.quote_unit not in expected_units
        ):
            raise RosterCaptureError(
                f"conflicting MIC/currency identity: {identity.mic}"
            )
        if not identity.evidence_source or not identity.evidence_digest:
            raise RosterCaptureError("market identity evidence is missing")


@dataclass(frozen=True)
class RosterProvenanceV1:
    roster_captured_at: datetime
    universe_basis: str = "captured_configured_roster"
    point_in_time_universe: bool = False
    survivorship_bias: str = "known"
    warning: str = SURVIVORSHIP_WARNING


class DataHubRosterSourceAdapter:
    """Turn the existing client's strict live DataHub seam into roster evidence."""

    def __init__(
        self,
        fetch_live: Callable[[], list[dict[str, object]] | None],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        source_version: str = DATAHUB_SP500_SOURCE_URL,
    ) -> None:
        self._fetch_live = fetch_live
        self._clock = clock
        self._source_version = source_version

    def __call__(self) -> RosterSourcePayloadV1:
        rows = self._fetch_live()
        if not rows:
            raise RosterCaptureError(
                "current DataHub S&P 500 roster unavailable",
                code="provider_unavailable",
            )
        symbols: set[str] = set()
        for row in rows:
            if not all(
                isinstance(row.get(field), str) and str(row[field]).strip()
                for field in ("symbol", "name", "sector")
            ):
                raise RosterCaptureError("DataHub roster row is malformed")
            symbol = normalize_symbol(str(row["symbol"]))
            if symbol in symbols:
                raise RosterCaptureError(f"duplicate DataHub roster symbol: {symbol}")
            symbols.add(symbol)
        return RosterSourcePayloadV1.build(
            source=RosterSource.DATAHUB_SP500,
            rows=rows,
            retrieved_at=self._clock(),
            source_version=self._source_version,
            package_version=version("requests"),
            config_version=ROSTER_POLICY_VERSION,
        )


class TradingViewRosterSourceAdapter:
    """Require successful evidence-rich output from the existing TV query."""

    def __init__(
        self,
        market: str,
        *,
        fetch: Callable[
            ..., TradingViewRosterEvidence
        ] = fetch_tv_screener_roster_evidence,
    ) -> None:
        normalized = market.strip().upper()
        if normalized not in {"US", "UK"}:
            raise ValueError("market must be US or UK")
        self._market = normalized
        self._fetch = fetch

    def __call__(self) -> RosterSourcePayloadV1:
        evidence = self._fetch(market=self._market)
        if (
            evidence.result.status != "ok"
            or not evidence.rows
            or len(evidence.rows) != len(evidence.result.tickers)
        ):
            raise RosterCaptureError(
                f"current TradingView {self._market} roster unavailable",
                code="provider_unavailable",
            )
        health = evidence.result.health
        if health.completed_at is None:
            raise RosterCaptureError("TradingView retrieval timestamp is missing")
        source = (
            RosterSource.TRADINGVIEW_US
            if self._market == "US"
            else RosterSource.TRADINGVIEW_UK
        )
        return RosterSourcePayloadV1.build(
            source=source,
            rows=evidence.rows,
            retrieved_at=health.completed_at,
            source_version=f"TradingViewStage2ScreenV1:{self._market}",
            package_version=version("tradingview-screener"),
            config_version=ROSTER_POLICY_VERSION,
        )


class CurrentIdentityTicker(Protocol):
    def get_history_metadata(self, repair: bool = False) -> Mapping[str, object]: ...


class YFinanceMarketIdentityResolver:
    """Resolve DataHub-only symbols from explicit current provider metadata."""

    _EXCHANGE_TO_MIC = {
        "NMS": "XNAS",
        "NGM": "XNAS",
        "NCM": "XNAS",
        "NASDAQ": "XNAS",
        "NYQ": "XNYS",
        "NYSE": "XNYS",
    }

    def __init__(
        self, ticker_factory: Callable[[str], CurrentIdentityTicker] = yf.Ticker
    ) -> None:
        self._ticker_factory = ticker_factory

    def __call__(
        self, symbol: str, _source_row: dict[str, object]
    ) -> MarketIdentityEvidence:
        try:
            metadata = dict(
                self._ticker_factory(symbol).get_history_metadata(repair=False)
            )
        except Exception as exc:
            raise RosterCaptureError(
                f"market identity metadata unavailable for {symbol}",
                code="provider_unavailable",
            ) from exc
        exchange = metadata.get("exchangeName") or metadata.get("exchange")
        provider_unit = metadata.get("currency")
        if not isinstance(exchange, str) or not isinstance(provider_unit, str):
            raise RosterCaptureError(
                f"market identity metadata missing for {symbol}",
                code="identity_ambiguous",
            )
        mic = self._EXCHANGE_TO_MIC.get(exchange.strip().upper())
        if mic is None or provider_unit.strip().upper() != "USD":
            raise RosterCaptureError(
                f"unsupported market identity for {symbol}",
                code="identity_ambiguous",
            )
        return MarketIdentityEvidence(
            mic=mic,
            currency="USD",
            quote_unit="USD",
            evidence_source="yfinance_current_metadata",
            evidence_digest=manifest_digest(canonical_provider_metadata(metadata)),
        )


def _fetch_tradingview_us_identity_rows() -> tuple[dict[str, object], ...]:
    """Fetch one bounded current US identity batch from TradingView."""
    from tradingview_screener import Query, col  # type: ignore[import]

    _count, frame = (
        Query()
        .select("name", "exchange", "currency", "market_cap_basic")
        .where(col("type") == "stock", col("market_cap_basic") > 500_000_000)
        .order_by("market_cap_basic", ascending=False)
        .limit(10_000)
        .get_scanner_data()
    )
    return tuple(
        {
            "symbol": symbol,
            "exchange": exchange,
            "currency": currency,
        }
        for symbol, exchange, currency in zip(
            frame["name"], frame["exchange"], frame["currency"], strict=True
        )
    )


class TradingViewBatchMarketIdentityResolver:
    """Resolve DataHub identities from one explicit TradingView batch."""

    _EXCHANGE_TO_MIC = {
        "NASDAQ": "XNAS",
        "NYSE": "XNYS",
        "CBOE": "BATS",
    }

    def __init__(
        self,
        fetch: Callable[[], Sequence[Mapping[str, object]]] = (
            _fetch_tradingview_us_identity_rows
        ),
        provider_symbol_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._fetch = fetch
        self._provider_symbol_aliases = dict(provider_symbol_aliases or {})
        self._identities: dict[str, MarketIdentityEvidence] | None = None

    def __call__(
        self, symbol: str, _source_row: dict[str, object]
    ) -> MarketIdentityEvidence:
        if self._identities is None:
            self._identities = self._load()
        identity = self._identities.get(symbol)
        if identity is None:
            raise RosterCaptureError(
                f"market identity metadata unavailable for {symbol}",
                code="identity_ambiguous",
            )
        return identity

    def _load(self) -> dict[str, MarketIdentityEvidence]:
        identities: dict[str, MarketIdentityEvidence] = {}
        for raw in self._fetch():
            source_symbol = raw.get("symbol")
            exchange = raw.get("exchange")
            currency = raw.get("currency")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (source_symbol, exchange, currency)
            ):
                continue
            assert isinstance(source_symbol, str)
            assert isinstance(exchange, str)
            assert isinstance(currency, str)
            provider_symbol = normalize_symbol(source_symbol)
            provider_symbol = self._provider_symbol_aliases.get(
                provider_symbol, provider_symbol
            )
            mic = self._EXCHANGE_TO_MIC.get(exchange.strip().upper())
            if mic is None or currency.strip().upper() != "USD":
                continue
            identity = MarketIdentityEvidence(
                mic=mic,
                currency="USD",
                quote_unit="USD",
                evidence_source="tradingview_current_metadata",
                evidence_digest=manifest_digest(
                    {
                        "provider": "tradingview-screener",
                        "query": "DataHubIdentityBatchV1",
                        "symbol": provider_symbol,
                        "exchange": exchange.strip().upper(),
                        "currency": currency.strip().upper(),
                    }
                ),
            )
            existing = identities.get(provider_symbol)
            if existing is not None and existing != identity:
                raise RosterCaptureError(
                    f"conflicting market identity metadata for {provider_symbol}",
                    code="identity_ambiguous",
                )
            identities[provider_symbol] = identity
        return identities


@dataclass(frozen=True)
class CapturedRosterMemberV1:
    security_id: str
    mic: str
    calendar: str
    provider_symbol: str
    currency: str
    quote_unit: str
    source_memberships: tuple[str, ...]
    identity_evidence: tuple[MarketIdentityEvidence, ...]
    evidence_digest: str


@dataclass(frozen=True)
class CapturedRosterV1:
    roster_digest: str
    canonical_manifest_json: str
    members: tuple[CapturedRosterMemberV1, ...]

    @classmethod
    def from_json(cls, roster_digest: str, value: str) -> CapturedRosterV1:
        payload = json.loads(value)
        members = tuple(
            CapturedRosterMemberV1(
                security_id=item["security_id"],
                mic=item["mic"],
                calendar=item["calendar"],
                provider_symbol=item["provider_symbol"],
                currency=item["currency"],
                quote_unit=item["quote_unit"],
                source_memberships=tuple(item["source_memberships"]),
                identity_evidence=tuple(
                    MarketIdentityEvidence(**evidence)
                    for evidence in item["identity_evidence"]
                ),
                evidence_digest=item["evidence_digest"],
            )
            for item in payload["members"]
        )
        return cls(roster_digest, value, members)


SourceFetcher = Callable[[], RosterSourcePayloadV1]


def _capture_content_digest(manifest: Mapping[str, object]) -> str:
    """Identify captured inputs without generated IDs or capture timestamps."""
    raw_sources = manifest.get("sources", ())
    raw_members = manifest.get("members", ())
    if not isinstance(raw_sources, Sequence) or not isinstance(raw_members, Sequence):
        raise RosterCaptureError(
            "roster manifest content is malformed", code="integrity_error"
        )
    sources = []
    for source in raw_sources:
        if not isinstance(source, Mapping):
            raise RosterCaptureError(
                "roster source evidence is malformed", code="integrity_error"
            )
        sources.append(
            {
                "source": source.get("source"),
                "payload_digest": source.get("payload_digest"),
            }
        )
    members = []
    for member in raw_members:
        if isinstance(member, Mapping):
            values = member
        else:
            values = {
                "mic": getattr(member, "mic", None),
                "calendar": getattr(member, "calendar", None),
                "provider_symbol": getattr(member, "provider_symbol", None),
                "currency": getattr(member, "currency", None),
                "quote_unit": getattr(member, "quote_unit", None),
                "source_memberships": getattr(member, "source_memberships", None),
                "identity_evidence": getattr(member, "identity_evidence", None),
                "evidence_digest": getattr(member, "evidence_digest", None),
            }
        members.append(
            {
                field: values.get(field)
                for field in (
                    "mic",
                    "calendar",
                    "provider_symbol",
                    "currency",
                    "quote_unit",
                    "source_memberships",
                    "identity_evidence",
                    "evidence_digest",
                )
            }
        )
    return manifest_digest(
        {
            "policy_version": manifest.get("policy_version"),
            "input_alias_revision": manifest.get("input_alias_revision"),
            "sources": sources,
            "members": members,
        }
    )


class ReconstructionRosterCaptureService:
    """Capture and atomically bind one immutable roster per profile lineage."""

    def __init__(
        self,
        repository: BacktestRepository,
        source_fetchers: tuple[SourceFetcher, SourceFetcher, SourceFetcher],
        datahub_identity_resolver: IdentityResolver,
        *,
        id_generator: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        policy: ReconstructionRosterPolicyV1 | None = None,
    ) -> None:
        self._repository = repository
        self._source_fetchers = source_fetchers
        self._identity_resolver = datahub_identity_resolver
        self._id_generator = id_generator
        self._clock = clock
        self._policy = policy or ReconstructionRosterPolicyV1()

    def capture(
        self,
        lineage_id: str,
        alias_manifest: SecurityAliasManifestV1,
        *,
        job_claim: tuple[str, str, int] | None = None,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> CapturedRosterV1:
        if not lineage_id.strip():
            raise ValueError("lineage_id is required")
        existing_digest = self._repository.roster_digest_for_lineage(lineage_id)
        if existing_digest is not None:
            existing_json = self._repository.roster_manifest_json(existing_digest)
            if existing_json is None:
                raise RosterCaptureError(
                    "lineage roster evidence is missing", code="integrity_error"
                )
            return CapturedRosterV1.from_json(existing_digest, existing_json)

        payloads: list[RosterSourcePayloadV1] = []
        for expected, fetch in zip(
            REQUIRED_SOURCE_ORDER, self._source_fetchers, strict=True
        ):
            payload = fetch()
            if payload.source is not expected:
                raise RosterCaptureError(
                    f"expected {expected.value}, received {payload.source.value}"
                )
            payloads.append(payload)
        normalized = self._policy.normalize(payloads, self._identity_resolver)
        captured_at = self._clock()
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("capture clock must return a timezone-aware instant")

        existing_identities = tuple(
            SecurityIdentityV1(security_id, mic, symbol, evidence_digest)
            for security_id, mic, symbol, evidence_digest in self._repository.identity_rows()
        )
        exact = {
            (identity.mic, identity.provider_symbol): identity
            for identity in existing_identities
        }
        by_id = {identity.security_id: identity for identity in existing_identities}
        aliases = SecurityAliasResolver(alias_manifest)
        identities = list(existing_identities)
        captured_members: list[CapturedRosterMemberV1] = []
        for member in normalized:
            identity = exact.get((member.mic, member.provider_symbol))
            if identity is None:
                alias_id = aliases.resolve(
                    "yfinance", member.mic, member.provider_symbol, captured_at.date()
                )
                if alias_id is not None:
                    identity = by_id.get(alias_id)
                    if identity is None:
                        raise RosterCaptureError(
                            "alias references an unknown security",
                            code="integrity_error",
                        )
                else:
                    identity = SecurityIdentityV1(
                        security_id=self._id_generator(),
                        mic=member.mic,
                        provider_symbol=member.provider_symbol,
                        evidence_digest=member.evidence_digest,
                    )
                    identities.append(identity)
                    exact[(identity.mic, identity.provider_symbol)] = identity
                    by_id[identity.security_id] = identity
            captured_members.append(
                CapturedRosterMemberV1(
                    security_id=identity.security_id,
                    mic=member.mic,
                    calendar=member.calendar,
                    provider_symbol=member.provider_symbol,
                    currency=member.currency,
                    quote_unit=member.quote_unit,
                    source_memberships=member.source_memberships,
                    identity_evidence=member.identity_evidence,
                    evidence_digest=member.evidence_digest,
                )
            )

        direct_aliases = {
            (entry.provider, entry.mic, entry.observed_symbol): entry
            for entry in alias_manifest.entries
        }
        augmented_entries = list(alias_manifest.entries)
        for identity in identities:
            key = ("yfinance", identity.mic, identity.provider_symbol)
            existing_alias = direct_aliases.get(key)
            if existing_alias is not None:
                if existing_alias.security_id != identity.security_id:
                    raise RosterCaptureError(
                        "provider alias conflicts with captured identity",
                        code="identity_ambiguous",
                    )
                continue
            augmented_entries.append(
                AliasEntryV1(
                    security_id=identity.security_id,
                    provider="yfinance",
                    mic=identity.mic,
                    observed_symbol=identity.provider_symbol,
                    effective_from=None,
                    effective_to=None,
                    evidence_source="captured_market_identity",
                    evidence_digest=identity.evidence_digest,
                    provenance="provider_evidence",
                )
            )
        committed_alias_manifest = SecurityAliasManifestV1.build(
            tuple(augmented_entries), created_at=captured_at
        )

        registry = SecurityIdentityRegistryV1.build(
            tuple(identities), created_at=captured_at
        )
        provenance = RosterProvenanceV1(captured_at)
        manifest_body = {
            "schema_version": ROSTER_MANIFEST_VERSION,
            "policy_version": ROSTER_POLICY_VERSION,
            "captured_at": captured_at,
            "identity_registry_revision": registry.revision,
            "alias_revision": committed_alias_manifest.revision,
            "input_alias_revision": alias_manifest.revision,
            "expected_count": len(captured_members),
            "sources": [
                {
                    "source": payload.source,
                    "payload_digest": payload.payload_digest,
                    "original_payload": payload.rows,
                    "retrieved_at": payload.retrieved_at,
                    "source_version": payload.source_version,
                    "package_version": payload.package_version,
                    "config_version": payload.config_version,
                }
                for payload in payloads
            ],
            "members": captured_members,
            "provenance": provenance,
        }
        roster_digest = manifest_digest(manifest_body)
        manifest_json = canonical_json(manifest_body)
        commit = RosterCaptureCommit(
            lineage_id=lineage_id,
            roster_digest=roster_digest,
            roster_manifest_json=manifest_json,
            policy_version=ROSTER_POLICY_VERSION,
            identity_registry_revision=registry.revision,
            identity_registry_json=canonical_json(
                {
                    "schema_version": registry.schema_version,
                    "revision": registry.revision,
                    "evidence_digest": registry.evidence_digest,
                    "identities": registry.identities,
                }
            ),
            identity_evidence_digest=registry.evidence_digest,
            alias_revision=committed_alias_manifest.revision,
            alias_manifest_json=canonical_json(
                {
                    "schema_version": committed_alias_manifest.schema_version,
                    "revision": committed_alias_manifest.revision,
                    "evidence_digest": committed_alias_manifest.evidence_digest,
                    "entries": committed_alias_manifest.entries,
                }
            ),
            alias_evidence_digest=committed_alias_manifest.evidence_digest,
            captured_at=captured_at.astimezone(timezone.utc).isoformat(),
            identities=tuple(
                (
                    identity.security_id,
                    identity.mic,
                    identity.provider_symbol,
                    identity.evidence_digest,
                )
                for identity in registry.identities
            ),
            aliases=tuple(
                (
                    entry.security_id,
                    entry.provider,
                    entry.mic,
                    entry.observed_symbol,
                    None
                    if entry.effective_from is None
                    else entry.effective_from.isoformat(),
                    None
                    if entry.effective_to is None
                    else entry.effective_to.isoformat(),
                    entry.evidence_source,
                    entry.evidence_digest,
                    entry.provenance,
                )
                for entry in committed_alias_manifest.entries
            ),
            sources=tuple(
                (
                    payload.source.value,
                    payload.payload_digest,
                    payload.original_payload_json,
                    payload.retrieved_at.astimezone(timezone.utc).isoformat(),
                )
                for payload in payloads
            ),
            members=tuple(
                (
                    member.security_id,
                    member.mic,
                    member.provider_symbol,
                    member.currency,
                    canonical_json(member.source_memberships),
                    canonical_json(member.identity_evidence),
                    member.evidence_digest,
                )
                for member in captured_members
            ),
        )
        try:
            self._repository.commit_roster_capture(
                commit, job_claim=job_claim, lease=lease
            )
        except sqlite3.IntegrityError as exc:
            winner_digest = self._repository.roster_digest_for_lineage(lineage_id)
            winner_json = (
                None
                if winner_digest is None
                else self._repository.roster_manifest_json(winner_digest)
            )
            if winner_digest is None or winner_json is None:
                raise
            winner = json.loads(winner_json)
            if _capture_content_digest(winner) != _capture_content_digest(
                manifest_body
            ):
                raise RosterCaptureError(
                    "concurrent capture conflicts with the lineage roster",
                    code="integrity_error",
                ) from exc
            return CapturedRosterV1.from_json(winner_digest, winner_json)
        return CapturedRosterV1(roster_digest, manifest_json, tuple(captured_members))
