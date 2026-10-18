"""Pinned UK UCITS ETF reference universe for asset-allocation backtests.

One authority for the versioned, content-hashed ETF list
(``config/etf_universe.ucits.v1.json``), acquiring its XLON session evidence
under ``etf:<SYMBOL>`` ids (never the scanner roster), aligning the optional
BTC-GBP sleeve to LSE closes, and sealing/validating the launch pin.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

from app.core.config import ROOT_DIR
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalEvidenceIntegrityError,
    HistoricalPriceRepository,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.trading_calendar import TradingCalendar

ETF_UNIVERSE_VERSION = "etf_universe.ucits.v1"
ETF_UNIVERSE_VERSION_PARAM = "etf_universe_version"
ETF_UNIVERSE_DIGEST_PARAM = "etf_universe_digest"
ETF_EVIDENCE_REVISIONS_PARAM = "etf_evidence_revisions"
ETF_UNIVERSE_HOST_PARAMS = (ETF_UNIVERSE_DIGEST_PARAM, ETF_EVIDENCE_REVISIONS_PARAM)
BITCOIN_SLEEVE_PARAM = "bitcoin_sleeve_enabled"
BITCOIN_SYMBOL = "BTC-GBP"
ETF_UNIVERSE_PATH = ROOT_DIR / "config" / "etf_universe.ucits.v1.json"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_UTC = "UTC"
_LONDON = "Europe/London"
_MIC = "XLON"


@dataclass(frozen=True)
class EtfEntry:
    """One pinned listing; ``exchange_timezone`` ``UTC`` marks a daily-bar
    sleeve (BTC-GBP) that is aligned to XLON closes."""

    symbol: str
    role: str
    asset_class: str
    currency: str
    quote_unit: str
    exchange_timezone: str
    inception: date
    optional: bool

    @property
    def security_id(self) -> str:
        """Evidence id, kept apart from scanner ``security_identities``."""
        return f"etf:{self.symbol}"

    @property
    def is_utc_sleeve(self) -> bool:
        """True for a UTC daily-bar series aligned to XLON sessions."""
        return self.exchange_timezone == _UTC


class EtfUniverse(NamedTuple):
    """The pinned entries and the SHA-256 of the list file's bytes."""

    entries: tuple[EtfEntry, ...]
    digest: str

    def selected(self, *, include_optional: bool) -> tuple[EtfEntry, ...]:
        """Entries in scope; optional sleeves only when opted in."""
        return tuple(e for e in self.entries if include_optional or not e.optional)


@dataclass(frozen=True)
class EtfAcquisitionOutcome:
    """Per-symbol acquisition result; failures never abort other symbols."""

    symbol: str
    data_revision: str | None
    incomplete_months: tuple[str, ...]
    error: str | None


def load_etf_universe(path: Path = ETF_UNIVERSE_PATH) -> EtfUniverse:
    """Read and validate the pinned list.

    Raises ``ValueError`` for any unreadable, malformed or unsupported file.
    """
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"ETF universe is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("ETF universe must be a JSON object")
    if payload.get("version") != ETF_UNIVERSE_VERSION:
        raise ValueError("unsupported ETF universe version")
    items = payload.get("entries")
    if not isinstance(items, list) or not items:
        raise ValueError("ETF universe 'entries' is missing or invalid")
    entries = tuple(_parse_entry(item) for item in items)
    if len({e.symbol for e in entries}) != len(entries):
        raise ValueError("ETF universe has duplicate symbols")
    return EtfUniverse(entries, hashlib.sha256(raw).hexdigest())


def _parse_entry(item: object) -> EtfEntry:
    if not isinstance(item, dict):
        raise ValueError("ETF universe entry must be an object")
    text = ("symbol", "role", "asset_class")
    if not all(isinstance(item.get(k), str) and item[k].strip() for k in text):
        raise ValueError(f"ETF universe entry is malformed: {item!r}")
    if (
        item.get("currency") != "GBP"
        or item.get("quote_unit") not in {"GBP", "GBp"}
        or item.get("exchange_timezone") not in {_LONDON, _UTC}
        or not isinstance(item.get("optional"), bool)
    ):
        raise ValueError(f"ETF universe entry is malformed: {item!r}")
    try:
        inception = date.fromisoformat(str(item.get("inception")))
    except ValueError as exc:
        raise ValueError(f"ETF universe inception is invalid: {item!r}") from exc
    return EtfEntry(
        symbol=item["symbol"],
        role=item["role"],
        asset_class=item["asset_class"],
        currency=item["currency"],
        quote_unit=item["quote_unit"],
        exchange_timezone=item["exchange_timezone"],
        inception=inception,
        optional=item["optional"],
    )


