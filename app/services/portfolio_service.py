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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, cast

from app.agents.analyst.exit_evaluator import ExitEvaluator
from app.core.config import ANALYSIS_JSON, PORTFOLIO_VALUE_CSV, TRADES_DB
from app.core.money import Money
from app.core.ticker_identity import canonicalize_or_fallback, load_aliases
from app.repositories import db
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.schemas.analysis_artifact import read_analysis_records
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.gbp_valuation_service import GbpValuationService
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

_DEFAULT_GBPUSD = 1.35
_HISTORICAL_FX_PAIR: dict[str, str] = {
    "USD": "GBPUSD=X",
    "HKD": "GBPHKD=X",
}
_YFINANCE_SYMBOL_OVERRIDES: dict[str, str] = {"9988": "9988.HK"}


class PortfolioService:
    """Valuation and presentation-data builder for the portfolio views."""

    def __init__(
        self,
        trader: TraderService,
        evaluator: ExitEvaluator | None = None,
        gbp_valuation: GbpValuationService | None = None,
    ) -> None:
        self._trader = trader
        self._evaluator = evaluator or ExitEvaluator()
        self._gbp_valuation = gbp_valuation or GbpValuationService(
            FxQuoteRepository(db.make_connect(lambda: TRADES_DB))
        )

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

    def gbpusd_rate(self) -> float:
        """Return live GBP/USD rate, falling back to last cached value."""
        rate = self._fetch_gbpusd_rate()
        if rate:
            self._trader.save_price_cache({"__GBPUSD__": rate})
            return rate
        cached, _, _disp = self._trader.load_price_cache()
        if "__GBPUSD__" in cached:
            logger.warning("GBPUSD rate fetch failed; using cached rate")
            return cached["__GBPUSD__"]
        logger.warning("GBPUSD rate unavailable; defaulting to %s", _DEFAULT_GBPUSD)
        return _DEFAULT_GBPUSD

    @staticmethod
    def _to_gbp(amount: float, currency: str, gbpusd: float) -> float:
        """Convert amount to GBP using current rate if currency is USD."""
        return amount / gbpusd if currency == "USD" else amount

    @staticmethod
    def _is_valid_rate(rate: float | None) -> bool:
        """Return True for a usable rate: not None and strictly positive.

        A cached or freshly-fetched rate of ``0``, negative, or ``None`` is
        always treated as unavailable, never fed into a conversion (AC7).
        """
        return rate is not None and math.isfinite(rate) and rate > 0

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
            return str(currency or "GBP").strip().upper()
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
            ticker: str(display_info[ticker][1]).strip().upper()
            for ticker in unique
            if ticker in display_info and display_info[ticker][1]
        }
        missing = [ticker for ticker in unique if ticker not in resolved]
        if not missing:
            return resolved

        cached = self._trader.get_cached_ticker_currencies(missing)
        resolved.update(cached)
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

    # --- live price fetching ---------------------------------------------

    def _fetch_price_gbp(
        self, yf_sym: str, gbpusd: float
    ) -> tuple[float, float, str] | None:
        """Fetch the most recent closing price for a yfinance symbol.

        Returns (gbp_price, original_price, currency_code) or None on failure.
        gbp_price is always in GBP; original_price is in the native currency.
        """
        import yfinance as yf

        data = yf.download(yf_sym, period="5d", progress=False, auto_adjust=True)
        if data.empty:
            return None
        close = data["Close"] if "Close" in data.columns else data.iloc[:, 0]
        series = close.iloc[:, 0] if hasattr(close, "columns") else close
        series = series.dropna()
        if series.empty:
            return None
        val = float(series.iloc[-1])
        if math.isnan(val):
            return None
        orig_price = round(val, 2)
        gbp_price = orig_price
        currency = "GBP"
        try:
            currency = yf.Ticker(yf_sym).fast_info.currency or "GBP"
            if currency == "GBp":
                gbp_price = round(orig_price / 100, 4)
                currency = "GBP"
            elif currency == "USD":
                gbp_price = round(orig_price / gbpusd, 4)
        except Exception:
            logger.warning(
                "Could not determine currency for %s; using raw price", yf_sym
            )
        return gbp_price, orig_price, currency

    def fetch_all_prices(
        self, tickers: list[str], aliases: dict[str, str], gbpusd: float
    ) -> tuple[dict[str, float], dict[str, tuple[float, str]]]:
        """Fetch GBP-normalised prices for all portfolio tickers (concurrently)."""

        def _resolve(t: str) -> tuple[str, float, float, str] | None:
            yf_sym = canonicalize_or_fallback(
                t,
                aliases,
                logger=logger,
                context="fetch_all_prices",
            )
            result = self._fetch_price_gbp(yf_sym, gbpusd)
            # ``.L``-suffix retry only when canonicalization left `t`
            # unchanged (no alias configured for it, safe to guess) -- not
            # raw `t not in aliases` membership, which would miss an
            # explicitly configured alias reached via a chain.
            if (result is None or result[0] < 0.01) and yf_sym == t:
                result = self._fetch_price_gbp(f"{t}.L", gbpusd)
            if result is not None and result[0] >= 0.01:
                gbp_price, orig_price, currency = result
                return t, gbp_price, orig_price, currency
            return None

        gbp_prices: dict[str, float] = {}
        display_info: dict[str, tuple[float, str]] = {}
        if not tickers:
            return gbp_prices, display_info
        with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
            for res in pool.map(_resolve, tickers):
                if res is not None:
                    t, gbp_price, orig_price, currency = res
                    gbp_prices[t] = gbp_price
                    display_info[t] = (orig_price, currency)
        return gbp_prices, display_info

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

    @staticmethod
    def gbp_totals(
        positions: list[Position], gbpusd: float
    ) -> tuple[float, float, float]:
        """Return (total_value_gbp, total_cost_gbp, total_pnl_gbp) for positions.

        Converts each position's native-currency ``total_cost``/
        ``current_value`` to GBP before summing, so USD holdings don't
        distort the aggregate the way a naive cross-currency sum would.
        ``total_cost`` includes every position; ``total_value`` only those
        with a known ``current_value`` (unpriced positions are excluded).
        """
        total_cost_gbp = sum(
            PortfolioService._to_gbp(p.total_cost, p.price_currency, gbpusd)
            for p in positions
        )
        total_value_gbp = sum(
            PortfolioService._to_gbp(p.current_value, p.price_currency, gbpusd)
            for p in positions
            if p.current_value is not None
        )
        total_pnl_gbp = total_value_gbp - total_cost_gbp
        return total_value_gbp, total_cost_gbp, total_pnl_gbp

    # --- chart data -------------------------------------------------------

    def _load_portfolio_history(self, portfolio_id: int | None = None) -> dict:
        """Return chart-ready dicts with labels, values, costs, and cash.

        With a ``portfolio_id`` the series comes from that portfolio's
        ``portfolio_snapshots`` (#147); without one it falls back to the legacy
        single-portfolio ``portfolio_value.csv``.
        """
        if portfolio_id is not None:
            rows = self._trader.snapshot_history(portfolio_id)
            return {
                "labels": [str(r[0])[:16].replace("T", " ") for r in rows],
                "values": [float(r[1]) for r in rows],
                "costs": [float(r[2]) for r in rows],
                "cash_values": [
                    float(r[3]) if r[3] is not None else None for r in rows
                ],
            }
        if not PORTFOLIO_VALUE_CSV.exists():
            return {"labels": [], "values": [], "costs": [], "cash_values": []}
        with open(PORTFOLIO_VALUE_CSV, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        rows = rows[-180:]

        def _cash(r: dict) -> float | None:
            v = r.get("cash_balance")
            return float(v) if v not in (None, "") else None

        return {
            "labels": [r["timestamp"][:16].replace("T", " ") for r in rows],
            "values": [float(r["total_value"]) for r in rows],
            "costs": [float(r["total_cost"]) for r in rows],
            "cash_values": [_cash(r) for r in rows],
        }

    def _trade_markers(
        self, chart_data: dict, portfolio_id: int | None = None
    ) -> tuple[list, list, list, list]:
        """Return (buy_values, sell_values, buy_labels, sell_labels) aligned to labels.

        Each array is len(labels) long with None at positions that have no trade.
        buy/sell_labels are tooltip strings for each non-None entry.
        """
        labels = chart_data["labels"]
        values = chart_data["values"]
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

        trades = self._trader.get_trade_history(portfolio_id=portfolio_id)
        trades.sort(key=lambda t: t.date)

        for trade in trades:
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

    def portfolio_partial_context(
        self,
        positions: list[Position],
        prices_as_of: str | None = None,
        gbpusd_rate: float | None = None,
        cash_balance: float | None = None,
        error_message: str | None = None,
        portfolio_id: int | None = None,
    ) -> dict:
        """Build the template context for the portfolio partial.

        Enriches positions with exit signals/next pivots, computes GBP-equivalent
        summary totals, and serialises chart data. Rendering stays in the route.
        """
        records = self.load_analysis()
        analysis_map = {r.ticker: r for r in records}
        for pos in positions:
            stock = analysis_map.get(pos.ticker)
            pos.exit_signal = self._evaluator.evaluate(pos, stock)
            if stock and stock.analysis:
                pos.next_pivot = stock.analysis.entry_price

        # Compute GBP-equivalent totals for summary cards (including cash)
        fx = gbpusd_rate or _DEFAULT_GBPUSD
        total_cost_gbp = sum(
            self._to_gbp(p.total_cost, p.price_currency, fx) for p in positions
        )
        if cash_balance is not None:
            total_cost_gbp += cash_balance
        positions_with_value = [p for p in positions if p.current_value is not None]
        total_value_gbp = sum(
            self._to_gbp(p.current_value, p.price_currency, fx)  # type: ignore[arg-type]
            for p in positions_with_value
        )
        if cash_balance is not None:
            total_value_gbp += cash_balance
        total_cost_gbp_valued = sum(
            self._to_gbp(p.total_cost, p.price_currency, fx)
            for p in positions_with_value
        )
        total_pnl_gbp = total_value_gbp - total_cost_gbp_valued - (cash_balance or 0)

        chart_data = self._load_portfolio_history(portfolio_id)
        buy_vals, sell_vals, buy_tips, sell_tips = self._trade_markers(
            chart_data, portfolio_id
        )
        # Always attach the account switcher metadata so any render of the
        # partial (trade, import, refresh, quick-add) keeps the selector (#147).
        portfolios = self._trader.list_portfolios()
        active_portfolio = next((p for p in portfolios if p.id == portfolio_id), None)
        # Cash-flow ledger for the selected account — read-only activity view
        # (#161). The balance still comes from the provider Running Balance.
        cash_flows = (
            self._trader.get_cash_flows(portfolio_id)
            if portfolio_id is not None
            else []
        )
        # Story 1.5, AC5: a count-only pointer to the dedicated
        # reconciliation view -- the detail lives there, not here.
        reconciliation_issue_count = (
            len(self._trader.list_reconciliation_issues(portfolio_id))
            if portfolio_id is not None
            else 0
        )
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
                for currency, amount, as_of in self._trader.list_cash_balances(
                    portfolio_id
                )
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
            {
                t.ticker
                for t in self._trader.get_trade_history(portfolio_id=portfolio_id)
                if t.source == "opening_lot"
            }
            if portfolio_id is not None
            else set()
        )
        return {
            "positions": positions,
            "portfolio_id": portfolio_id,
            "portfolios": portfolios,
            "active_portfolio": active_portfolio,
            "cash_flows": cash_flows,
            "opening_lot_tickers": opening_lot_tickers,
            "reconciliation_issue_count": reconciliation_issue_count,
            "cash_balances_by_currency": cash_balances_by_currency,
            "positions_with_value": positions_with_value,
            "total_cost_gbp": total_cost_gbp,
            "total_value_gbp": total_value_gbp,
            "total_pnl_gbp": total_pnl_gbp,
            "total_cost_gbp_valued": total_cost_gbp_valued,
            "cash_balance": cash_balance,
            "prices_as_of": prices_as_of,
            "gbpusd_rate": gbpusd_rate,
            "error_message": error_message,
            "chart_labels": json.dumps(chart_data["labels"]),
            "chart_values": json.dumps(chart_data["values"]),
            "chart_costs": json.dumps(chart_data["costs"]),
            "chart_cash": json.dumps(chart_data["cash_values"]),
            "chart_points": len(chart_data["values"]),
            "chart_buys": json.dumps(buy_vals),
            "chart_sells": json.dumps(sell_vals),
            "chart_buy_tips": json.dumps(buy_tips),
            "chart_sell_tips": json.dumps(sell_tips),
        }

    def default_portfolio_context(self, portfolio_id: int | None = None) -> dict:
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
            }
        # Resolve the active portfolio: an unknown/None id falls back to the
        # first (migrated SIPP) portfolio.
        active_id = portfolio_id
        if active_id is None or not any(p.id == active_id for p in portfolios):
            active_id = portfolios[0].id

        cached_prices, prices_as_of, display_info = self._trader.load_price_cache()
        analysis_prices = self.current_prices(self.load_analysis())
        prices = {**cached_prices, **analysis_prices}
        positions = self._trader.get_portfolio(
            prices or None, display_info or None, active_id
        )
        gbpusd = cached_prices.get("__GBPUSD__")
        cash_balance = self._trader.get_cash_balance(active_id)
        return self.portfolio_partial_context(
            positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=cash_balance,
            portfolio_id=active_id,
        )
