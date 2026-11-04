"""Build a SPY total-return benchmark line from a run's pinned evidence."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from decimal import Decimal
import logging
import sqlite3
from typing import Protocol

from app.repositories.backtest_repo import BacktestResultV1
from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.currency import prepare_fx_closes
from app.services.backtest.market_planes import provider_decimal

logger = logging.getLogger(__name__)

NO_PIN_NOTE = "No pinned SPY evidence for this run."
NO_FX_NOTE = "No GBP/USD rates pinned to this run."
BAD_EVIDENCE_NOTE = "The pinned SPY evidence could not be read for this run."
NO_COVERAGE_NOTE = "The pinned SPY evidence does not cover this run's sessions."
NOT_SPY_NOTE = "The pinned benchmark is not SPY."
STALE_FX_NOTE = "The pinned GBP/USD rates end before this run does."
_READ_ERRORS = (
    ValueError,
    TypeError,
    ArithmeticError,
    LookupError,
    RuntimeError,
    OSError,
    sqlite3.Error,
)


class EvidenceReader(Protocol):
    """The read-only slice of ``HistoricalPriceRepository`` this module uses."""

    def get(self, data_revision: str) -> StoredHistoricalEvidence: ...


@dataclass(frozen=True)
class BenchmarkSeries:
    """SPY total-return value in the run's base currency, one per run session."""

    values: tuple[Decimal, ...]


@dataclass(frozen=True)
class BenchmarkUnavailable:
    """Why no benchmark line can be drawn."""

    note: str


def spy_total_return_series(
    result: BacktestResultV1,
    sessions: Sequence[date],
    prices: EvidenceReader,
    fx_revision: str | None,
) -> BenchmarkSeries | BenchmarkUnavailable:
    """Return SPY total return aligned to ``sessions``, or why it is unavailable.

    Dividends are reinvested at the ex-date raw close; a run session without
    an XNYS close carries the last SPY value forward, and nothing is ever
    backfilled before the first SPY session. GBP runs divide the USD value by
    the latest pinned GBP/USD close on or before each session. Outcomes are
    cached per immutable revision; read errors are logged and never cached.
    """
    pin = result.regime_benchmark
    if pin is None:
        return BenchmarkUnavailable(NO_PIN_NOTE)
    if result.base_currency not in {"USD", "GBP"}:
        return BenchmarkUnavailable(
            f"No SPY benchmark for base currency {result.base_currency}."
        )
    if result.base_currency == "USD":
        fx_revision = None
    elif fx_revision is None:
        return BenchmarkUnavailable(NO_FX_NOTE)
    try:
        return _cached_series(
            prices, pin.security_id, pin.price_revision, fx_revision, tuple(sessions)
        )
    except _READ_ERRORS:
        logger.warning("Pinned SPY benchmark evidence is unreadable", exc_info=True)
        return BenchmarkUnavailable(BAD_EVIDENCE_NOTE)


@lru_cache(maxsize=32)
def _cached_series(
    prices: EvidenceReader,
    security_id: str,
    price_revision: str,
    fx_revision: str | None,
    sessions: tuple[date, ...],
) -> BenchmarkSeries | BenchmarkUnavailable:
    """Compute one benchmark outcome; ``fx_revision`` ``None`` means USD."""
    evidence = prices.get(price_revision)
    if evidence.security_id != security_id or evidence.currency != "USD":
        return BenchmarkUnavailable(BAD_EVIDENCE_NOTE)
    if "SPY" not in {evidence.requested_symbol, evidence.observed_symbol}:
        return BenchmarkUnavailable(NOT_SPY_NOTE)
    usd = _aligned(_total_return(evidence), sessions, _end(evidence))
    if usd is None:
        return BenchmarkUnavailable(NO_COVERAGE_NOTE)
    if fx_revision is None:
        return BenchmarkSeries(usd)
    fx_evidence = prices.get(fx_revision)
    if not sessions or sessions[-1] >= _end(fx_evidence):
        return BenchmarkUnavailable(STALE_FX_NOTE)
    fx = prepare_fx_closes(fx_evidence)
    converted: list[Decimal] = []
    for value, session in zip(usd, sessions):
        rate = fx.latest_on_or_before(session)
        if rate is None:
            return BenchmarkUnavailable(NO_FX_NOTE)
        converted.append(value / rate[1])
    return BenchmarkSeries(tuple(converted))


def _end(evidence: StoredHistoricalEvidence) -> date:
    """The exclusive end of an evidence interval."""
    return date.fromisoformat(evidence.end)


def _total_return(
    evidence: StoredHistoricalEvidence,
) -> tuple[tuple[date, Decimal], ...]:
    """Reinvest each cash dividend at its ex-date close; ``units * close``.

    yfinance closes are already split-continuous, so splits need no handling.
    """
    start, end = date.fromisoformat(evidence.start), _end(evidence)
    rows = sorted(
        (
            date.fromisoformat(str(raw["session"])),
            provider_decimal(raw["close"]),
            provider_decimal(raw["dividends"]),
        )
        for raw in evidence.rows
    )
    if len({session for session, _, _ in rows}) != len(rows):
        raise ValueError("SPY evidence has duplicate sessions")
    units = Decimal(1)
    series: list[tuple[date, Decimal]] = []
    for session, close, dividend in rows:
        if close is None or dividend is None or close <= 0:
            raise ValueError("SPY evidence row is invalid")
        if not start <= session < end:
            raise ValueError("SPY evidence row is outside its interval")
        if dividend > 0:
            units *= 1 + dividend / close
        series.append((session, units * close))
    return tuple(series)


def _aligned(
    series: tuple[tuple[date, Decimal], ...],
    sessions: Sequence[date],
    evidence_end: date,
) -> tuple[Decimal, ...] | None:
    """Carry SPY forward onto ``sessions``; ``None`` if any lies outside it."""
    if not sessions or sessions[-1] >= evidence_end:
        return None
    spy_sessions = [session for session, _ in series]
    values: list[Decimal] = []
    for session in sessions:
        index = bisect_right(spy_sessions, session) - 1
        if index < 0:
            return None
        values.append(series[index][1])
    return tuple(values)