def incomplete_months(
    observed: Iterable[date], expected: Iterable[date]
) -> tuple[str, ...]:
    """Sorted ``YYYY-MM`` months with any expected session missing."""
    present = set(observed)
    return tuple(sorted({f"{d:%Y-%m}" for d in expected if d not in present}))


def align_btc_to_sessions(
    rows: Iterable[Mapping[str, object]], sessions: Iterable[date]
) -> dict[date, float]:
    """Map each XLON session ``d`` to the close of the UTC bar dated ``d-1``.

    That bar closes at 00:00 UTC on ``d``, before the 16:30 London close of
    ``d``, so there is no look-ahead. A missing ``d-1`` bar leaves ``d``
    absent; nothing is forward-filled.
    """
    closes = {
        date.fromisoformat(str(row["session"])): float.fromhex(str(row["close"]))
        for row in rows
    }
    one_day = timedelta(days=1)
    return {d: closes[d - one_day] for d in sessions if d - one_day in closes}


def _row_sessions(rows: Iterable[Mapping[str, object]]) -> tuple[date, ...]:
    return tuple(date.fromisoformat(str(row["session"])) for row in rows)


def _decision_start(entry: EtfEntry) -> date:
    """First XLON session that can carry a value for ``entry``."""
    return entry.inception + timedelta(days=1 if entry.is_utc_sleeve else 0)


def _missing_months(
    entry: EtfEntry,
    rows: Sequence[Mapping[str, object]],
    calendar: TradingCalendar,
    start: date,
    end: date,
) -> tuple[str, ...]:
    """Incomplete months over the XLON sessions in ``[start, end)``."""
    sessions = calendar.sessions_in_range(_MIC, start, end)
    observed: Iterable[date] = (
        align_btc_to_sessions(rows, sessions)
        if entry.is_utc_sleeve
        else _row_sessions(rows)
    )
    return incomplete_months(observed, sessions)


