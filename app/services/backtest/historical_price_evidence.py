"""Provider-native historical price evidence under the closed AD-6 contract."""

from __future__ import annotations

import math
import time
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
from typing import Any, Protocol

import pandas as pd
import yfinance as yf

from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.historical_data_qualification import (
    CANONICALIZER_VERSION,
    REQUEST_CONTRACT_VERSION,
    FailureCode,
    ProviderFailure,
    _classify_exception,
    _digest,
    _jsonable,
    _safe_reason,
    deterministic_jitter,
)

#: The only cross-currency pair v1 needs (``base_currency`` and every
#: evidence ``currency`` are closed to ``{"GBP", "USD"}``) and the
#: pseudo-security id under which its daily rate series is ingested into
#: the historical price cache (#459).
FX_PAIR = "GBPUSD=X"
FX_SERIES_SECURITY_ID = "fx:GBPUSD=X"

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
CANONICAL_EXCHANGE_SESSIONS_POLICY = "canonical_exchange_sessions_v2"


class TickerLike(Protocol):
    def history(self, **kwargs: object) -> pd.DataFrame: ...

    def get_history_metadata(self, repair: bool = False) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HistoricalEvidenceRequest:
    """One resolved security and canonical inclusive/exclusive request interval."""

    security_id: str | None
    alias_revision: str | None
    symbol: str
    start: date
    end: date
    expected_currency: str
    expected_quote_unit: str
    expected_timezone: str
    expected_sessions: tuple[date, ...]
    allowed_observed_symbols: tuple[str, ...]
    allow_missing_prefix: bool = False
    canonical_exchange_sessions: bool = False


@dataclass(frozen=True)
class HistoricalEvidencePayload:
    security_id: str | None
    alias_revision: str | None
    provider: str
    provider_version: str
    request_contract_version: str
    requested_symbol: str
    observed_symbol: str
    currency: str
    quote_unit: str
    quote_unit_scale: str
    exchange_timezone: str
    start: str
    end: str
    request_contract: Mapping[str, object]
    rows: tuple[Mapping[str, object], ...]
    actions: tuple[Mapping[str, object], ...]
    response_metadata_digest: str
    data_revision: str
    canonical_manifest_json: str
    acquired_at: str


def rebind_historical_evidence_alias(
    evidence: Any, *, alias_revision: str, acquired_at: str
) -> HistoricalEvidencePayload:
    """Reseal verified evidence when only its global alias revision changed."""
    identity: dict[str, object] = {
        "canonicalizer_version": CANONICALIZER_VERSION,
        "request_contract_version": evidence.request_contract_version,
        "request": dict(evidence.request_contract),
        "requested_symbol": evidence.requested_symbol,
        "observed_symbol": evidence.observed_symbol,
        "currency": evidence.currency,
        "quote_unit": evidence.quote_unit,
        "quote_unit_scale": evidence.quote_unit_scale,
        "exchange_timezone": evidence.exchange_timezone,
        "rows": list(evidence.rows),
        "provider": evidence.provider,
        "provider_version": evidence.provider_version,
        "security_id": evidence.security_id,
        "alias_revision": alias_revision,
        "actions": list(evidence.actions),
    }
    return HistoricalEvidencePayload(
        security_id=evidence.security_id,
        alias_revision=alias_revision,
        provider=evidence.provider,
        provider_version=evidence.provider_version,
        request_contract_version=evidence.request_contract_version,
        requested_symbol=evidence.requested_symbol,
        observed_symbol=evidence.observed_symbol,
        currency=evidence.currency,
        quote_unit=evidence.quote_unit,
        quote_unit_scale=evidence.quote_unit_scale,
        exchange_timezone=evidence.exchange_timezone,
        start=evidence.start,
        end=evidence.end,
        request_contract=dict(evidence.request_contract),
        rows=evidence.rows,
        actions=evidence.actions,
        response_metadata_digest=evidence.response_metadata_digest,
        data_revision=manifest_digest(identity),
        canonical_manifest_json=canonical_json(identity),
        acquired_at=acquired_at,
    )


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


