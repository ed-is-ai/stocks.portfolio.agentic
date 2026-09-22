"""Reference-only benchmark evidence acquisition."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import math
from typing import Callable

from app.repositories.backtest_repo import (
    BacktestRepository,
    ReferenceIdentityRegistrationV1,
)
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_price_evidence import (
    CANONICAL_EXCHANGE_SESSIONS_POLICY,
    HistoricalEvidencePayload,
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.security_identity import AliasEntryV1, SecurityIdentityV1
from app.services.backtest.strategy_job import RegimeBenchmarkPinV1
from app.services.backtest.trading_calendar import TradingCalendar


class BenchmarkEvidenceError(ValueError):
    """Reference evidence cannot satisfy its bounded historical contract."""


@dataclass(frozen=True)
class BenchmarkEvidenceResultV1:
    registration: ReferenceIdentityRegistrationV1
    request: HistoricalEvidenceRequest
    data_revision: str
    price_revision: str
    action_revision: str
    session_policy: str
    calendar_mic: str
    calendar_session_table_digest: str
    price_plane_policy_version: str
    first_decision_session: date
    warmup_sessions: tuple[date, ...]

    def reference_pin(self) -> RegimeBenchmarkPinV1:
        """Return the immutable manifest pin for this acquired revision."""
        return RegimeBenchmarkPinV1(
            security_id=self.registration.identity.security_id,
            identity_registry_revision=self.registration.identity_registry_revision,
            alias_revision=self.registration.alias_revision,
            price_revision=self.price_revision,
            action_revision=self.action_revision,
            evidence_digest=self.data_revision,
            request_start=self.request.start,
            request_end=self.request.end,
            session_policy=self.session_policy,
            calendar_mic=self.calendar_mic,
            calendar_session_table_digest=self.calendar_session_table_digest,
            price_plane_policy_version=self.price_plane_policy_version,
        )


class BenchmarkEvidenceService:
    """Register and acquire one non-tradable benchmark evidence revision."""

    def __init__(
        self,
        *,
        backtest_repository: BacktestRepository,
        price_repository: HistoricalPriceRepository,
        adapter: YFinanceHistoricalEvidenceAdapter | None = None,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backtest = backtest_repository
        self._prices = price_repository
        self._adapter = adapter or YFinanceHistoricalEvidenceAdapter()
        self._calendar = calendar or TradingCalendar()
        self._clock = clock

    def acquire(
        self,
        identity: SecurityIdentityV1,
        alias: AliasEntryV1,
        *,
        start: date,
        end: date,
        ma_length: int = 200,
    ) -> BenchmarkEvidenceResultV1:
        if identity.mic != "ARCX" or alias.mic != "ARCX":
            raise BenchmarkEvidenceError("benchmark reference MIC must be ARCX")
        if identity.provider_symbol != "SPY":
            raise BenchmarkEvidenceError("benchmark reference must be SPY")
        if identity.provider_symbol != alias.observed_symbol:
            raise BenchmarkEvidenceError(
                "benchmark identity and alias symbols do not match"
            )
        if alias.provider != "yfinance":
            raise BenchmarkEvidenceError("benchmark evidence requires yfinance provenance")
        if start >= end:
            raise BenchmarkEvidenceError("benchmark interval must be non-empty")
        if (
            isinstance(ma_length, bool)
            or not isinstance(ma_length, int)
            or ma_length < 2
        ):
            raise BenchmarkEvidenceError("benchmark moving-average length is invalid")
        if start < date(1970, 1, 1) or end > date(2100, 12, 31):
            raise BenchmarkEvidenceError(
                "benchmark interval exceeds the canonical calendar authority"
            )

        decision_sessions = self._calendar.sessions_in_range("XNYS", start, end)
        if not decision_sessions:
            raise BenchmarkEvidenceError("benchmark interval has no decision sessions")
        first_decision = decision_sessions[0]
        last_decision = decision_sessions[-1]
        if (
            alias.effective_from is None
            or not alias.contains(first_decision)
            or not alias.contains(last_decision)
        ):
            raise BenchmarkEvidenceError(
                "benchmark alias does not prove continuity over the requested interval"
            )

        # The canonical exchange calendar is the authority for ARCX session
        # dates.  This lookup is bounded by the existing 1970 calendar table,
        # so no new calendar contract or MIC mapping is introduced.
        prior = self._calendar.sessions_in_range(
            "XNYS", date(1970, 1, 1), first_decision + timedelta(days=1)
        )
        if len(prior) < ma_length:
            raise BenchmarkEvidenceError("benchmark history lacks SMA warm-up")
        warmup_sessions = prior[-ma_length:]
        expected_sessions = self._calendar.sessions_in_range(
            "XNYS", warmup_sessions[0], end
        )
        request = HistoricalEvidenceRequest(
            security_id=identity.security_id,
            alias_revision=None,
            symbol=identity.provider_symbol,
            start=warmup_sessions[0],
            end=end,
            expected_sessions=expected_sessions,
            allowed_observed_symbols=(identity.provider_symbol,),
            expected_currency="USD",
            expected_quote_unit="USD",
            expected_timezone="America/New_York",
            allow_missing_prefix=False,
            canonical_exchange_sessions=True,
        )
        # Registration has its own immutable transaction and is intentionally
        # separate from later run-manifest evidence pinning.
        created_at = (
            self._clock() if self._clock is not None else datetime.now(timezone.utc)
        )
        registration = self._backtest.register_reference_identity(
            identity, alias, created_at=created_at
        )
        request = replace(request, alias_revision=registration.alias_revision)
        payload = self._adapter.fetch(request)
        self._validate_payload(payload, request)
        data_revision = self._prices.commit(payload)
        self._prices.verify(data_revision)
        return BenchmarkEvidenceResultV1(
            registration=registration,
            request=request,
            data_revision=data_revision,
            price_revision=data_revision,
            action_revision=data_revision,
            session_policy=CANONICAL_EXCHANGE_SESSIONS_POLICY,
            calendar_mic="XNYS",
            calendar_session_table_digest=self._calendar.session_table_digest(),
            price_plane_policy_version=PRICE_VOLUME_PLANE_VERSION,
            first_decision_session=first_decision,
            warmup_sessions=warmup_sessions,
        )

    def resolve_existing(
        self,
        security_id: str,
        *,
        start: date,
        end: date,
        ma_length: int = 200,
    ) -> RegimeBenchmarkPinV1:
        """Build a pin from already-acquired reference evidence, offline."""
        if isinstance(ma_length, bool) or not isinstance(ma_length, int) or ma_length < 2:
            raise BenchmarkEvidenceError("benchmark moving-average length is invalid")
        mic, symbol, _identity_evidence, identity_revision = (
            self._backtest.reference_identity_details(security_id)
        )
        if mic != "ARCX" or symbol != "SPY":
            raise BenchmarkEvidenceError("registered reference is not SPY on ARCX")
        alias_revision = self._backtest.reference_alias_revision(security_id)
        decision_sessions = self._calendar.sessions_in_range("XNYS", start, end)
        if not decision_sessions:
            raise BenchmarkEvidenceError("benchmark interval has no decision sessions")
        prior = self._calendar.sessions_in_range(
            "XNYS", date(1970, 1, 1), decision_sessions[0] + timedelta(days=1)
        )
        if len(prior) < ma_length:
            raise BenchmarkEvidenceError("benchmark history lacks SMA warm-up")
        request_start = prior[-ma_length]
        revision = self._prices.covering_revision(
            security_id=security_id,
            requested_symbol="SPY",
            start=request_start.isoformat(),
            end=end.isoformat(),
        )
        if revision is None:
            raise BenchmarkEvidenceError("registered benchmark evidence is unavailable")
        evidence = self._prices.verify(revision)
        expected_sessions = self._calendar.sessions_in_range("XNYS", request_start, end)
        observed_sessions = tuple(
            date.fromisoformat(str(row["session"])) for row in evidence.rows
        )
        if observed_sessions != expected_sessions or len(observed_sessions) < ma_length:
            raise BenchmarkEvidenceError("registered benchmark evidence is incomplete")
        if (
            evidence.security_id != security_id
            or evidence.alias_revision != alias_revision
            or evidence.provider != "yfinance"
            or evidence.requested_symbol != "SPY"
            or evidence.observed_symbol != "SPY"
            or evidence.currency != "USD"
            or evidence.quote_unit != "USD"
            or evidence.exchange_timezone != "America/New_York"
            or evidence.start != request_start.isoformat()
            or evidence.end != end.isoformat()
        ):
            raise BenchmarkEvidenceError("registered benchmark evidence identity mismatch")
        return RegimeBenchmarkPinV1(
            security_id=security_id,
            identity_registry_revision=identity_revision,
            alias_revision=alias_revision,
            price_revision=revision,
            action_revision=revision,
            evidence_digest=revision,
            request_start=request_start,
            request_end=end,
            session_policy=CANONICAL_EXCHANGE_SESSIONS_POLICY,
            calendar_mic="XNYS",
            calendar_session_table_digest=self._calendar.session_table_digest(),
            price_plane_policy_version=PRICE_VOLUME_PLANE_VERSION,
        )

    @staticmethod
    def _validate_payload(
        payload: HistoricalEvidencePayload, request: HistoricalEvidenceRequest
    ) -> None:
        try:
            sessions = tuple(date.fromisoformat(str(row["session"])) for row in payload.rows)
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkEvidenceError("benchmark session is invalid") from exc
        if sessions != request.expected_sessions:
            raise BenchmarkEvidenceError(
                "benchmark evidence is missing one or more canonical sessions"
            )
        if payload.security_id != request.security_id:
            raise BenchmarkEvidenceError("benchmark evidence identity mismatch")
        if payload.alias_revision != request.alias_revision:
            raise BenchmarkEvidenceError("benchmark evidence alias mismatch")
        if payload.provider != "yfinance":
            raise BenchmarkEvidenceError("benchmark evidence provider mismatch")
        if payload.requested_symbol != request.symbol:
            raise BenchmarkEvidenceError("benchmark evidence requested symbol mismatch")
        if payload.observed_symbol != request.symbol:
            raise BenchmarkEvidenceError("benchmark evidence observed symbol mismatch")
        if payload.currency != request.expected_currency:
            raise BenchmarkEvidenceError("benchmark evidence currency mismatch")
        if payload.quote_unit != request.expected_quote_unit:
            raise BenchmarkEvidenceError("benchmark evidence quote unit mismatch")
        if payload.exchange_timezone != request.expected_timezone:
            raise BenchmarkEvidenceError("benchmark evidence timezone mismatch")
        if payload.start != request.start.isoformat() or payload.end != request.end.isoformat():
            raise BenchmarkEvidenceError("benchmark evidence bounds mismatch")
        contract = payload.request_contract
        if (
            contract.get("start") != request.start.isoformat()
            or contract.get("end") != request.end.isoformat()
        ):
            raise BenchmarkEvidenceError("benchmark evidence request contract mismatch")
        closes = []
        for row in payload.rows:
            try:
                close = float.fromhex(str(row["close"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise BenchmarkEvidenceError("benchmark close is invalid") from exc
            if not math.isfinite(close) or close <= 0:
                raise BenchmarkEvidenceError("benchmark close is unusable")
            closes.append(close)
        if len(closes) < 2:
            raise BenchmarkEvidenceError("benchmark evidence has insufficient closes")
