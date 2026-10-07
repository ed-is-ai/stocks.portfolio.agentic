"""WIKI archive (#70) as a historical price evidence provider (#82 B).

WIKI stores as-traded daily rows. Evidence uses yfinance's shape instead:
prices and dividends divided by the product of the WIKI splits in the
request window dated after the row (so every adjustment has its split
action in the evidence), with ``split`` and ``dividend`` actions.
``HistoricalMarketPlanes.as_traded()`` then multiplies
the splits back, returning WIKI's own closes. Volume stays as traded, which
is how the planes read stored volume. The archive is opened read-only.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path

from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.historical_data_qualification import (
    CANONICALIZER_VERSION,
    FailureCode,
    ProviderFailure,
    _safe_reason,
)
from app.services.backtest.historical_price_evidence import (
    CANONICAL_EXCHANGE_SESSIONS_POLICY,
    HistoricalEvidencePayload,
    HistoricalEvidenceRequest,
    _number,
)
from app.services.index_membership.coverage import read_only

WIKI_PROVIDER = "wiki"
WIKI_REQUEST_CONTRACT_VERSION = "WikiArchiveDailyV1"
_CURRENCY = "USD"
_TIMEZONE = "America/New_York"
_ROWS = (
    "SELECT date, open, high, low, close, volume, ex_dividend, split_ratio"
    " FROM wiki_prices WHERE ticker = ? ORDER BY date"
)

_Value = float | None
#: ``(date, open, high, low, close, volume, ex_dividend, split_ratio)``.
type WikiRow = tuple[str, _Value, _Value, _Value, _Value, _Value, _Value, _Value]


def wiki_request_contract(start: str, end: str) -> dict[str, object]:
    """Return the WIKI request contract for ``[start, end)`` (ISO dates)."""
    return {"start": start, "end": end, "interval": "1d", "source": "wiki_prices"}


def _failure(code: FailureCode, detail: str | None = None) -> ProviderFailure:
    return ProviderFailure(code, detail or _safe_reason(code))


class WikiHistoricalEvidenceAdapter:
    """Build provider-native evidence for one WIKI ticker from ``wiki_db``."""

    def __init__(
        self,
        wiki_db: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._wiki_db = wiki_db
        self._clock = clock

    def fetch(self, definition: HistoricalEvidenceRequest) -> HistoricalEvidencePayload:
        """Return evidence for ``definition.symbol`` (a WIKI ticker).

        Raises ``ProviderFailure`` when the archive is missing, the request
        contract does not hold, or no row survives the request window.
        """
        if definition.start >= definition.end:
            raise _failure(FailureCode.PROVIDER_CONTRACT_ERROR)
        if definition.symbol not in definition.allowed_observed_symbols:
            raise _failure(FailureCode.IDENTITY_AMBIGUOUS)
        if (
            definition.expected_currency not in {None, _CURRENCY}
            or definition.expected_quote_unit not in {None, _CURRENCY}
            or definition.expected_timezone not in {None, _TIMEZONE}
        ):
            raise _failure(FailureCode.PROVIDER_CONTRACT_ERROR)
        source_digest, history = self._read(definition.symbol)
        rows, actions = _normalize(definition, history)
        request: dict[str, object] = wiki_request_contract(
            definition.start.isoformat(), definition.end.isoformat()
        )
        if definition.canonical_exchange_sessions:
            request["observation_policy"] = CANONICAL_EXCHANGE_SESSIONS_POLICY
        identity: dict[str, object] = {
            "canonicalizer_version": CANONICALIZER_VERSION,
            "request_contract_version": WIKI_REQUEST_CONTRACT_VERSION,
            "request": request,
            "requested_symbol": definition.symbol,
            "observed_symbol": definition.symbol,
            "currency": _CURRENCY,
            "quote_unit": _CURRENCY,
            "quote_unit_scale": "1",
            "exchange_timezone": _TIMEZONE,
            "rows": rows,
            "provider": WIKI_PROVIDER,
            "provider_version": source_digest,
            "security_id": definition.security_id,
            "alias_revision": definition.alias_revision,
            "actions": actions,
        }
        return HistoricalEvidencePayload(
            security_id=definition.security_id,
            alias_revision=definition.alias_revision,
            provider=WIKI_PROVIDER,
            provider_version=source_digest,
            request_contract_version=WIKI_REQUEST_CONTRACT_VERSION,
            requested_symbol=definition.symbol,
            observed_symbol=definition.symbol,
            currency=_CURRENCY,
            quote_unit=_CURRENCY,
            quote_unit_scale="1",
            exchange_timezone=_TIMEZONE,
            start=definition.start.isoformat(),
            end=definition.end.isoformat(),
            request_contract=request,
            rows=tuple(rows),
            actions=tuple(actions),
            response_metadata_digest=manifest_digest(
                {"source_digest": source_digest, "ticker": definition.symbol}
            ),
            data_revision=manifest_digest(identity),
            canonical_manifest_json=canonical_json(identity),
            acquired_at=self._clock().astimezone(timezone.utc).isoformat(),
        )

    def _read(self, ticker: str) -> tuple[str, list[WikiRow]]:
        """Return the import digest and every row of ``ticker`` by date."""
        if not self._wiki_db.is_file():
            raise _failure(
                FailureCode.PROVIDER_UNAVAILABLE,
                f"WIKI price archive not found: {self._wiki_db}",
            )
        conn = read_only(self._wiki_db)
        try:
            conn.execute("BEGIN")  # one snapshot for the digest and the rows
            latest = conn.execute(
                "SELECT source_digest FROM wiki_imports ORDER BY id DESC LIMIT 1"
            ).fetchone()
            history = conn.execute(_ROWS, (ticker,)).fetchall()
        except sqlite3.Error as exc:
            raise _failure(
                FailureCode.PROVIDER_UNAVAILABLE,
                f"WIKI price archive unreadable: {self._wiki_db}",
            ) from exc
        finally:
            conn.close()
        if latest is None:
            raise _failure(
                FailureCode.PROVIDER_UNAVAILABLE,
                f"WIKI price archive has no import: {self._wiki_db}",
            )
        return str(latest[0]), history


def _normalize(
    definition: HistoricalEvidenceRequest, history: list[WikiRow]
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    """Apply the yfinance normaliser's session rules and split-adjust rows."""
    try:
        window = [
            row
            for row in history
            if definition.start <= date.fromisoformat(row[0]) < definition.end
        ]
    except ValueError as exc:  # a stored date that is not ISO
        raise _failure(FailureCode.PROVIDER_CONTRACT_ERROR) from exc
    expected = tuple(definition.expected_sessions)
    if definition.canonical_exchange_sessions:
        expected_set = set(expected)
        window = [row for row in window if date.fromisoformat(row[0]) in expected_set]
        sessions_match = bool(window)
    else:
        sessions = tuple(date.fromisoformat(row[0]) for row in window)
        sessions_match = sessions == expected
        if definition.allow_missing_prefix and sessions:
            sessions_match = sessions == expected[-len(sessions) :]
    if not sessions_match:
        raise _failure(FailureCode.REQUIRED_DATA_MISSING)

    # Only rows that become evidence may adjust earlier prices, so every
    # adjustment has its split action.
    splits = [
        (row[0], _ratio(row[7]))
        for row in window
        if _ratio(row[7]) != 1.0 and _valid(*row[1:])
    ]
    rows: list[Mapping[str, object]] = []
    actions: list[Mapping[str, object]] = []
    for session, open_value, high, low, close, volume, dividend, ratio in window:
        if not _valid(open_value, high, low, close, volume, dividend, ratio):
            if definition.canonical_exchange_sessions:
                continue
            raise _failure(FailureCode.REQUIRED_DATA_MISSING)
        assert open_value is not None and high is not None and low is not None
        assert close is not None and volume is not None
        factor = math.prod(value for day, value in splits if day > session)
        paid = (dividend or 0.0) / factor
        split = _ratio(ratio)
        split = 0.0 if split == 1.0 else split
        rows.append(
            {
                "session": session,
                "open": _number(open_value / factor),
                "high": _number(high / factor),
                "low": _number(low / factor),
                "close": _number(close / factor),
                "adj_close": None,
                "volume": _number(volume),
                "dividends": _number(paid),
                "stock_splits": _number(split),
            }
        )
        if paid:
            actions.append(
                {"session": session, "action_type": "dividend", "value": _number(paid)}
            )
        if split:
            actions.append(
                {"session": session, "action_type": "split", "value": _number(split)}
            )
    if not rows:
        raise _failure(FailureCode.REQUIRED_DATA_MISSING)
    return rows, actions


def _ratio(value: float | None) -> float:
    """Return a WIKI split ratio, reading a missing one as 1 (no split)."""
    return 1.0 if value is None else float(value)


def _valid(
    open_value: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None,
    dividend: float | None,
    ratio: float | None,
) -> bool:
    """Return whether one WIKI row passes the yfinance observation checks."""
    if open_value is None or high is None or low is None or close is None:
        return False
    if volume is None:
        return False
    numbers = (open_value, high, low, close, volume, dividend or 0.0, _ratio(ratio))
    return (
        all(math.isfinite(value) for value in numbers)
        and min(open_value, high, low, close) > 0
        and low <= min(open_value, close)
        and high >= max(open_value, close)
        and volume >= 0
        and (dividend or 0.0) >= 0
        and _ratio(ratio) > 0
    )