def _quote_contract(provider_unit: str) -> tuple[str, str, str]:
    if provider_unit == "GBp":
        return "GBP", "GBp", "0.01"
    if provider_unit in {"GBP", "USD"}:
        return provider_unit, provider_unit, "1"
    raise ProviderFailure(
        FailureCode.PROVIDER_CONTRACT_ERROR,
        _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
    )


def request_contract(request: HistoricalEvidenceRequest) -> dict[str, object]:
    contract: dict[str, object] = {
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "interval": "1d",
        "prepost": False,
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "timeout": 15,
        "raise_errors": True,
    }
    return contract


def canonical_provider_metadata(metadata: Mapping[str, Any]) -> Mapping[str, object]:
    """Convert provider metadata to the canonical JSON domain.

    yfinance 1.0 includes a ``tradingPeriods`` DataFrame in its otherwise
    mapping-shaped history metadata. It is response metadata, not price
    evidence, but it remains part of the deterministic response digest.
    """

    def convert(value: Any) -> object:
        if isinstance(value, pd.DataFrame):
            return {
                "kind": "dataframe",
                "value": json.loads(
                    value.to_json(orient="split", date_format="iso", date_unit="ns")
                ),
            }
        if isinstance(value, pd.Series):
            return {
                "kind": "series",
                "value": json.loads(value.to_json(date_format="iso", date_unit="ns")),
            }
        if isinstance(value, Mapping):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return _jsonable(value)

    return {str(key): convert(value) for key, value in metadata.items()}


