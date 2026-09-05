"""Dated GBP closes for portfolio holdings, from stored evidence only (#481).

The historical price cache (``historical_price_cache.db``) already holds
provider-native daily closes keyed by the symbol that was *requested*, so a
portfolio ticker can reach them through the shared alias map -- the
``historical_price_*`` tables are not, as the repair pass previously
assumed, unreachable without a backtest ``security_id``.

Every lookup here is exact-date and evidence-only: no network call, no
nearby session, no invented FX rate. Anything missing returns None so the
caller records an honest gap.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
import sqlite3

from app.core.config import HISTORICAL_PRICE_CACHE
from app.core.ticker_identity import (
    canonicalize_or_fallback,
    load_aliases,
    matching_raw_tickers,
)
from app.repositories.db import Connect
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.fx_rate_cache_repo import FxRateCacheRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.snapshot_valuation import valid_rate_or_none

logger = logging.getLogger(__name__)


def _read_only_connect(path: Path) -> Connect:
    """Return a ``Connect`` that opens ``path`` read-only.

    ``sqlite3.connect`` creates an absent file; ``mode=ro`` instead raises
    "unable to open database file", which the repository reads as "no
    evidence". That keeps a repair run from leaving an empty cache database
    behind on a checkout that has never run a backtest, and makes this
    module's read-only contract structural rather than merely intended.
    """

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    return _connect


def build_price_source(trades_connect: Connect) -> "HistoricalCacheGbpPriceSource":
    """Wire the cache-backed price source against the app's real databases.

    ``trades_connect`` opens ``trades.db`` (both FX evidence stores live
    there); the historical cache is opened from
    :data:`app.core.config.HISTORICAL_PRICE_CACHE`. The cache is only ever
    opened for reading: on a checkout that has never run a backtest the
    file (and its ``data/`` directory) does not exist, and the source
    reports no evidence rather than creating a stray empty database.
    """
    if not HISTORICAL_PRICE_CACHE.exists():
        logger.info(
            "no historical price cache at %s: every row stays a gap",
            HISTORICAL_PRICE_CACHE,
        )
    cache_connect = _read_only_connect(HISTORICAL_PRICE_CACHE)
    return HistoricalCacheGbpPriceSource(
        HistoricalPriceRepository(cache_connect),
        FxQuoteRepository(trades_connect),
        FxRateCacheRepository(trades_connect),
    )


class HistoricalCacheGbpPriceSource:
    """A ``HistoricalGbpPriceSource`` backed by stored dated evidence.

    Resolves a holding's ticker through the alias map to every spelling the
    cache might have been asked for, reads the close for the exact session
    date, scales it to major units with the revision's ``quote_unit_scale``
    (this is what makes a pence-quoted ``GBp`` LSE line correct), and, for a
    non-GBP currency, divides by a stored dated ``GBP<CCY>=X`` rate --
    ``fx_quotes`` first, then ``fx_rate_cache``. The cache is read-only:
    nothing is written or pinned.
    """

    def __init__(
        self,
        prices: HistoricalPriceRepository,
        fx_quotes: FxQuoteRepository,
        fx_cache: FxRateCacheRepository,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._prices = prices
        self._fx_quotes = fx_quotes
        self._fx_cache = fx_cache
        self._aliases = load_aliases() if aliases is None else aliases

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        """Return the GBP close for ``ticker`` on ``as_of`` (YYYY-MM-DD)."""
        symbols = self._symbols_for(ticker)
        observed = self._prices.dated_close(sorted(symbols), as_of)
        if observed is None:
            return None
        scale = _positive_float_or_none(observed.quote_unit_scale)
        if scale is None or _positive_float_or_none(observed.close) is None:
            # A NaN close is a ``keepna`` placeholder, and a zero or
            # negative one is not a price either -- valuing a holding at
            # either would understate the portfolio, not report a gap.
            logger.debug(
                "no usable close for %s on %s (revision %s)",
                ticker,
                as_of,
                observed.data_revision,
            )
            return None
        native = observed.close * scale
        currency = observed.currency.strip().upper()
        if not currency:
            # An undenominated close cannot be converted, or assumed GBP.
            logger.debug("no currency on the close for %s on %s", ticker, as_of)
            return None
        if currency == "GBP":
            return native
        rate = self._dated_rate(f"GBP{currency}=X", as_of)
        if rate is None:
            logger.debug("no dated %s rate for %s on %s", currency, ticker, as_of)
            return None
        # Pairs quote units of the foreign currency per 1 GBP.
        return native / rate

    def _symbols_for(self, ticker: str) -> set[str]:
        """Return every provider spelling ``ticker`` could be cached under."""
        canonical = canonicalize_or_fallback(
            ticker,
            self._aliases,
            logger=logger,
            context="snapshot repair historical price lookup",
        )
        return matching_raw_tickers(canonical, self._aliases) | {ticker}

    def _dated_rate(self, pair: str, as_of: str) -> float | None:
        """Return a stored rate for the exact ``(pair, as_of)``, or None.

        Never falls back to a nearby date -- unlike
        ``PortfolioService.historical_fx_rates``, whose seven-day
        nearest-prior-day search would silently price a holding off the
        wrong day's rate. Tries ``fx_quotes`` then ``fx_rate_cache`` (both
        same-day-opportunistic) and finally ``historical_price_cache`` --
        the full ``GBPUSD=X`` series backfilled by ``ensure_fx_coverage``
        (#496); only that one pair is ever backfilled there, so this third
        tier resolves USD holdings only, same as the other two.
        """
        quote = self._fx_quotes.get_for_pair_and_date(pair, as_of)
        if quote is not None:
            rate = valid_rate_or_none(float(quote.rate))
            if rate is not None:
                return rate
        cached = self._fx_cache.get_many([as_of], pair)
        rate = valid_rate_or_none(cached.get(as_of))
        if rate is not None:
            return rate
        observed = self._prices.dated_close([pair], as_of)
        if observed is None:
            return None
        scale = _positive_float_or_none(observed.quote_unit_scale)
        close = _positive_float_or_none(observed.close)
        if scale is None or close is None:
            return None
        return valid_rate_or_none(close * scale)


def _positive_float_or_none(value: str | float) -> float | None:
    """Parse a stored number into a finite positive float, or None."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None
