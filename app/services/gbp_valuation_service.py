"""GBP valuation of non-GBP ``Money`` (Story 1.6, FR-10/AD-24).

A GBP Valuation Projection is a read-only, presentation-only view of a
``Money`` amount -- it never converts, nets, or hedges anything in the
ledger (PRD §5 Non-Goals). Every projection is either backed by a real,
dated, same-day yfinance FX quote (with a stable content digest so it's
independently verifiable) or is an explicit ``valuation_unavailable``
state -- never a silently fabricated GBP figure.

Isolation (AD-10): this module has zero imports from ``app/services/backtest/``.
``historical_price_evidence.py``'s ``TickerLike``/injected-clock seam is
cited prior art by pattern only; a small local ``TickerLike`` Protocol is
defined here rather than imported, since importing it would create the
exact reverse cross-epic coupling AD-10 forbids.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict

from app.core.money import Money
from app.repositories.fx_quote_repo import (
    FxQuote,
    FxQuoteRepository,
    FxUnavailableAttempt,
)

logger = logging.getLogger(__name__)

#: Every non-GBP entry in Story 1.4's SUPPORTED_CURRENCIES. Each pair
#: quotes units of the non-GBP currency per 1 GBP -- the same convention
#: PortfolioService._to_gbp/gbpusd_rate() already use for GBPUSD=X.
_PAIR_FOR_CURRENCY: dict[str, str] = {
    "USD": "GBPUSD=X",
    "EUR": "GBPEUR=X",
    "HKD": "GBPHKD=X",
}


class TickerLike(Protocol):
    def history(self, *, period: str) -> pd.DataFrame: ...


def _quote_digest(provider: str, pair: str, as_of: str, rate: Decimal) -> str:
    """Stable content digest over a quote's identifying fields.

    A small, self-contained SHA-256 over four scalar fields -- deliberately
    not the Backtest epic's shared canonicalizer (``canonical_manifest.py``),
    which would be exactly the reverse cross-epic coupling AD-10 forbids.
    """
    raw = f"{provider}|{pair}|{as_of}|{rate}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GbpValuationProjection(BaseModel):
    """A read-only GBP view of one ``Money`` amount.

    ``status="valuation_unavailable"`` means exactly what it says: no GBP
    figure is available, never a guessed one. ``reason`` is one of
    ``"unsupported_pair"``, ``"fetch_failed"``, ``"stale"`` when
    unavailable, else None.
    """

    model_config = ConfigDict(frozen=True)

    money: Money
    status: Literal["valued", "valuation_unavailable"]
    quote: FxQuote | None = None
    gbp_amount: Decimal | None = None
    reason: str | None = None


class GbpValuationService:
    """Values ``Money`` in GBP, backed by a same-day, digest-verifiable
    yfinance FX quote (Story 1.6, AC1-3)."""

    _attempt_locks_guard = threading.Lock()
    _attempt_locks: dict[tuple[str, str, str], threading.Lock] = {}

    def __init__(
        self,
        fx_quote_repo: FxQuoteRepository,
        *,
        ticker_factory: Callable[[str], TickerLike] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._fx_quote_repo = fx_quote_repo
        if ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory
        self._clock = clock

    def value_in_gbp(self, money: Money) -> GbpValuationProjection:
        if money.currency == "GBP":
            # A trivial identity projection: GBP money needs no FX quote
            # and is not itself a "conversion" -- no fetch, no repo call.
            return GbpValuationProjection(
                money=money, status="valued", gbp_amount=money.amount
            )

        pair = _PAIR_FOR_CURRENCY.get(money.currency)
        if pair is None:
            # A currency Money legitimately carries that this map doesn't
            # cover -- fail closed, never KeyError/500.
            return GbpValuationProjection(
                money=money,
                status="valuation_unavailable",
                reason="unsupported_pair",
            )

        today = self._clock().date().isoformat()
        provider = "yfinance"
        key = (provider, pair, today)
        with self._lock_for(key):
            cached = self._fx_quote_repo.get_for_provider_pair_and_date(
                provider, pair, today
            )
            if cached is not None:
                return self._valued(money, cached)
            unavailable = self._fx_quote_repo.get_unavailable_attempt(*key)
            if unavailable is not None:
                return self._unavailable(money, unavailable.reason)

            fetched = self._fetch_todays_quote(pair)
            if fetched is None:
                self._record_unavailable(provider, pair, today, "fetch_failed")
                return self._unavailable(money, "fetch_failed")

            as_of, rate = fetched
            if as_of.isoformat() != today:
                # Strict same-day semantics: a prior close is never a valid
                # valuation for the requested date.
                self._record_unavailable(provider, pair, today, "stale")
                return self._unavailable(money, "stale")

            digest = _quote_digest(provider, pair, today, rate)
            quote = FxQuote(
                pair=pair, provider=provider, as_of=today, rate=rate, digest=digest
            )
            self._fx_quote_repo.insert_or_get(quote)
            return self._valued(money, quote)

    @classmethod
    def _lock_for(cls, key: tuple[str, str, str]) -> threading.Lock:
        with cls._attempt_locks_guard:
            return cls._attempt_locks.setdefault(key, threading.Lock())

    def _record_unavailable(
        self, provider: str, pair: str, today: str, reason: str
    ) -> None:
        try:
            self._fx_quote_repo.record_unavailable_attempt(
                FxUnavailableAttempt(
                    provider=provider, pair=pair, requested_date=today, reason=reason
                )
            )
        except Exception:
            # Negative caching is an optimization. A locked/read-only
            # database must not turn a provider miss into a valuation error.
            logger.warning(
                "Unable to persist unavailable FX attempt for %s on %s",
                pair,
                today,
                exc_info=True,
            )

    @staticmethod
    def _unavailable(money: Money, reason: str) -> GbpValuationProjection:
        return GbpValuationProjection(
            money=money, status="valuation_unavailable", reason=reason
        )

    @staticmethod
    def _valued(money: Money, quote: FxQuote) -> GbpValuationProjection:
        gbp_amount = (money.amount / quote.rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        return GbpValuationProjection(
            money=money, status="valued", quote=quote, gbp_amount=gbp_amount
        )

    def _fetch_todays_quote(self, pair: str) -> tuple[date, Decimal] | None:
        """Fetch the latest daily close for ``pair``. Never raises -- every
        failure mode (exception, empty frame, NaN/non-finite, non-positive
        close) degrades to None."""
        try:
            frame = self._ticker_factory(pair).history(period="1d")
        except Exception:
            logger.warning("FX quote fetch failed for %s", pair, exc_info=True)
            return None
        if frame is None or frame.empty:
            return None
        try:
            # A single-symbol Ticker.history() call, unlike yf.download's
            # multi-ticker MultiIndex-columns case, always returns a plain
            # Series here -- no defensive .iloc[:, 0] re-indexing needed.
            close = frame["Close"] if "Close" in frame.columns else frame.iloc[:, 0]
            series = close.dropna()
            if series.empty:
                return None
            idx = series.index[-1]
            val = float(series.iloc[-1])
            if not math.isfinite(val) or val <= 0:
                return None
            as_of = (
                idx.date()
                if hasattr(idx, "date")
                else date.fromisoformat(str(idx)[:10])
            )
            return as_of, Decimal(str(val))
        except Exception:
            logger.warning("FX quote parse failed for %s", pair, exc_info=True)
            return None