def normalize_historical_response(
    *,
    definition: HistoricalEvidenceRequest,
    frame: pd.DataFrame,
    metadata: Mapping[str, Any],
    request: Mapping[str, object],
    acquired_at: datetime,
    provider_version: str,
) -> HistoricalEvidencePayload:
    """Normalize one yfinance response and calculate its immutable revision."""
    try:
        if definition.start >= definition.end:
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        if any(column not in frame.columns for column in _REQUIRED_COLUMNS):
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
        if observed_symbol not in definition.allowed_observed_symbols:
            raise ProviderFailure(
                FailureCode.IDENTITY_AMBIGUOUS,
                _safe_reason(FailureCode.IDENTITY_AMBIGUOUS),
            )
        currency, quote_unit, quote_scale = _quote_contract(provider_unit)
        if (
            currency != definition.expected_currency
            or quote_unit != definition.expected_quote_unit
            or exchange_timezone != definition.expected_timezone
        ):
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )

        localized = frame.copy()
        localized.index = frame.index.tz_convert(exchange_timezone)
        localized = localized.sort_index()
        expected_sessions = tuple(definition.expected_sessions)
        if definition.canonical_exchange_sessions:
            expected_set = set(expected_sessions)
            localized = localized[
                [timestamp.date() in expected_set for timestamp in localized.index]
            ]
        sessions = tuple(timestamp.date() for timestamp in localized.index)
        if definition.canonical_exchange_sessions:
            sessions_match = bool(sessions)
        else:
            sessions_match = sessions == expected_sessions
            if definition.allow_missing_prefix and sessions:
                sessions_match = sessions == expected_sessions[-len(sessions) :]
        if not sessions_match or any(
            session < definition.start or session >= definition.end
            for session in sessions
        ):
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        if localized[list(_REQUIRED_VALUE_COLUMNS)].isna().any(axis=None):
            if definition.canonical_exchange_sessions:
                localized = localized.dropna(subset=list(_REQUIRED_VALUE_COLUMNS))
            else:
                raise ProviderFailure(
                    FailureCode.REQUIRED_DATA_MISSING,
                    _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
                )

        rows: list[Mapping[str, object]] = []
        actions: list[Mapping[str, object]] = []
        for timestamp, row in localized.iterrows():
            session = pd.Timestamp(str(timestamp)).date().isoformat()
            numeric = tuple(
                float(row[column])
                for column in (
                    "Open",
                    "High",
                    "Low",
                    "Close",
                    "Volume",
                    "Dividends",
                    "Stock Splits",
                )
            )
            open_value, high, low, close, volume, dividend, split = numeric
            valid_observation = (
                all(math.isfinite(value) for value in numeric)
                and min(open_value, high, low, close) > 0
                and low <= min(open_value, close)
                and high >= max(open_value, close)
                and volume >= 0
                and dividend >= 0
                and split >= 0
            )
            if definition.canonical_exchange_sessions and not valid_observation:
                continue
            if not all(math.isfinite(value) for value in numeric):
                raise ProviderFailure(
                    FailureCode.PROVIDER_CONTRACT_ERROR,
                    _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
                )
            if volume < 0 or dividend < 0 or split < 0:
                raise ProviderFailure(
                    FailureCode.PROVIDER_CONTRACT_ERROR,
                    _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
                )
            normalized_row = {
                "session": session,
                "open": _number(row["Open"]),
                "high": _number(row["High"]),
                "low": _number(row["Low"]),
                "close": _number(row["Close"]),
                "adj_close": _number(row["Adj Close"], nullable=True),
                "volume": _number(row["Volume"]),
                "dividends": _number(row["Dividends"]),
                "stock_splits": _number(row["Stock Splits"]),
            }
            rows.append(normalized_row)
            if dividend:
                actions.append(
                    {
                        "session": session,
                        "action_type": "dividend",
                        "value": _number(dividend),
                    }
                )
            if split:
                actions.append(
                    {
                        "session": session,
                        "action_type": "split",
                        "value": _number(split),
                    }
                )

        normalized_metadata = canonical_provider_metadata(metadata)
        canonical_request = dict(request)
        if definition.canonical_exchange_sessions:
            canonical_request["observation_policy"] = CANONICAL_EXCHANGE_SESSIONS_POLICY
        if not rows:
            raise ProviderFailure(
                FailureCode.REQUIRED_DATA_MISSING,
                _safe_reason(FailureCode.REQUIRED_DATA_MISSING),
            )
        identity: dict[str, object] = {
            "canonicalizer_version": CANONICALIZER_VERSION,
            "request_contract_version": REQUEST_CONTRACT_VERSION,
            "request": canonical_request,
            "requested_symbol": definition.symbol,
            "observed_symbol": observed_symbol,
            "currency": currency,
            "quote_unit": quote_unit,
            "quote_unit_scale": quote_scale,
            "exchange_timezone": exchange_timezone,
            "rows": rows,
        }
        # Qualification payload digests predate Story 1.3. Preserve them when
        # no resolved security is supplied; production evidence binds identity,
        # provider version, aliases and the explicit action projection.
        if definition.security_id is not None:
            identity.update(
                {
                    "provider": "yfinance",
                    "provider_version": provider_version,
                    "security_id": definition.security_id,
                    "alias_revision": definition.alias_revision,
                    "actions": actions,
                }
            )
        manifest_json = canonical_json(identity)
        return HistoricalEvidencePayload(
            security_id=definition.security_id,
            alias_revision=definition.alias_revision,
            provider="yfinance",
            provider_version=provider_version,
            request_contract_version=REQUEST_CONTRACT_VERSION,
            requested_symbol=definition.symbol,
            observed_symbol=observed_symbol,
            currency=currency,
            quote_unit=quote_unit,
            quote_unit_scale=quote_scale,
            exchange_timezone=exchange_timezone,
            start=definition.start.isoformat(),
            end=definition.end.isoformat(),
            request_contract=canonical_request,
            rows=tuple(rows),
            actions=tuple(actions),
            response_metadata_digest=_digest(normalized_metadata),
            data_revision=manifest_digest(identity),
            canonical_manifest_json=manifest_json,
            acquired_at=acquired_at.astimezone(timezone.utc).isoformat(),
        )
    except ProviderFailure:
        raise
    except Exception as exc:
        raise ProviderFailure(
            FailureCode.PROVIDER_CONTRACT_ERROR,
            _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
        ) from exc


