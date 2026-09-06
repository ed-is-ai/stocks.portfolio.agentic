"""Portfolio service — valuation, GBP/USD conversion, and chart data.

Owns the pricing/valuation logic that previously lived as helper functions in
``web/app.py``: live price fetching, GBP normalisation, portfolio history, trade
markers, and the aggregate GBP totals shown on the summary cards. Returns plain
context dicts; rendering stays in the API layer.
"""

import csv
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, localcontext
from functools import lru_cache
from typing import Any, ClassVar, cast

from app.agents.analyst.exit_evaluator import ExitEvaluator
from app.core.config import ANALYSIS_JSON, PORTFOLIO_VALUE_CSV, TRADES_DB
from app.core.money import Money
from app.core.ticker_identity import canonicalize_or_fallback, load_aliases
from app.repositories import db
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.schemas.analysis_artifact import read_analysis_records
from app.schemas.portfolio_import import ProviderOption
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.gbp_valuation_service import GbpValuationService
from app.services.portfolio_import.contract_registry import ContractRegistryError
from app.services.portfolio_import.registry_loader import get_contract_registry
from app.services.series_downsample import downsample_last_per_bucket
from app.services.snapshot_valuation import amount_in_gbp
from app.services.strategy_assignment_service import StrategyAssignmentService
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

_DEFAULT_GBPUSD = 1.35
_GBPUSD_CACHE_TTL_SECONDS = 60.0


def _load_provider_options() -> tuple[ProviderOption, ...]:
    """Best-effort read of the import-provider dropdown metadata.

    A broken/misconfigured contracts directory should never take down an
    otherwise-working portfolio page render -- mirrors this module's
    existing best-effort conventions (e.g. price-refresh failures still
    render cached data) rather than letting an unrelated feature's startup
    problem surface as a raw 500 on every page load.
    """
    try:
        return get_contract_registry().list_providers()
    except ContractRegistryError:
        logger.exception("Failed to load import provider options")
        return ()


_HISTORICAL_FX_PAIR: dict[str, str] = {
    "USD": "GBPUSD=X",
    "HKD": "GBPHKD=X",
}
_YFINANCE_SYMBOL_OVERRIDES: dict[str, str] = {"9988": "9988.HK"}
_PRICE_DOWNLOAD_CHUNK_SIZE = 50


class _CashBalanceUnset:
    """Sentinel distinguishing "caller passed no cash_balance" from a real
    ``float | None`` value -- a plain ``object()`` narrows via ``is`` to
    itself but not away from the parameter's declared type, so every
    ``else`` branch below stayed typed as ``float | None | object``.
    """


_CASH_BALANCE_UNSET = _CashBalanceUnset()

# Portfolio value-history chart range presets (#421): preset -> day count
# cutoff, measured back from now. Any other/absent value resolves to 12M.
# Values are approximate calendar spans (30d, ~13 weeks, 1 year); the 3Y/5Y
# presets carry a leap-day allowance (3 * 365 + 1, 5 * 365 + 1) so a full
# calendar span is never clipped a day short.
CHART_RANGE_DAYS: dict[str, int] = {
    "1M": 30,
    "3M": 91,
    "12M": 365,
    "3Y": 1096,
    "5Y": 1826,
}
DEFAULT_CHART_RANGE = "12M"
_CHART_MAX_POINTS = 250


@dataclass(frozen=True)
class PortfolioInputSnapshot:
    """Inputs read once while building a single portfolio render context.

    This deliberately is not a cache: callers create one snapshot for one
    response only, so subsequent requests always observe the current ledger.
    """

    analysis_records: list[StockRecord]
    portfolios: list[Any]
    chart_data: dict[str, list]
    trades: list[Any]
    cash_flows: list[Any]
    reconciliation_issue_count: int
    cash_balances: list[tuple[str, Any, str | None]]
    cash_balance: float | None


