"""AD-22 qualification for the pinned free historical-data contract."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
import yfinance as yf
from curl_cffi.requests import exceptions as curl_exceptions

from app.repositories.backtest_repo import BacktestRepository, QualificationResult
from app.services.backtest.canonical_manifest import (
    canonical_bytes as _shared_canonical_bytes,
    jsonable as _shared_jsonable,
    manifest_digest as _shared_manifest_digest,
)
from app.services.backtest.trading_calendar import TradingCalendar

REQUEST_CONTRACT_VERSION = "YFinanceDailyProviderNativeV1"
FIXTURE_CONTRACT_VERSION = "HistoricalSourceQualificationFixturesV1"
CANONICALIZER_VERSION = "HistoricalEvidenceCanonicalizerV1"
MANDATORY_FIXTURE_IDS = (
    "us_active",
    "lse_gbpence",
    "renamed_alias",
    "ordinary_split",
    "reverse_split",
    "dividend",
    "gbpusd_orientation",
)
MANDATORY_PROBE_IDS = ("us_active", "lse_active", "gbpusd")
_REQUIRED_COLUMNS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
    "Dividends",
    "Stock Splits",
)
_REQUIRED_VALUE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")


class FailureCode(StrEnum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_THROTTLED = "provider_throttled"
    PROVIDER_CONTRACT_ERROR = "provider_contract_error"
    REQUIRED_DATA_MISSING = "required_data_missing"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    CALENDAR_ERROR = "calendar_error"
    INTEGRITY_ERROR = "integrity_error"


class ProviderFailure(RuntimeError):
    def __init__(self, code: FailureCode, detail: str, *, retryable: bool = False):
        super().__init__(detail)
        self.code = code
        self.retryable = retryable


class TickerLike(Protocol):
    def history(self, **kwargs: object) -> pd.DataFrame: ...
    def get_history_metadata(self, repair: bool = False) -> Mapping[str, Any]: ...


class QualificationAdapter(Protocol):
    def fetch(
        self, definition: "ProbeDefinition"
    ) -> "HistoricalQualificationPayload": ...


@dataclass(frozen=True)
class HistoricalQualificationPayload:
    requested_symbol: str
    observed_symbol: str
    currency: str
    quote_unit: str
    quote_unit_scale: str
    exchange_timezone: str
    request_contract: Mapping[str, object]
    rows: tuple[Mapping[str, object], ...]
    response_metadata_digest: str
    content_digest: str
    acquired_at: str


@dataclass(frozen=True)
class ProbeDefinition:
    symbol: str
    start: date
    end: date
    expected_currency: str
    expected_quote_unit: str
    expected_timezone: str
    expected_sessions: tuple[date, ...]
    allowed_observed_symbols: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceCheck:
    passed: bool
    evidence_digest: str


@dataclass(frozen=True)
class QualificationContract:
    contract_digest: str
    source_versions_json: str
    fixture_digest: str
    probe_definition_digest: str


@dataclass(frozen=True)
class QualificationAvailability:
    available: bool
    reason: str | None = None


def _jsonable(value: Any) -> Any:
    return _shared_jsonable(value)


def _canonical_bytes(value: object) -> bytes:
    return _shared_canonical_bytes(value)


def _digest(value: object) -> str:
    return _shared_manifest_digest(value)


def _number(value: Any, *, nullable: bool = False) -> str | None:
    if value is None or pd.isna(value):
        if nullable:
            return None
        raise ProviderFailure(
            FailureCode.REQUIRED_DATA_MISSING,
            _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderFailure(
            FailureCode.PROVIDER_CONTRACT_ERROR,
            _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
        ) from exc
    if not math.isfinite(number):
        raise ProviderFailure(
            FailureCode.PROVIDER_CONTRACT_ERROR,
            _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
        )
    return number.hex()


def _safe_reason(code: FailureCode) -> str:
    return {
        FailureCode.PROVIDER_UNAVAILABLE: "Historical source unavailable",
        FailureCode.PROVIDER_THROTTLED: "Historical source is throttled",
        FailureCode.PROVIDER_CONTRACT_ERROR: "Historical source contract mismatch",
        FailureCode.REQUIRED_DATA_MISSING: "Required historical data is missing",
        FailureCode.IDENTITY_AMBIGUOUS: "Historical security identity is ambiguous",
        FailureCode.CALENDAR_ERROR: "Historical calendar qualification failed",
        FailureCode.INTEGRITY_ERROR: "Historical evidence integrity check failed",
    }[code]


def _classify_exception(exc: Exception) -> ProviderFailure:
    if isinstance(exc, ProviderFailure):
        return exc
    message = str(exc).lower()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429 or "throttl" in message or "rate limit" in message:
        return ProviderFailure(
            FailureCode.PROVIDER_THROTTLED,
            _safe_reason(FailureCode.PROVIDER_THROTTLED),
            retryable=True,
        )
    transport_types = (
        ConnectionError,
        TimeoutError,
        curl_exceptions.ConnectionError,
        curl_exceptions.Timeout,
    )
    if (
        isinstance(exc, transport_types)
        or status == 408
        or (isinstance(status, int) and 500 <= status <= 599)
    ):
        return ProviderFailure(
            FailureCode.PROVIDER_UNAVAILABLE,
            _safe_reason(FailureCode.PROVIDER_UNAVAILABLE),
            retryable=True,
        )
    return ProviderFailure(
        FailureCode.PROVIDER_CONTRACT_ERROR,
        _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
    )


def deterministic_jitter(request_key: str, retry_index: int) -> float:
    digest = hashlib.sha256(f"{request_key}:{retry_index}".encode()).digest()
    return (int.from_bytes(digest[:2], "big") % 251) / 1000


def _quote_contract(provider_unit: str) -> tuple[str, str, str]:
    if provider_unit == "GBp":
        return "GBP", "GBp", "0.01"
    if provider_unit in {"GBP", "USD"}:
        return provider_unit, provider_unit, "1"
    raise ProviderFailure(
        FailureCode.PROVIDER_CONTRACT_ERROR,
        _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
    )


def normalize_provider_response(
    *,
    requested_symbol: str,
    start: date,
    end: date,
    frame: pd.DataFrame,
    metadata: Mapping[str, Any],
    request: Mapping[str, object],
    expected_currency: str,
    expected_quote_unit: str,
    expected_timezone: str,
    expected_sessions: Sequence[date],
    allowed_observed_symbols: Sequence[str],
    acquired_at: datetime,
) -> HistoricalQualificationPayload:
    try:
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        missing = [
            column for column in _REQUIRED_COLUMNS if column not in frame.columns
        ]
        if missing:
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        if frame.index.tz is None or frame.index.has_duplicates:
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )
        observed_symbol = metadata.get("symbol")
        provider_unit = metadata.get("currency")
        exchange_timezone = metadata.get("exchangeTimezoneName")
        if not all(
            isinstance(value, str)
            for value in (observed_symbol, provider_unit, exchange_timezone)
        ):
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )
        assert isinstance(observed_symbol, str)
        assert isinstance(provider_unit, str)
        assert isinstance(exchange_timezone, str)
        if observed_symbol not in allowed_observed_symbols:
            raise ProviderFailure(
                FailureCode.IDENTITY_AMBIGUOUS,
                _safe_reason(FailureCode.IDENTITY_AMBIGUOUS),
            )
        currency, quote_unit, quote_scale = _quote_contract(provider_unit)
        if (
            currency != expected_currency
            or quote_unit != expected_quote_unit
            or exchange_timezone != expected_timezone
        ):
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )
        localized = frame.copy()
        localized.index = frame.index.tz_convert(exchange_timezone)
        localized = localized.sort_index()
        sessions = tuple(timestamp.date() for timestamp in localized.index)
        if sessions != tuple(expected_sessions) or any(
            session < start or session >= end for session in sessions
        ):
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        if localized[list(_REQUIRED_VALUE_COLUMNS)].isna().any(axis=None):
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        rows: list[Mapping[str, object]] = []
        for timestamp, row in localized.iterrows():
            volume = float(row["Volume"])
            dividend = float(row["Dividends"])
            split = float(row["Stock Splits"])
            if volume < 0 or dividend < 0 or split < 0:
                raise ProviderFailure(
                    FailureCode.PROVIDER_CONTRACT_ERROR,
                    _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
                )
            rows.append(
                {
                    "session": pd.Timestamp(str(timestamp)).date().isoformat(),
                    "open": _number(row["Open"]),
                    "high": _number(row["High"]),
                    "low": _number(row["Low"]),
                    "close": _number(row["Close"]),
                    "adj_close": _number(row["Adj Close"]),
                    "volume": _number(row["Volume"]),
                    "dividends": _number(row["Dividends"]),
                    "stock_splits": _number(row["Stock Splits"]),
                }
            )
        normalized_metadata = _jsonable(metadata)
        evidence = {
            "canonicalizer_version": CANONICALIZER_VERSION,
            "request_contract_version": REQUEST_CONTRACT_VERSION,
            "request": request,
            "requested_symbol": requested_symbol,
            "observed_symbol": observed_symbol,
            "currency": currency,
            "quote_unit": quote_unit,
            "quote_unit_scale": quote_scale,
            "exchange_timezone": exchange_timezone,
            "rows": rows,
        }
        return HistoricalQualificationPayload(
            requested_symbol=requested_symbol,
            observed_symbol=observed_symbol,
            currency=currency,
            quote_unit=quote_unit,
            quote_unit_scale=quote_scale,
            exchange_timezone=exchange_timezone,
            request_contract=request,
            rows=tuple(rows),
            response_metadata_digest=_digest(normalized_metadata),
            content_digest=_digest(evidence),
            acquired_at=acquired_at.astimezone(timezone.utc).isoformat(),
        )
    except ProviderFailure:
        raise
    except Exception as exc:
        raise ProviderFailure(
            FailureCode.PROVIDER_CONTRACT_ERROR,
            _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
        ) from exc


class YFinanceQualificationAdapter:
    """Qualification facade over the reusable provider-native AD-6 adapter."""

    def __init__(
        self,
        ticker_factory: Callable[[str], TickerLike] = yf.Ticker,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[str, int], float] = deterministic_jitter,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._ticker_factory = ticker_factory
        self._sleeper = sleeper
        self._jitter = jitter
        self._clock = clock

    def fetch(self, definition: ProbeDefinition) -> HistoricalQualificationPayload:
        # Lazy import avoids a module cycle: the shared adapter deliberately
        # reuses this module's established failure taxonomy.
        from app.services.backtest.historical_price_evidence import (
            HistoricalEvidenceRequest,
            YFinanceHistoricalEvidenceAdapter,
        )

        payload = YFinanceHistoricalEvidenceAdapter(
            self._ticker_factory,
            sleeper=self._sleeper,
            jitter=self._jitter,
            clock=self._clock,
        ).fetch(
            HistoricalEvidenceRequest(
                security_id=None,
                alias_revision=None,
                symbol=definition.symbol,
                start=definition.start,
                end=definition.end,
                expected_currency=definition.expected_currency,
                expected_quote_unit=definition.expected_quote_unit,
                expected_timezone=definition.expected_timezone,
                expected_sessions=definition.expected_sessions,
                allowed_observed_symbols=definition.allowed_observed_symbols,
            )
        )
        return HistoricalQualificationPayload(
            requested_symbol=payload.requested_symbol,
            observed_symbol=payload.observed_symbol,
            currency=payload.currency,
            quote_unit=payload.quote_unit,
            quote_unit_scale=payload.quote_unit_scale,
            exchange_timezone=payload.exchange_timezone,
            request_contract=payload.request_contract,
            rows=payload.rows,
            response_metadata_digest=payload.response_metadata_digest,
            content_digest=payload.data_revision,
            acquired_at=payload.acquired_at,
        )


def classify_missing_observation(requested: date, first_observation: date) -> str:
    if requested < first_observation:
        return "before_first_provider_observation"
    raise ProviderFailure(
        FailureCode.REQUIRED_DATA_MISSING,
        "Required observation is missing on or after first provider observation",
    )


def fx_rate_is_fresh(rate_date: date, use_date: date) -> bool:
    return 0 <= (use_date - rate_date).days <= 5


def _source_versions() -> dict[str, str]:
    return {
        "exchange_calendars": version("exchange_calendars"),
        "pandas": version("pandas"),
        "yfinance": version("yfinance"),
        "canonicalizer": CANONICALIZER_VERSION,
    }


def current_source_versions_json() -> str:
    """Return the canonical runtime source-version identity used by qualification."""
    return _canonical_bytes(_source_versions()).decode()


def build_qualification_contract(
    calendar_digest: str,
    fixture_payload: Mapping[str, object],
    probe_definitions: Mapping[str, ProbeDefinition],
) -> QualificationContract:
    fixture_digest = _digest(fixture_payload)
    probe_definition_digest = _digest(probe_definitions)
    source_versions_json = _canonical_bytes(_source_versions()).decode()
    contract_digest = _digest(
        {
            "sources": _source_versions(),
            "calendar_digest": calendar_digest,
            "request_contract": REQUEST_CONTRACT_VERSION,
            "fixture_contract": FIXTURE_CONTRACT_VERSION,
            "fixture_digest": fixture_digest,
            "probe_definition_digest": probe_definition_digest,
        }
    )
    return QualificationContract(
        contract_digest=contract_digest,
        source_versions_json=source_versions_json,
        fixture_digest=fixture_digest,
        probe_definition_digest=probe_definition_digest,
    )


class QualificationAvailabilityService:
    def __init__(self, repository: BacktestRepository) -> None:
        self._repository = repository

    def availability(
        self, contract: QualificationContract
    ) -> QualificationAvailability:
        result = self._repository.latest_qualification(contract.contract_digest)
        if (
            result is None
            or not result.passed
            or result.source_versions_json != contract.source_versions_json
            or result.fixture_digest != contract.fixture_digest
            or result.probe_definition_digest != contract.probe_definition_digest
        ):
            return QualificationAvailability(
                False, "Historical data contract is not qualified"
            )
        return QualificationAvailability(True)


class QualificationRecorder:
    """Persist verified aggregate evidence from the qualification runner."""

    def __init__(
        self,
        repository: BacktestRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._clock = clock

    def record(
        self,
        contract: QualificationContract,
        fixture_results: Mapping[str, EvidenceCheck],
        probe_results: Mapping[str, EvidenceCheck],
        failure: ProviderFailure | None = None,
    ) -> int:
        fixture_keys_valid = tuple(sorted(fixture_results)) == tuple(
            sorted(MANDATORY_FIXTURE_IDS)
        )
        probe_keys_valid = tuple(sorted(probe_results)) == tuple(
            sorted(MANDATORY_PROBE_IDS)
        )
        passed = (
            failure is None
            and fixture_keys_valid
            and probe_keys_valid
            and all(result.passed for result in fixture_results.values())
            and all(result.passed for result in probe_results.values())
        )
        code = failure.code if failure else FailureCode.INTEGRITY_ERROR
        result = QualificationResult(
            contract_digest=contract.contract_digest,
            source_versions_json=contract.source_versions_json,
            fixture_digest=contract.fixture_digest,
            probe_definition_digest=contract.probe_definition_digest,
            probe_digest=_digest(
                {
                    name: result.evidence_digest
                    for name, result in sorted(probe_results.items())
                }
            ),
            qualified_at=self._clock().astimezone(timezone.utc).isoformat(),
            passed=passed,
            failure_code=None if passed else str(code),
            failure_reason=None if passed else _safe_reason(code),
        )
        return self._repository.record_qualification(result)


class _FixtureTicker:
    def __init__(self, frame: pd.DataFrame, metadata: Mapping[str, Any]) -> None:
        self._frame = frame
        self._metadata = metadata

    def history(self, **kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> Mapping[str, Any]:
        return dict(self._metadata)


class QualificationRunner:
    """Execute every mandatory deterministic fixture and bounded live probe."""

    def __init__(
        self,
        repository: BacktestRepository,
        fixture_path: Path,
        probe_definitions: Mapping[str, ProbeDefinition],
        *,
        live_adapter: QualificationAdapter | None = None,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if tuple(sorted(probe_definitions)) != tuple(sorted(MANDATORY_PROBE_IDS)):
            raise ValueError("Mandatory live probe definitions are incomplete")
        self._repository = repository
        self._fixture_path = fixture_path
        self._probe_definitions = probe_definitions
        self._live_adapter = live_adapter or YFinanceQualificationAdapter(clock=clock)
        self._calendar = calendar or TradingCalendar()
        self._clock = clock

    def _fixture_payload(self) -> Mapping[str, object]:
        payload = json.loads(self._fixture_path.read_text())
        if payload.get("fixture_version") != FIXTURE_CONTRACT_VERSION:
            raise ValueError("Unsupported qualification fixture version")
        cases = payload.get("provider_cases")
        if not isinstance(cases, list) or tuple(
            sorted(case["id"] for case in cases)
        ) != tuple(sorted(MANDATORY_FIXTURE_IDS)):
            raise ValueError("Mandatory qualification fixtures are incomplete")
        return payload

    def contract(self) -> QualificationContract:
        return build_qualification_contract(
            self._calendar.session_table_digest(),
            self._fixture_payload(),
            self._probe_definitions,
        )

    def run(self) -> QualificationContract:
        payload = self._fixture_payload()
        contract = build_qualification_contract(
            self._calendar.session_table_digest(), payload, self._probe_definitions
        )
        fixture_results = {
            name: EvidenceCheck(False, _digest({"status": "not_run", "id": name}))
            for name in MANDATORY_FIXTURE_IDS
        }
        probe_results = {
            name: EvidenceCheck(False, _digest({"status": "not_run", "id": name}))
            for name in MANDATORY_PROBE_IDS
        }
        failure: ProviderFailure | None = None
        try:
            for case in payload["provider_cases"]:  # type: ignore[index]
                definition = ProbeDefinition(
                    symbol=case["requested_symbol"],
                    start=date.fromisoformat(case["start"]),
                    end=date.fromisoformat(case["end"]),
                    expected_currency=case["expected_currency"],
                    expected_quote_unit=case["expected_quote_unit"],
                    expected_timezone=case["expected_timezone"],
                    expected_sessions=tuple(
                        date.fromisoformat(value) for value in case["expected_sessions"]
                    ),
                    allowed_observed_symbols=tuple(case["allowed_observed_symbols"]),
                )
                frame = pd.DataFrame(case["rows"])
                frame.index = pd.DatetimeIndex(case["index"])
                adapter = YFinanceQualificationAdapter(
                    lambda _symbol, f=frame, m=case["metadata"]: _FixtureTicker(f, m),
                    sleeper=lambda _delay: None,
                    jitter=lambda _key, _attempt: 0,
                    clock=self._clock,
                )
                normalized = adapter.fetch(definition)
                if normalized.content_digest != case["expected_content_digest"]:
                    raise ProviderFailure(
                        FailureCode.INTEGRITY_ERROR,
                        _safe_reason(FailureCode.INTEGRITY_ERROR),
                    )
                fixture_results[case["id"]] = EvidenceCheck(
                    True, normalized.content_digest
                )
            for name in MANDATORY_PROBE_IDS:
                normalized = self._live_adapter.fetch(self._probe_definitions[name])
                probe_results[name] = EvidenceCheck(True, normalized.content_digest)
        except Exception as exc:
            failure = _classify_exception(exc)
        QualificationRecorder(self._repository, clock=self._clock).record(
            contract, fixture_results, probe_results, failure
        )
        return contract