class YFinanceHistoricalEvidenceAdapter:
    """Fetch one immutable provider-native interval with bounded retries."""

    def __init__(
        self,
        ticker_factory: Callable[[str], TickerLike] = yf.Ticker,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[str, int], float] = deterministic_jitter,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        provider_version: str | None = None,
    ) -> None:
        self._ticker_factory = ticker_factory
        self._sleeper = sleeper
        self._jitter = jitter
        self._clock = clock
        self._provider_version = provider_version or version("yfinance")

    def fetch(self, definition: HistoricalEvidenceRequest) -> HistoricalEvidencePayload:
        if definition.start >= definition.end:
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )
        request = request_contract(definition)
        request_key = (
            f"{definition.symbol}:{definition.start}:{definition.end}:"
            f"{REQUEST_CONTRACT_VERSION}"
            if definition.security_id is None
            else f"{definition.security_id}:{definition.symbol}:{definition.start}:"
            f"{definition.end}:{REQUEST_CONTRACT_VERSION}"
        )
        for attempt in range(3):
            try:
                ticker = self._ticker_factory(definition.symbol)
                frame = ticker.history(**request)
                metadata = dict(ticker.get_history_metadata(repair=False))
                return normalize_historical_response(
                    definition=definition,
                    frame=frame,
                    metadata=metadata,
                    request=request,
                    acquired_at=self._clock(),
                    provider_version=self._provider_version,
                )
            except Exception as exc:
                failure = _classify_exception(exc)
                retryable = failure.retryable or (
                    failure.code is FailureCode.PROVIDER_CONTRACT_ERROR
                )
                if not retryable or attempt == 2:
                    raise failure from exc
                base = 1.0 if attempt == 0 else 2.0
                self._sleeper(min(2.25, base + self._jitter(request_key, attempt)))
        raise AssertionError("unreachable")


class FxSeriesFetcher(Protocol):
    """Fetch the daily ``GBPUSD=X`` rate series over one run window.

    ``start``/``end`` are the inclusive-window bounds exactly as they
    should appear in the produced evidence (``end`` exclusive, matching
    :class:`HistoricalEvidenceRequest`'s own convention).
    """

    def fetch(self, *, start: date, end: date) -> HistoricalEvidencePayload: ...


class YFinanceFxSeriesFetcher:
    """Production ranged fetch of the daily ``GBPUSD=X`` rate series (#459).

    The engine's currency contract (``currency.py::_fx_closes``) consumes
    a full daily rate series as ``StoredHistoricalEvidence``; this fetcher
    produces exactly that payload through the shared
    :class:`YFinanceHistoricalEvidenceAdapter`, so it passes engine
    validation unmodified (provider ``yfinance``, symbol ``GBPUSD=X``,
    USD/USD quote contract, UTC, no corporate actions).

    ``expected_sessions`` covers every calendar day in the window and the
    canonical observation policy drops non-trading/invalid provider rows,
    so weekend and holiday gaps degrade to the engine's own
    <=5-calendar-day staleness guard instead of a hard failure.
    """

    def __init__(
        self,
        ticker_factory: Callable[[str], TickerLike] = yf.Ticker,
        *,
        adapter: YFinanceHistoricalEvidenceAdapter | None = None,
    ) -> None:
        self._adapter = adapter or YFinanceHistoricalEvidenceAdapter(ticker_factory)

    def fetch(self, *, start: date, end: date) -> HistoricalEvidencePayload:
        if start >= end:
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                _safe_reason(FailureCode.PROVIDER_CONTRACT_ERROR),
            )
        request = HistoricalEvidenceRequest(
            security_id=FX_SERIES_SECURITY_ID,
            alias_revision=None,
            symbol=FX_PAIR,
            start=start,
            end=end,
            expected_currency="USD",
            expected_quote_unit="USD",
            expected_timezone="UTC",
            expected_sessions=tuple(
                start + timedelta(days=offset) for offset in range((end - start).days)
            ),
            allowed_observed_symbols=(FX_PAIR,),
            canonical_exchange_sessions=True,
        )
        return self._adapter.fetch(request)