class PortfolioService:
    """Valuation and presentation-data builder for the portfolio views."""

    _gbpusd_cache_rate: ClassVar[float | None] = None
    _gbpusd_cache_fetched_at: ClassVar[float | None] = None
    _gbpusd_refreshing: ClassVar[bool] = False
    _gbpusd_cache_generation: ClassVar[int] = 0
    _gbpusd_cache_condition: ClassVar[threading.Condition] = threading.Condition()

    def __init__(
        self,
        trader: TraderService,
        evaluator: ExitEvaluator | None = None,
        gbp_valuation: GbpValuationService | None = None,
        assignment_service: StrategyAssignmentService | None = None,
    ) -> None:
        self._trader = trader
        self._evaluator = evaluator or ExitEvaluator()
        self._gbp_valuation = gbp_valuation or GbpValuationService(
            FxQuoteRepository(db.make_connect(lambda: TRADES_DB))
        )
        # Optional Strategy-assignment seam (#440). None keeps pre-#440
        # behaviour exactly: the template's strategy keys render as None.
        self._assignment_service = assignment_service

    # --- analysis + aliases ----------------------------------------------

    def load_analysis(self) -> list[StockRecord]:
        """Load latest analysis results, returning empty list on any error.

        Supports both the current self-describing artifact envelope
        (``{"meta": ..., "records": [...]}``) and a legacy bare JSON list,
        via ``read_analysis_records``.

        Individual records that fail validation are skipped rather than
        discarding the whole set — one malformed row (e.g. a null price from a
        bad market-data bar) must not blank out the entire watchlist.
        """
        try:
            data = read_analysis_records(ANALYSIS_JSON)
        except Exception:
            return []
        records: list[StockRecord] = []
        for row in data:
            try:
                records.append(StockRecord.model_validate(row))
            except Exception:
                continue
        return records

    @staticmethod
    def current_prices(records: list[StockRecord]) -> dict[str, float]:
        """Return {ticker: price} from analysis records."""
        return {r.ticker: r.price for r in records}

    def load_ticker_aliases(self) -> dict[str, str]:
        """Load internal-ticker → Yahoo Finance symbol mappings.

        Delegates to ``app.core.ticker_identity.load_aliases()`` -- the
        single source of truth for alias data shared with ``TraderAgent``
        and ``RealisedPnlService`` -- while keeping this method's exact
        signature so existing bound-method monkeypatches keep working.
        """
        return load_aliases()

    # --- FX ---------------------------------------------------------------

    @staticmethod
    def _fetch_gbpusd_rate() -> float | None:
        """Fetch live GBP/USD rate from yfinance; return None on failure."""
        try:
            import yfinance as yf

            last_price = yf.Ticker("GBPUSD=X").fast_info.last_price
            if last_price is None:
                return None
            return float(cast(Any, last_price))
        except Exception:
            return None

    def reset_gbpusd_cache(self) -> None:
        """Clear the process-local live cache without touching persistence."""
        cls = PortfolioService
        with cls._gbpusd_cache_condition:
            cls._gbpusd_cache_rate = None
            cls._gbpusd_cache_fetched_at = None
            cls._gbpusd_cache_generation += 1
            cls._gbpusd_cache_condition.notify_all()

    def _cached_gbpusd_rate(self, now: float) -> float | None:
        cls = PortfolioService
        rate = self._valid_rate_or_none(cls._gbpusd_cache_rate)
        if (
            rate is not None
            and cls._gbpusd_cache_fetched_at is not None
            and now - cls._gbpusd_cache_fetched_at < _GBPUSD_CACHE_TTL_SECONDS
        ):
            return rate
        return None

    def _persisted_or_default_gbpusd_rate(self) -> float:
        try:
            cached, _, _disp = self._trader.load_price_cache()
        except Exception:
            logger.exception("Failed to load persisted GBPUSD rate")
            cached = {}
        persisted_rate = self._valid_rate_or_none(cached.get("__GBPUSD__"))
        if persisted_rate is not None:
            logger.warning("GBPUSD rate fetch failed; using cached rate")
            return persisted_rate
        logger.warning("GBPUSD rate unavailable; defaulting to %s", _DEFAULT_GBPUSD)
        return _DEFAULT_GBPUSD

    def gbpusd_rate(self) -> float:
        """Return a single-flight cached live GBP/USD rate or durable fallback."""
        cls = PortfolioService
        use_durable_fallback = False
        refresh_generation = cls._gbpusd_cache_generation
        with cls._gbpusd_cache_condition:
            cached_rate = self._cached_gbpusd_rate(time.monotonic())
            if cached_rate is not None:
                return cached_rate
            if cls._gbpusd_refreshing:
                cls._gbpusd_cache_condition.wait_for(lambda: not cls._gbpusd_refreshing)
                cached_rate = self._cached_gbpusd_rate(time.monotonic())
                if cached_rate is not None:
                    return cached_rate
                use_durable_fallback = True
            else:
                cls._gbpusd_refreshing = True
                refresh_generation = cls._gbpusd_cache_generation

        if use_durable_fallback:
            return self._persisted_or_default_gbpusd_rate()

        try:
            try:
                rate = self._valid_rate_or_none(self._fetch_gbpusd_rate())
            except Exception:
                logger.exception("GBPUSD rate fetch raised unexpectedly")
                rate = None
            if rate is None:
                return self._persisted_or_default_gbpusd_rate()
            published = False
            with cls._gbpusd_cache_condition:
                if refresh_generation == cls._gbpusd_cache_generation:
                    cls._gbpusd_cache_rate = rate
                    cls._gbpusd_cache_fetched_at = time.monotonic()
                    published = True
                cls._gbpusd_refreshing = False
                cls._gbpusd_cache_condition.notify_all()
            if published:
                try:
                    self._trader.save_price_cache({"__GBPUSD__": rate})
                except Exception:
                    logger.exception("Failed to persist live GBPUSD rate")
            return rate
        finally:
            with cls._gbpusd_cache_condition:
                if cls._gbpusd_refreshing:
                    cls._gbpusd_refreshing = False
                    cls._gbpusd_cache_condition.notify_all()

    @staticmethod
    def _to_gbp(amount: float, currency: str, gbpusd: float) -> float:
        """Convert amount to GBP using current rate if currency is USD."""
        if currency == "USD":
            return amount / gbpusd
        if currency == "GBp":
            return amount / 100
        return amount

    def _amount_in_gbp(
        self, amount: float, currency: str, gbpusd: float | None
    ) -> float | None:
        """Value one native holding amount in GBP, never fabricating FX.

        Delegates to :func:`app.services.snapshot_valuation.amount_in_gbp` so
        the live summary cards and the stored value-history snapshots share
        exactly one conversion rule. ``gbpusd`` may be ``None`` when no rate
        was supplied; ``amount_in_gbp`` already treats that as "no USD
        valuation" rather than guessing.
        """
        return amount_in_gbp(amount, currency, gbpusd, self._gbp_valuation)

    @staticmethod
    def _valid_rate_or_none(rate: Any) -> float | None:
        """Return a finite positive rate, rejecting malformed cache contents."""
        if isinstance(rate, (bool, str, bytes)):
            return None
        try:
            numeric_rate = float(rate)
        except (TypeError, ValueError, OverflowError):
            return None
        return (
            numeric_rate if math.isfinite(numeric_rate) and numeric_rate > 0 else None
        )

    @staticmethod
    def _is_valid_rate(rate: Any) -> bool:
        """Return True for a usable rate: not None and strictly positive.

        A cached or freshly-fetched rate of ``0``, negative, or ``None`` is
        always treated as unavailable, never fed into a conversion (AC7).
        """
        return PortfolioService._valid_rate_or_none(rate) is not None

    def historical_gbpusd_rates(self, dates: list[str]) -> dict[str, float]:
        """Compatibility wrapper for historical GBP/USD rates."""
        return self.historical_fx_rates("USD", dates)

    def historical_fx_rates(self, currency: str, dates: list[str]) -> dict[str, float]:
        """Return trade-date rates for a supported currency against GBP.

        Rates use the ``GBP<currency>=X`` orientation: units of the foreign
        currency per GBP. Cache identity includes both pair and date so
        same-date rates for USD and HKD can never collide.

        A date absent from the returned dict means the rate could not be
        resolved — never a ``0.0``/negative sentinel. A cached row holding
        an already-invalid value (``0``/negative/``NULL``) is treated as
        unavailable and excluded, but is *not* retried against ``yfinance``
        (the cache is authoritative for a date already attempted).
        """
        pair = _HISTORICAL_FX_PAIR.get(currency.upper())
        if pair is None:
            return {}
        unique_dates = sorted(set(dates))
        if not unique_dates:
            return {}
        cached = self._trader.get_cached_fx_rates(unique_dates, pair)
        result: dict[str, float] = {}
        missing: list[str] = []
        for d in unique_dates:
            if d not in cached:
                missing.append(d)
                continue
            rate = cached[d]
            if self._is_valid_rate(rate):
                result[d] = round(rate, 4)
        if missing:
            fetched = (
                self._fetch_historical_gbpusd(missing)
                if pair == "GBPUSD=X"
                else self._fetch_historical_fx(pair, missing)
            )
            valid_fetched = {
                d: round(rate, 4)
                for d, rate in fetched.items()
                if self._is_valid_rate(rate)
            }
            # A date still unresolved after the full 7-day search (e.g. a
            # trade older than the whole available FX series) is cached as
            # a known-invalid sentinel too, not left un-cached -- otherwise
            # it would trigger a fresh `yfinance` download on every single
            # future call forever, defeating NFR1's per-calendar-date
            # caching intent. Reuses the same "cached invalid = never
            # refetched" mechanism already used for a bad stored value.
            unresolved = {d: -1.0 for d in missing if d not in valid_fetched}
            to_persist = {**valid_fetched, **unresolved}
            if to_persist:
                self._trader.save_fx_rates(to_persist, pair)
            result.update(valid_fetched)
        return result

    @staticmethod
    def _fetch_historical_gbpusd(dates: list[str]) -> dict[str, float]:
        """Compatibility wrapper for the GBP/USD historical fetch seam."""
        return PortfolioService._fetch_historical_fx("GBPUSD=X", dates)

    @staticmethod
    def _fetch_historical_fx(pair: str, dates: list[str]) -> dict[str, float]:
        """Batch-fetch one GBP/foreign FX pair via one ranged download.

        One ``yfinance`` call covers the full date range (plus a 7-day
        lookback margin), reusing ``_fetch_price_gbp``'s MultiIndex-safe
        column-access idiom. For each requested date, an exact match in the
        downloaded series is used first; otherwise the nearest earlier date
        present in the series is used, searching back up to 7 calendar days.
        A date with no rate anywhere in that window (including one older
        than the whole series) is simply omitted — no exception, no
        sentinel.
        """
        import yfinance as yf

        if not dates:
            return {}
        parsed = sorted(date.fromisoformat(d) for d in dates)
        start = parsed[0] - timedelta(days=7)
        end = parsed[-1] + timedelta(days=1)
        try:
            data = yf.download(
                pair, start=start, end=end, progress=False, auto_adjust=True
            )
        except Exception:
            # yfinance is an unofficial, unsupported scraping library --
            # a network/rate-limit failure here must degrade every
            # requested date to unresolved (fx_unavailable downstream),
            # matching `_fetch_gbpusd_rate()`'s existing same-style
            # try/except, never crash the whole Account's computation.
            logger.warning(
                "historical FX fetch failed for %s on %s", pair, dates, exc_info=True
            )
            return {}
        if data.empty:
            return {}
        close = data["Close"] if "Close" in data.columns else data.iloc[:, 0]
        series = close.iloc[:, 0] if hasattr(close, "columns") else close
        series = series.dropna()
        if series.empty:
            return {}
        by_date: dict[str, float] = {}
        for idx, val in series.items():
            key = (
                idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            )
            by_date[key] = float(val)

        result: dict[str, float] = {}
        for d in dates:
            target = date.fromisoformat(d)
            for offset in range(8):  # exact match (0) plus up to 7 days prior
                candidate = (target - timedelta(days=offset)).isoformat()
                if candidate in by_date:
                    result[d] = by_date[candidate]
                    break
        return result

    @lru_cache(maxsize=1024)
    def ticker_currency(self, ticker: str) -> str:
        """Return a ticker's trading currency (e.g. ``"GBP"``, ``"USD"``).

        The sole seam (AD-5) for resolving a ticker's currency, reusing the
        same ``yf.Ticker(...).fast_info.currency`` classification
        ``_fetch_price_gbp`` already applies to current positions
        (``GBp`` normalised to ``GBP``, defaulting to ``GBP`` on any
        failure). Resolved through the same multi-hop
        ``canonicalize_or_fallback`` walk ``fetch_all_prices``'s
        ``_resolve`` uses (not a one-hop ``aliases.get``), so a canonical
        ticker fed back in (e.g. from ``get_portfolio()``'s output) still
        resolves correctly.
        """
        import yfinance as yf

        aliases = {**self.load_ticker_aliases(), **_YFINANCE_SYMBOL_OVERRIDES}
        yf_sym = canonicalize_or_fallback(
            ticker,
            aliases,
            logger=logger,
            context="ticker_currency",
        )
        try:
            currency = yf.Ticker(yf_sym).fast_info.currency
            if not currency and yf_sym.upper().endswith(".HK"):
                currency = "HKD"
            return self._quote_currency(currency or "GBP")
        except Exception:
            if yf_sym.upper().endswith(".HK"):
                logger.warning(
                    "Could not determine currency for %s; inferring HKD from %s",
                    ticker,
                    yf_sym,
                )
                return "HKD"
            logger.warning(
                "Could not determine currency for %s; defaulting to GBP", ticker
            )
            return "GBP"

    def ticker_currencies(self, tickers: list[str]) -> dict[str, str]:
        """Resolve currencies in bulk, preferring persisted price metadata.

        Realised P&L can contain many historical tickers. Resolving each one
        serially through Yahoo on every render made the tab's latency scale
        with the size of the account instead of the local FIFO calculation.

        A ticker no longer in the live price-scan cache (delisted, or sold
        long enough ago to have dropped off it) falls through to a second,
        durable cache (``ticker_currency_cache``, keyed only by ticker) before
        paying for a live Yahoo lookup -- that lookup's in-process
        ``@lru_cache`` on ``ticker_currency`` resets on every server restart,
        so without this a delisted ticker re-pays its (failing) network
        lookup on every restart forever.
        """
        unique = tuple(dict.fromkeys(tickers))
        if not unique:
            return {}

        _prices, _fetched_at, display_info = self._trader.load_price_cache()
        resolved = {
            ticker: self._trading_currency(display_info[ticker][1])
            for ticker in unique
            if ticker in display_info and display_info[ticker][1]
        }
        missing = [ticker for ticker in unique if ticker not in resolved]
        if not missing:
            return resolved

        cached = self._trader.get_cached_ticker_currencies(missing)
        resolved.update(
            {
                ticker: self._trading_currency(currency)
                for ticker, currency in cached.items()
            }
        )
        still_missing = [ticker for ticker in missing if ticker not in cached]
        if not still_missing:
            return resolved

        worker_count = min(8, len(still_missing))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            currencies = executor.map(self.ticker_currency, still_missing)
            newly_resolved = dict(zip(still_missing, currencies, strict=True))
        resolved.update(newly_resolved)
        self._trader.save_ticker_currencies(newly_resolved)
        return resolved

    @staticmethod
    def _quote_currency(currency: object) -> str:
        """Normalise a provider quote unit without losing LSE pence case."""
        value = str(currency).strip()
        return (
            "GBp"
            if value.lower() == "gbp" and value != value.upper()
            else value.upper()
        )

    @staticmethod
    def _trading_currency(currency: object) -> str:
        """Map a quote unit to its ISO trading currency for realised P&L."""
        return (
            "GBP"
            if PortfolioService._quote_currency(currency) == "GBp"
            else PortfolioService._quote_currency(currency)
        )

    def _price_quote_currencies(
        self, tickers: list[str], symbols: dict[str, str]
    ) -> dict[str, str]:
        """Resolve quote units without N per-symbol provider metadata calls.

        yfinance ``download`` does not reliably expose per-symbol currency.
        Reuse persisted display/durable metadata first, then use Yahoo's
        provider-symbol conventions for cold misses: ``.L`` is pence,
        ``.HK`` is HKD, and unsuffixed Yahoo equities are USD.  The inferred
        unit is persisted so it is paid once and can be corrected by a later
        successful provider display payload.
        """
        _prices, _fetched_at, display_info = self._trader.load_price_cache()
        resolved = {
            ticker: self._quote_currency(display_info[ticker][1])
            for ticker in tickers
            if ticker in display_info and display_info[ticker][1]
        }
        missing = [ticker for ticker in tickers if ticker not in resolved]
        if missing:
            cached = self._trader.get_cached_ticker_currencies(missing)
            resolved.update(
                {
                    ticker: self._quote_currency(currency)
                    for ticker, currency in cached.items()
                }
            )
        inferred = {
            ticker: (
                "GBp"
                if symbols[ticker].upper().endswith(".L")
                else "HKD"
                if symbols[ticker].upper().endswith(".HK")
                else "USD"
            )
            for ticker in tickers
            if ticker not in resolved
        }
        if inferred:
            self._trader.save_ticker_currencies(inferred)
            resolved.update(inferred)
        return resolved

    # --- live price fetching ---------------------------------------------

    @staticmethod
    def _latest_close(data: Any, symbol: str, batch_size: int) -> float | None:
        """Extract one symbol's latest usable close from yfinance output.

        yfinance returns a flat frame for a single-symbol download and a
        MultiIndex frame for batches.  It has also changed the MultiIndex
        level order between releases, so select the ``Close`` level by name
        rather than assuming a particular order.
        """
        if data is None or data.empty:
            return None
        try:
            columns = data.columns
            if getattr(columns, "nlevels", 1) > 1:
                close_columns = [
                    column
                    for column in columns
                    if "Close" in column and symbol in column
                ]
                if not close_columns:
                    return None
                series = data[close_columns[0]]
            elif batch_size == 1:
                series = data["Close"] if "Close" in columns else data.iloc[:, 0]
            else:
                # A batch response without per-ticker columns cannot safely
                # be attributed to a holding.
                return None
            series = series.dropna()
            if series.empty:
                return None
            value = float(series.iloc[-1])
            return value if math.isfinite(value) and value > 0 else None
        except Exception:
            logger.warning("Could not extract close for %s", symbol, exc_info=True)
            return None

    @staticmethod
    def _chunked(items: list[str], size: int) -> list[list[str]]:
        return [items[index : index + size] for index in range(0, len(items), size)]

    def _download_closes(self, symbols: list[str]) -> tuple[dict[str, float], set[str]]:
        """Download close prices in bounded chunks, isolating failed chunks."""
        import yfinance as yf

        prices: dict[str, float] = {}
        failed: set[str] = set()
        for chunk in self._chunked(symbols, _PRICE_DOWNLOAD_CHUNK_SIZE):
            try:
                data = yf.download(
                    chunk if len(chunk) > 1 else chunk[0],
                    period="5d",
                    progress=False,
                    auto_adjust=True,
                )
            except Exception:
                logger.warning("Price batch fetch failed for %s", chunk, exc_info=True)
                failed.update(chunk)
                continue
            for symbol in chunk:
                close = self._latest_close(data, symbol, len(chunk))
                if close is None:
                    failed.add(symbol)
                else:
                    prices[symbol] = close
        return prices, failed

    def _price_in_gbp(
        self, original_price: float, currency: str, gbpusd: float
    ) -> float | None:
        """Convert a provider quote unit to GBP without silently guessing FX."""
        quote_currency = currency.strip() or "GBP"
        if quote_currency == "GBp":
            return round(original_price / 100, 4)
        currency = quote_currency.upper()
        if currency == "GBP":
            return round(original_price, 4)
        if currency == "USD":
            return (
                round(original_price / gbpusd, 4)
                if self._is_valid_rate(gbpusd)
                else None
            )
        if currency == "HKD":
            projection = self._gbp_valuation.value_in_gbp(
                Money(amount=Decimal(str(original_price)), currency="HKD")
            )
            return (
                float(projection.gbp_amount)
                if projection.gbp_amount is not None
                else None
            )
        return None

    def fetch_all_prices_with_failures(
        self, tickers: list[str], aliases: dict[str, str], gbpusd: float
    ) -> tuple[dict[str, float], dict[str, tuple[float, str]], set[str]]:
        """Batch-fetch prices and return failures without dropping successes."""
        unique_tickers = list(dict.fromkeys(tickers))
        if not unique_tickers:
            return {}, {}, set()
        symbols = {
            ticker: canonicalize_or_fallback(
                ticker, aliases, logger=logger, context="fetch_all_prices"
            )
            for ticker in unique_tickers
        }
        # Price-cache display metadata and the durable cache are consulted
        # before symbol-convention inference; no cold refresh issues N
        # ``yf.Ticker`` metadata requests.
        currencies = self._price_quote_currencies(unique_tickers, symbols)
        closes, failed_symbols = self._download_closes(
            list(dict.fromkeys(symbols.values()))
        )

        # Only symbols that had no configured alias may safely use the LSE
        # suffix heuristic. Retry all such misses together, never per ticker.
        lse_symbols = list(
            dict.fromkeys(
                f"{ticker}.L"
                for ticker, symbol in symbols.items()
                if symbol == ticker and symbol in failed_symbols
            )
        )
        if lse_symbols:
            fallback_closes, fallback_failed = self._download_closes(lse_symbols)
            closes.update(fallback_closes)
            for ticker, symbol in symbols.items():
                fallback = f"{ticker}.L"
                if symbol == ticker and fallback in fallback_closes:
                    closes[symbol] = fallback_closes[fallback]
                    failed_symbols.discard(symbol)
                    currencies[ticker] = "GBp"
                    self._trader.save_ticker_currencies({ticker: "GBp"})
                elif symbol == ticker and fallback not in fallback_failed:
                    failed_symbols.discard(symbol)

        gbp_prices: dict[str, float] = {}
        display_info: dict[str, tuple[float, str]] = {}
        failures: set[str] = set()
        for ticker, symbol in symbols.items():
            close = closes.get(symbol)
            if close is None:
                failures.add(ticker)
                continue
            currency = currencies.get(ticker, "GBP")
            gbp_price = self._price_in_gbp(close, currency, gbpusd)
            if gbp_price is None or gbp_price < 0.01:
                failures.add(ticker)
                continue
            gbp_prices[ticker] = gbp_price
            display_info[ticker] = (round(close, 2), currency)
        return gbp_prices, display_info, failures

    def fetch_all_prices(
        self, tickers: list[str], aliases: dict[str, str], gbpusd: float
    ) -> tuple[dict[str, float], dict[str, tuple[float, str]]]:
        """Compatibility wrapper for callers that do not need failures."""
        prices, display_info, _failures = self.fetch_all_prices_with_failures(
            tickers, aliases, gbpusd
        )
        return prices, display_info

    # --- headless pricing (shared by the orchestrator's email/snapshot) --

    def get_prices_for_holdings(
        self, tickers: list[str]
    ) -> tuple[dict[str, float], dict[str, tuple[float, str]], float]:
        """Return cache-first GBP prices, display_info, and GBP/USD rate.

        Reads the shared price cache (the same one the web UI reads and
        writes); any ticker missing from it is live-fetched via
        ``fetch_all_prices`` and the result is persisted back to the cache so
        the fetch is never repeated. This is the pricing core used headlessly
        by the orchestrator (no request/DB-session coupling) so the email
        snapshot and the ``/portfolio`` route always agree on price.

        Held tickers already cached cost zero net-new price calls; only the
        misses (e.g. holdings outside the current scan watchlist) trigger a
        live fetch. When nothing is missing, the GBP/USD rate is also taken
        from the cache rather than re-fetched live.
        """
        cached_prices, _, cached_display = self._trader.load_price_cache()
        missing = [t for t in tickers if t not in cached_prices]
        if not missing:
            gbpusd = cached_prices.get("__GBPUSD__", _DEFAULT_GBPUSD)
            return cached_prices, cached_display, gbpusd

        gbpusd = self.gbpusd_rate()
        aliases = self.load_ticker_aliases()
        fetched_prices, fetched_display = self.fetch_all_prices(
            missing, aliases, gbpusd
        )
        if fetched_prices:
            self._trader.save_price_cache(fetched_prices, fetched_display)

        prices = {**cached_prices, **fetched_prices}
        display_info = {**cached_display, **fetched_display}
        return prices, display_info, gbpusd

    def gbp_totals(
        self, positions: list[Position], gbpusd: float
    ) -> tuple[float, float, float]:
        """Return (total_value_gbp, total_cost_gbp, total_pnl_gbp) for positions.

        Converts each position's native-currency ``total_cost``/
        ``current_value`` to GBP before summing, so USD holdings don't
        distort the aggregate the way a naive cross-currency sum would.
        ``total_cost`` includes every position; ``total_value`` only those
        with a known ``current_value`` (unpriced positions are excluded).
        """
        total_cost_gbp = sum(
            amount
            for amount in (
                self._amount_in_gbp(p.total_cost, p.price_currency, gbpusd)
                for p in positions
            )
            if amount is not None
        )
        total_value_gbp = sum(
            amount
            for amount in (
                self._amount_in_gbp(p.current_value, p.price_currency, gbpusd)
                for p in positions
                if p.current_value is not None
            )
            if amount is not None
        )
        total_pnl_gbp = total_value_gbp - total_cost_gbp
        return total_value_gbp, total_cost_gbp, total_pnl_gbp

    # --- chart data -------------------------------------------------------

    @staticmethod
    def chart_cutoff_iso(range_key: str) -> str:
        """Return the ``YYYY-MM-DD`` cutoff for a chart range preset (#421).

        A date-only prefix is deliberate: stored timestamps are compared as
        strings, and a full ``.isoformat()`` with a ``+00:00`` offset can sort
        before an equal-second timestamp because ``'+' < '.'``. The presets
        are day-counts, so a day-granular cutoff is exact.
        """
        days = CHART_RANGE_DAYS.get(range_key, CHART_RANGE_DAYS[DEFAULT_CHART_RANGE])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        return cutoff.isoformat()

    def chart_range_availability(
        self, portfolio_id: int | None, active_range: str
    ) -> dict[str, bool]:
        """Return ``{preset: has_more_data_than_a_narrower_preset}`` (#498).

        Walks presets narrowest to widest: once a preset's cutoff reaches
        back to (or past) the oldest snapshot, its window already contains
        every stored point, so it "fully covers" the history -- and every
        wider preset after it would render that exact same complete
        series, making it redundant. ``1M`` and whichever preset is
        currently selected always stay available; everything else (no
        portfolio, or no stored snapshots yet) is available too, so the
        selector never looks broken before the fact is known.
        """
        if portfolio_id is None:
            return dict.fromkeys(CHART_RANGE_DAYS, True)
        earliest = self._trader.earliest_snapshot_timestamp(portfolio_id)
        if not isinstance(earliest, str):
            return dict.fromkeys(CHART_RANGE_DAYS, True)
        availability: dict[str, bool] = {}
        fully_covers = False
        for preset in CHART_RANGE_DAYS:
            availability[preset] = preset in ("1M", active_range) or not fully_covers
            if self.chart_cutoff_iso(preset) <= earliest:
                fully_covers = True
        return availability

    def _load_portfolio_history(
        self, portfolio_id: int | None = None, range_key: str = DEFAULT_CHART_RANGE
    ) -> dict:
        """Return one aligned, chart-ready projection of retained snapshots.

        With a ``portfolio_id`` the series comes from that portfolio's
        ``portfolio_snapshots`` (#147), filtered to the ``range_key`` window
        and downsampled to ``_CHART_MAX_POINTS`` last-per-bucket points (#421);
        without one it falls back to the legacy single-portfolio
        ``portfolio_value.csv`` (unscoped, unranged).

        ``total_values`` is deliberately projected here rather than in the
        template: each point is the same-row holdings market value plus cash.
        Invalid or unavailable components remain explicit gaps in every
        series, never fabricated zeroes or non-standard JSON numbers.
        """
        if portfolio_id is not None:
            rows = self._trader.snapshot_history(
                portfolio_id, since=self.chart_cutoff_iso(range_key)
            )
            selected_rows = self._chronological_chart_rows(list(rows))
            selected_rows = downsample_last_per_bucket(selected_rows, _CHART_MAX_POINTS)
            return self._project_portfolio_chart_rows(selected_rows)
        if not PORTFOLIO_VALUE_CSV.exists():
            return self._project_portfolio_chart_rows([])
        with open(PORTFOLIO_VALUE_CSV, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        legacy_rows = [
            (
                row.get("timestamp", ""),
                row.get("total_value"),
                row.get("total_cost"),
                row.get("cash_balance"),
            )
            for row in rows
        ]
        legacy_rows = self._chronological_chart_rows(legacy_rows)[-180:]
        legacy_rows = downsample_last_per_bucket(legacy_rows, _CHART_MAX_POINTS)
        return self._project_portfolio_chart_rows(legacy_rows)

    @staticmethod
    def _chart_timestamp_utc(value: Any) -> datetime | None:
        """Parse a retained timestamp and normalize the instant to UTC."""
        try:
            parsed = (
                value
                if isinstance(value, datetime)
                else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _chronological_chart_rows(
        cls, rows: list[tuple[Any, ...]]
    ) -> list[tuple[Any, ...]]:
        """Return valid timestamped rows in true chronological order.

        A timestamp that cannot be placed on the timeline cannot safely enter
        date-bucket downsampling, so it is excluded rather than allowing the
        downsampler to exceed its point ceiling.
        """
        normalized: list[tuple[datetime, tuple[Any, ...]]] = []
        for row in rows:
            if not row:
                continue
            instant = cls._chart_timestamp_utc(row[0])
            if instant is not None:
                normalized.append((instant, row))
        normalized.sort(key=lambda item: item[0])
        return [(instant.isoformat(), *row[1:]) for instant, row in normalized]

    @staticmethod
    def _finite_chart_decimal(value: Any) -> Decimal | None:
        """Normalize one stored chart number without accepting sentinels."""
        if value is None or isinstance(value, (bool, bytes)):
            return None
        if isinstance(value, str) and not value.strip():
            return None
        try:
            numeric = Decimal(str(value))
        except (DecimalException, TypeError, ValueError):
            return None
        return numeric if numeric.is_finite() else None

    @classmethod
    def _project_portfolio_chart_rows(cls, rows: list[tuple[Any, ...]]) -> dict:
        """Project aligned finite series and same-row portfolio totals."""
        values: list[float | None] = []
        costs: list[float | None] = []
        cash_values: list[float | None] = []
        total_values: list[float | None] = []
        estimated: list[bool] = []

        def presentation_value(value: Decimal | None) -> float | None:
            if value is None:
                return None
            try:
                result = float(value)
            except (OverflowError, ValueError):
                return None
            if not math.isfinite(result) or (result == 0.0 and value != 0):
                return None
            return result

        for row in rows:
            market_decimal = cls._finite_chart_decimal(row[1] if len(row) > 1 else None)
            cost_decimal = cls._finite_chart_decimal(row[2] if len(row) > 2 else None)
            cash_decimal = cls._finite_chart_decimal(row[3] if len(row) > 3 else None)
            # A row from before #519 (or a stub without the column) has no
            # flag at all -- absent means observed, never estimated.
            estimated.append(bool(row[4]) if len(row) > 4 else False)
            market_value = presentation_value(market_decimal)
            cash_value = presentation_value(cash_decimal)
            values.append(market_value)
            costs.append(presentation_value(cost_decimal))
            cash_values.append(cash_value)
            if market_value is None or cash_value is None:
                total_values.append(None)
                continue
            # presentation_value only returns non-None for a non-None input,
            # so both decimals are proven present at this point.
            assert market_decimal is not None
            assert cash_decimal is not None
            try:
                # `.exponent` is `int | Literal['n', 'N', 'F']` in general --
                # those string sentinels only occur for NaN/Infinity, which
                # `_finite_chart_decimal` has already excluded from both
                # operands, so it is always a real `int` here.
                least_exponent = min(
                    cast(int, market_decimal.as_tuple().exponent),
                    cast(int, cash_decimal.as_tuple().exponent),
                )
                highest_digit = max(market_decimal.adjusted(), cash_decimal.adjusted())
                with localcontext() as context:
                    context.prec = max(28, highest_digit - least_exponent + 2)
                    total = presentation_value(market_decimal + cash_decimal)
            except DecimalException:
                total = None
            total_values.append(total)

        has_unavailable_totals = any(value is None for value in total_values)
        usable_totals = sum(1 for value in total_values if value is not None)
        usable_values = sum(1 for value in values if value is not None)
        return {
            "labels": [str(row[0])[:16].replace("T", " ") for row in rows],
            "total_values": total_values,
            "values": values,
            "costs": costs,
            "cash_values": cash_values,
            "has_unavailable_totals": has_unavailable_totals,
            "all_totals_unavailable": bool(total_values)
            and all(value is None for value in total_values),
            # Backfilled rows (#502) carry a market value but no cash balance,
            # and Portfolio Value is market + cash -- so it is None for every
            # one of them while Market Value is populated. Portfolio Value is
            # the chart's only non-hidden series, so without this the user
            # sees a near-empty chart sitting on top of years of real data
            # (#512). Signals the template to reveal Market Value too.
            "market_value_extends_further": usable_values > usable_totals,
            # Aligned with ``labels``: True where at least one holding in that
            # snapshot was carried at cost rather than priced (#519), so the
            # chart can mark the point instead of presenting it as observed.
            "estimated": estimated,
            "has_estimated_values": any(estimated),
        }

    def _trade_markers(
        self,
        chart_data: dict,
        portfolio_id: int | None = None,
        trades: list[Any] | None = None,
    ) -> tuple[list, list, list, list]:
        """Return (buy_values, sell_values, buy_labels, sell_labels) aligned to labels.

        Each array is len(labels) long with None at positions that have no trade.
        buy/sell_labels are tooltip strings for each non-None entry.
        """
        labels = chart_data["labels"]
        # Anchor markers to the *displayed* Portfolio Value series, not the
        # hidden Market Value one: a marker must vanish along with the point
        # it would otherwise float above (#466).
        values = chart_data["total_values"]
        n = len(labels)
        if n == 0:
            # No value-history snapshots yet (e.g. a freshly created portfolio
            # with trades but no chart points) — nothing to anchor markers to.
            return [], [], [], []

        label_dates = []
        for lbl in labels:
            try:
                label_dates.append(datetime.strptime(lbl[:10], "%Y-%m-%d").date())
            except ValueError:
                label_dates.append(None)

        buy_vals: list = [None] * n
        sell_vals: list = [None] * n
        buy_tips: list = [None] * n
        sell_tips: list = [None] * n

        if trades is None:
            trades = self._trader.get_trade_history(portfolio_id=portfolio_id)
        for trade in sorted(trades, key=lambda t: t.date):
            try:
                td = datetime.strptime(trade.date, "%Y-%m-%d").date()
            except ValueError:
                continue
            # Find nearest label index by calendar distance
            best_idx, best_diff = 0, 10**9
            for i, ld in enumerate(label_dates):
                if ld is None:
                    continue
                diff = abs((ld - td).days)
                if diff < best_diff:
                    best_diff, best_idx = diff, i
            tip = (
                f"{trade.action} {trade.shares:g} {trade.ticker} "
                f"@ ${trade.price:.2f} ({trade.date})"
            )
            if trade.action == "BUY":
                buy_vals[best_idx] = values[best_idx]
                buy_tips[best_idx] = tip
            else:
                sell_vals[best_idx] = values[best_idx]
                sell_tips[best_idx] = tip

        return buy_vals, sell_vals, buy_tips, sell_tips

    # --- context builders -------------------------------------------------

    def portfolio_input_snapshot(
        self,
        portfolio_id: int | None,
        *,
        analysis_records: list[StockRecord] | None = None,
        portfolios: list[Any] | None = None,
        cash_balance: float | None | _CashBalanceUnset = _CASH_BALANCE_UNSET,
        range_key: str = DEFAULT_CHART_RANGE,
    ) -> PortfolioInputSnapshot:
        """Read all mutable inputs needed by one portfolio partial once.

        ``range_key`` selects the value-history window for the chart (#421).
        """
        if portfolio_id is None:
            return PortfolioInputSnapshot(
                analysis_records=analysis_records
                if analysis_records is not None
                else self.load_analysis(),
                portfolios=portfolios
                if portfolios is not None
                else self._trader.list_portfolios(),
                chart_data=self._load_portfolio_history(),
                # Legacy unscoped refreshes render aggregate chart markers too.
                # Preserve that output while still loading the history once.
                trades=self._trader.get_trade_history(),
                cash_flows=[],
                reconciliation_issue_count=0,
                cash_balances=[],
                cash_balance=(
                    self._trader.get_cash_balance()
                    if isinstance(cash_balance, _CashBalanceUnset)
                    else cash_balance
                ),
            )

        trades = self._trader.get_trade_history(portfolio_id=portfolio_id)
        return PortfolioInputSnapshot(
            analysis_records=analysis_records
            if analysis_records is not None
            else self.load_analysis(),
            portfolios=portfolios
            if portfolios is not None
            else self._trader.list_portfolios(),
            chart_data=self._load_portfolio_history(portfolio_id, range_key),
            trades=trades,
            cash_flows=self._trader.get_cash_flows(portfolio_id),
            reconciliation_issue_count=len(
                self._trader.list_reconciliation_issues(portfolio_id)
            ),
            cash_balances=self._trader.list_cash_balances(portfolio_id),
            # A caller that already read cash for its response (notably a
            # successful import) must not make this snapshot depend on a
            # second mutable-ledger read.
            cash_balance=(
                self._trader.get_cash_balance(portfolio_id)
                if isinstance(cash_balance, _CashBalanceUnset)
                else cash_balance
            ),
        )

    def with_current_chart_data(
        self, snapshot: PortfolioInputSnapshot, portfolio_id: int | None
    ) -> PortfolioInputSnapshot:
        """Refresh chart history after a route persists a new value snapshot."""
        return replace(snapshot, chart_data=self._load_portfolio_history(portfolio_id))

    def positions_from_input_snapshot(
        self,
        snapshot: PortfolioInputSnapshot,
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Calculate positions from the exact trade read in ``snapshot``."""
        return self._trader.get_portfolio_from_trades(
            snapshot.trades, current_prices, display_info
        )

    def portfolio_partial_context(
        self,
        positions: list[Position],
        prices_as_of: str | None = None,
        gbpusd_rate: float | None = None,
        cash_balance: float | None | _CashBalanceUnset = _CASH_BALANCE_UNSET,
        error_message: str | None = None,
        warning_message: str | None = None,
        portfolio_id: int | None = None,
        input_snapshot: PortfolioInputSnapshot | None = None,
        range_key: str = DEFAULT_CHART_RANGE,
    ) -> dict:
        """Build the template context for the portfolio partial.

        Enriches positions with exit signals/next pivots, computes GBP-equivalent
        summary totals, and serialises chart data. Rendering stays in the route.
        """
        snapshot = input_snapshot or self.portfolio_input_snapshot(
            portfolio_id, cash_balance=cash_balance, range_key=range_key
        )
        # The normal render derives its cash from the same snapshot as the
        # chart, trades, and positions. An explicitly supplied value wins for
        # backwards-compatible callers such as import responses.
        effective_cash_balance = (
            snapshot.cash_balance
            if isinstance(cash_balance, _CashBalanceUnset)
            else cash_balance
        )
        records = snapshot.analysis_records
        analysis_map = {r.ticker: r for r in records}
        for pos in positions:
            stock = analysis_map.get(pos.ticker)
            pos.exit_signal = self._evaluator.evaluate(pos, stock)
            if stock and stock.analysis:
                pos.next_pivot = stock.analysis.entry_price

        # Compute GBP-equivalent totals for summary cards. ``total_value_gbp``
        # remains the authoritative cash-inclusive portfolio total; the
        # dashboard consumes the separate market-only projection below.
        fx = gbpusd_rate or _DEFAULT_GBPUSD
        valued_costs = [
            self._amount_in_gbp(p.total_cost, p.price_currency, fx) for p in positions
        ]
        total_cost_gbp = sum(amount for amount in valued_costs if amount is not None)
        if effective_cash_balance is not None:
            total_cost_gbp += effective_cash_balance
        positions_with_value = [p for p in positions if p.current_value is not None]
        valued_values = [
            self._amount_in_gbp(p.current_value, p.price_currency, fx)  # type: ignore[arg-type]
            for p in positions_with_value
        ]
        market_value_gbp = sum(amount for amount in valued_values if amount is not None)
        total_value_gbp = market_value_gbp
        if effective_cash_balance is not None:
            total_value_gbp += effective_cash_balance
        total_cost_gbp_valued = sum(
            amount
            for amount in (
                self._amount_in_gbp(p.total_cost, p.price_currency, fx)
                for p in positions_with_value
            )
            if amount is not None
        )
        total_pnl_gbp = (
            total_value_gbp - total_cost_gbp_valued - (effective_cash_balance or 0)
        )

        # GH-484: per-position GBP equivalents for the holdings table. Built
        # as a parallel dict keyed by ticker — ``Position`` models are never
        # mutated here. Only non-GBP rows get entries: ``GBp``/``GBX`` are
        # already normalised to ``"GBP"`` before a ``Position`` is ever built
        # (``TraderAgent.get_portfolio``), so they never actually reach this
        # code as anything but ``"GBP"`` — the membership check is defensive,
        # not doing real work — and a "≈ £" echo for a pounds position would
        # be noise either way. USD uses the caller's rate verbatim — never
        # the legacy default the aggregate cards above fall back to — and
        # other currencies use the same-day GbpValuationService quote;
        # either way an unavailable conversion yields None and the template
        # omits the figure rather than fabricating one.
        position_gbp_values: dict[str, dict[str, float | None]] = {}
        for pos in positions:
            if pos.price_currency in {"GBP", "GBp", "GBX"}:
                continue
            entry: dict[str, float | None] = {}
            if pos.current_value is not None:
                entry["market_value_gbp"] = self._amount_in_gbp(
                    pos.current_value, pos.price_currency, gbpusd_rate
                )
            if pos.unrealised_pnl is not None:
                entry["unrealised_pnl_gbp"] = self._amount_in_gbp(
                    pos.unrealised_pnl, pos.price_currency, gbpusd_rate
                )
            if entry:
                position_gbp_values[pos.ticker] = entry

        chart_data = snapshot.chart_data
        # A trade older than the selected window shows no marker (#421); an
        # in-window trade whose snapshot was downsampled out still snaps to
        # the nearest retained label (existing ``_trade_markers`` behaviour).
        marker_trades = snapshot.trades
        if portfolio_id is not None:
            cutoff_date = self.chart_cutoff_iso(range_key)[:10]
            marker_trades = [t for t in snapshot.trades if t.date >= cutoff_date]
        buy_vals, sell_vals, buy_tips, sell_tips = self._trade_markers(
            chart_data, portfolio_id, marker_trades
        )
        chart_range = (
            range_key if range_key in CHART_RANGE_DAYS else DEFAULT_CHART_RANGE
        )
        chart_has_history = (
            len(self._trader.snapshot_history(portfolio_id, limit=2)) >= 2
            if portfolio_id is not None
            else len(chart_data["values"]) >= 2
        )
        # Always attach the account switcher metadata so any render of the
        # partial (trade, import, refresh, quick-add) keeps the selector (#147).
        portfolios = snapshot.portfolios
        active_portfolio = next((p for p in portfolios if p.id == portfolio_id), None)
        # Cash-flow ledger for the selected account — read-only activity view
        # (#161). The balance still comes from the provider Running Balance.
        cash_flows = snapshot.cash_flows
        # Story 1.5, AC5: a count-only pointer to the dedicated
        # reconciliation view -- the detail lives there, not here.
        reconciliation_issue_count = snapshot.reconciliation_issue_count
        # Story 1.6, AC3: every currency this portfolio holds a cash
        # balance in, each paired with a GBP Valuation Projection -- never
        # a fabricated GBP figure. Additive alongside the legacy GBP-only
        # `cash_balance` scalar above, not a replacement for it.
        cash_balances_by_currency = (
            [
                {
                    "currency": currency,
                    "amount": amount,
                    "as_of": as_of,
                    "projection": self._gbp_valuation.value_in_gbp(
                        Money(amount=amount, currency=currency)
                    ),
                }
                for currency, amount, as_of in snapshot.cash_balances
            ]
            if portfolio_id is not None
            else []
        )
        # Story 2.4, AC6: every ticker with at least one manually-entered
        # Opening Lot (source="opening_lot") in this portfolio -- lets the
        # holdings table show a "Manually entered" indicator even though a
        # ``Position`` is an aggregate over possibly-several trades, not
        # one row this schema can tag itself.
        opening_lot_tickers = (
            {t.ticker for t in snapshot.trades if t.source == "opening_lot"}
            if portfolio_id is not None
            else set()
        )
        # Strategy assignment chip + scan-freshness banner (#440). None-safe:
        # without an assignment service (or with no assignment) both keys are
        # None and rendering is unchanged apart from the new control.
        assignment_service = self._assignment_service
        strategy_assignment = (
            assignment_service.assignment_view(portfolio_id)
            if assignment_service is not None and portfolio_id is not None
            else None
        )
        strategy_freshness = (
            assignment_service.freshness() if assignment_service else None
        )
        return {
            "positions": positions,
            "portfolio_id": portfolio_id,
            "portfolios": portfolios,
            "active_portfolio": active_portfolio,
            # Story 3.3: sourced from the same cached registry singleton
            # every render of the import form needs -- never absent, so the
            # template can always render the provider/account-type selects.
            "provider_options": _load_provider_options(),
            "cash_flows": cash_flows,
            "opening_lot_tickers": opening_lot_tickers,
            "strategy_assignment": strategy_assignment,
            "strategy_freshness": strategy_freshness,
            "reconciliation_issue_count": reconciliation_issue_count,
            "cash_balances_by_currency": cash_balances_by_currency,
            "positions_with_value": positions_with_value,
            "total_cost_gbp": total_cost_gbp,
            "total_value_gbp": total_value_gbp,
            "market_value_gbp": market_value_gbp,
            "total_pnl_gbp": total_pnl_gbp,
            "total_cost_gbp_valued": total_cost_gbp_valued,
            "position_gbp_values": position_gbp_values,
            "cash_balance": effective_cash_balance,
            "prices_as_of": prices_as_of,
            "gbpusd_rate": gbpusd_rate,
            "error_message": error_message,
            "warning_message": warning_message,
            "chart_labels": json.dumps(chart_data["labels"]),
            "chart_total_values": json.dumps(chart_data["total_values"]),
            "chart_values": json.dumps(chart_data["values"]),
            "chart_costs": json.dumps(chart_data["costs"]),
            "chart_cash": json.dumps(chart_data["cash_values"]),
            "chart_has_unavailable_totals": chart_data["has_unavailable_totals"],
            "chart_all_totals_unavailable": chart_data["all_totals_unavailable"],
            "chart_estimated": json.dumps(chart_data["estimated"]),
            "chart_has_estimated_values": chart_data["has_estimated_values"],
            "chart_market_value_extends_further": chart_data[
                "market_value_extends_further"
            ],
            "chart_points": len(chart_data["values"]),
            # GH-484: usable (non-null) Portfolio Value points — the canvas
            # gate. Two points render as detached dots, indistinguishable
            # from a broken chart, so the template needs this count.
            # Counts whichever series will actually be drawn: Portfolio Value
            # normally, but Market Value when that is the one with history
            # (#512). Gating purely on totals hid the canvas entirely on a
            # range whose real data lives in the market-value series.
            "chart_usable_total_points": max(
                sum(1 for value in chart_data["total_values"] if value is not None),
                sum(1 for value in chart_data["values"] if value is not None)
                if chart_data["market_value_extends_further"]
                else 0,
            ),
            "chart_buys": json.dumps(buy_vals),
            "chart_sells": json.dumps(sell_vals),
            "chart_buy_tips": json.dumps(buy_tips),
            "chart_sell_tips": json.dumps(sell_tips),
            "chart_range": chart_range,
            "chart_has_history": chart_has_history,
            "chart_range_availability": self.chart_range_availability(
                portfolio_id, chart_range
            ),
        }

    def chart_fragment_context(
        self, portfolio_id: int | None, range_key: str = DEFAULT_CHART_RANGE
    ) -> dict:
        """Build just the chart-card context for ``GET /partials/portfolio/chart``.

        Deliberately lean (#421): a range change re-renders only
        ``#portfolio-chart-card`` and must not rebuild positions, prices, or
        cash — so this never calls ``default_portfolio_context`` /
        ``positions_from_input_snapshot``.
        """
        portfolios = self._trader.list_portfolios()
        active_id = portfolio_id
        if portfolios and (
            active_id is None or not any(p.id == active_id for p in portfolios)
        ):
            active_id = portfolios[0].id

        chart_data = self._load_portfolio_history(active_id, range_key)
        chart_range = (
            range_key if range_key in CHART_RANGE_DAYS else DEFAULT_CHART_RANGE
        )
        if active_id is not None:
            cutoff_date = self.chart_cutoff_iso(range_key)[:10]
            trades = [
                t
                for t in self._trader.get_trade_history(portfolio_id=active_id)
                if t.date >= cutoff_date
            ]
            has_history = len(self._trader.snapshot_history(active_id, limit=2)) >= 2
        else:
            # Legacy single-portfolio CSV path: mirror the inline render's
            # ">= 2 points" check rather than hardcoding False, so the card
            # shell still renders with real data (#421).
            trades = []
            has_history = len(chart_data["values"]) >= 2
        buy_vals, sell_vals, buy_tips, sell_tips = self._trade_markers(
            chart_data, active_id, trades
        )
        return {
            "portfolio_id": active_id,
            "chart_range": chart_range,
            "chart_has_history": has_history,
            "chart_range_availability": self.chart_range_availability(
                active_id, chart_range
            ),
            "chart_labels": json.dumps(chart_data["labels"]),
            "chart_total_values": json.dumps(chart_data["total_values"]),
            "chart_values": json.dumps(chart_data["values"]),
            "chart_costs": json.dumps(chart_data["costs"]),
            "chart_cash": json.dumps(chart_data["cash_values"]),
            "chart_has_unavailable_totals": chart_data["has_unavailable_totals"],
            "chart_all_totals_unavailable": chart_data["all_totals_unavailable"],
            "chart_estimated": json.dumps(chart_data["estimated"]),
            "chart_has_estimated_values": chart_data["has_estimated_values"],
            "chart_market_value_extends_further": chart_data[
                "market_value_extends_further"
            ],
            "chart_points": len(chart_data["values"]),
            # Must expose the identical key as ``portfolio_partial_context``
            # so both render paths gate the canvas the same way (GH-484).
            # Counts whichever series will actually be drawn: Portfolio Value
            # normally, but Market Value when that is the one with history
            # (#512). Gating purely on totals hid the canvas entirely on a
            # range whose real data lives in the market-value series.
            "chart_usable_total_points": max(
                sum(1 for value in chart_data["total_values"] if value is not None),
                sum(1 for value in chart_data["values"] if value is not None)
                if chart_data["market_value_extends_further"]
                else 0,
            ),
            "chart_buys": json.dumps(buy_vals),
            "chart_sells": json.dumps(sell_vals),
            "chart_buy_tips": json.dumps(buy_tips),
            "chart_sell_tips": json.dumps(sell_tips),
        }

    def default_portfolio_context(
        self, portfolio_id: int | None = None, range_key: str = DEFAULT_CHART_RANGE
    ) -> dict:
        """Build the portfolio partial context from cached prices + analysis.

        Used by ``GET /partials/portfolio`` and after a trade is recorded.
        Scoped to ``portfolio_id`` when given (holdings, cash, and value chart);
        the portfolio selector metadata is always attached so the header can
        render the account switcher (#147).
        """
        portfolios = self._trader.list_portfolios()
        if not portfolios:
            # Empty state: no accounts exist yet.
            return {
                "positions": [],
                "portfolios": [],
                "portfolio_id": None,
                "no_portfolios": True,
                "provider_options": _load_provider_options(),
                "strategy_assignment": None,
                "strategy_freshness": None,
            }
        # Resolve the active portfolio: an unknown/None id falls back to the
        # first (migrated SIPP) portfolio.
        active_id = portfolio_id
        if active_id is None or not any(p.id == active_id for p in portfolios):
            active_id = portfolios[0].id

        snapshot = self.portfolio_input_snapshot(
            active_id, portfolios=portfolios, range_key=range_key
        )
        cached_prices, prices_as_of, display_info = self._trader.load_price_cache()
        analysis_prices = self.current_prices(snapshot.analysis_records)
        prices = {**cached_prices, **analysis_prices}
        positions = self.positions_from_input_snapshot(
            snapshot, prices or None, display_info or None
        )
        gbpusd = cached_prices.get("__GBPUSD__")
        return self.portfolio_partial_context(
            positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=snapshot.cash_balance,
            portfolio_id=active_id,
            input_snapshot=snapshot,
            range_key=range_key,
        )