class EtfUniverseEvidenceService:
    """Acquire pinned ETF evidence from each entry's verified inception."""

    def __init__(
        self,
        price_repository: HistoricalPriceRepository,
        adapter: YFinanceHistoricalEvidenceAdapter | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._prices = price_repository
        self._adapter = adapter or YFinanceHistoricalEvidenceAdapter()
        self._calendar = calendar or TradingCalendar()

    def acquire(
        self, universe: EtfUniverse, *, end: date, include_optional: bool = True
    ) -> tuple[EtfAcquisitionOutcome, ...]:
        """Fetch ``[inception, end)`` per symbol, reporting failures per symbol."""
        outcomes = []
        for entry in universe.selected(include_optional=include_optional):
            try:
                outcomes.append(self._acquire_one(entry, end))
            except Exception as exc:  # noqa: BLE001 -- isolate per-symbol failure
                error = str(exc) or type(exc).__name__
                outcomes.append(EtfAcquisitionOutcome(entry.symbol, None, (), error))
        return tuple(outcomes)

    def _acquire_one(self, entry: EtfEntry, end: date) -> EtfAcquisitionOutcome:
        if end <= entry.inception:
            return EtfAcquisitionOutcome(
                entry.symbol, None, (), "end precedes inception"
            )
        payload = self._adapter.fetch(self._request(entry, end))
        sessions = _row_sessions(payload.rows)
        if not sessions:
            return EtfAcquisitionOutcome(entry.symbol, None, (), "no rows returned")
        if sessions[0] != entry.inception:
            error = f"inception mismatch: first session {sessions[0].isoformat()}"
            return EtfAcquisitionOutcome(entry.symbol, None, (), error)
        revision = self._prices.commit(payload)
        stored = self._prices.verify(revision)
        missing = _missing_months(
            entry, stored.rows, self._calendar, _decision_start(entry), end
        )
        return EtfAcquisitionOutcome(entry.symbol, revision, missing, None)

    def _request(self, entry: EtfEntry, end: date) -> HistoricalEvidenceRequest:
        if entry.is_utc_sleeve:
            days = (end - entry.inception).days
            sessions = tuple(entry.inception + timedelta(n) for n in range(days))
        else:
            sessions = self._calendar.sessions_in_range(_MIC, entry.inception, end)
        return HistoricalEvidenceRequest(
            security_id=entry.security_id,
            alias_revision=None,
            symbol=entry.symbol,
            start=entry.inception,
            end=end,
            expected_sessions=sessions,
            allowed_observed_symbols=(entry.symbol,),
            expected_currency=entry.currency,
            expected_quote_unit=entry.quote_unit,
            expected_timezone=entry.exchange_timezone,
            canonical_exchange_sessions=True,
        )


def resolve_etf_universe_pin(
    universe: EtfUniverse,
    prices: HistoricalPriceRepository,
    *,
    start: date,
    end: date,
    include_optional: bool,
    calendar: TradingCalendar | None = None,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Resolve each symbol's acquired revision offline for run ``[start, end)``.

    Returns the pin and per-symbol problems (no evidence, or incomplete run
    months). A symbol whose inception is on/after ``end`` did not yet exist
    in the run window and is left out of the pin.
    """
    calendar = calendar or TradingCalendar()
    revisions: dict[str, str] = {}
    problems: list[str] = []
    for entry in universe.selected(include_optional=include_optional):
        if entry.inception >= end:
            continue
        revision = prices.covering_revision(
            security_id=entry.security_id,
            requested_symbol=entry.symbol,
            start=entry.inception.isoformat(),
            end=end.isoformat(),
        )
        if revision is None:
            problems.append(f"{entry.symbol}: no acquired evidence covers the run")
            continue
        try:
            rows = prices.verify(revision).rows
        except (EvidenceMissingError, HistoricalEvidenceIntegrityError) as exc:
            problems.append(f"{entry.symbol}: evidence failed verification ({exc})")
            continue
        run_start = max(start, _decision_start(entry))
        missing = _missing_months(entry, rows, calendar, run_start, end)
        if missing:
            problems.append(f"{entry.symbol}: incomplete months {', '.join(missing)}")
            continue
        revisions[entry.symbol] = revision
    pin: dict[str, object] = {
        ETF_UNIVERSE_DIGEST_PARAM: universe.digest,
        ETF_EVIDENCE_REVISIONS_PARAM: revisions,
    }
    return pin, tuple(problems)


def validate_etf_universe_pin(parameters: Mapping[str, object]) -> None:
    """Raise ``ValueError`` unless the sealed ETF pin is well formed.

    Parameters without ``etf_universe_version`` carry no pin and must not
    carry stray pin keys either.
    """
    digest = parameters.get(ETF_UNIVERSE_DIGEST_PARAM)
    revisions = parameters.get(ETF_EVIDENCE_REVISIONS_PARAM)
    if ETF_UNIVERSE_VERSION_PARAM not in parameters:
        if digest is not None or revisions is not None:
            raise ValueError("ETF pin supplied without an ETF universe version")
        return
    if parameters[ETF_UNIVERSE_VERSION_PARAM] != ETF_UNIVERSE_VERSION:
        raise ValueError("unsupported ETF universe version")
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        raise ValueError("ETF universe digest is missing or malformed")
    if (
        not isinstance(revisions, Mapping)
        or not revisions
        or not all(
            isinstance(symbol, str)
            and isinstance(revision, str)
            and _SHA256_HEX.fullmatch(revision)
            for symbol, revision in revisions.items()
        )
    ):
        raise ValueError("ETF evidence revisions are missing or malformed")
    if BITCOIN_SYMBOL in revisions and parameters.get(BITCOIN_SLEEVE_PARAM) is not True:
        raise ValueError("bitcoin evidence pinned without the bitcoin sleeve")
