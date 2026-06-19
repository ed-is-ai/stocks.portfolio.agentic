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
from datetime import datetime

from app.agents.analyst.exit_evaluator import ExitEvaluator
from app.core.config import (
    ANALYSIS_JSON,
    PORTFOLIO_VALUE_CSV,
    TICKER_ALIASES_JSON,
)
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

_DEFAULT_GBPUSD = 1.35


class PortfolioService:
    """Valuation and presentation-data builder for the portfolio views."""

    def __init__(
        self,
        trader: TraderService,
        evaluator: ExitEvaluator | None = None,
    ) -> None:
        self._trader = trader
        self._evaluator = evaluator or ExitEvaluator()

    # --- analysis + aliases ----------------------------------------------

    def load_analysis(self) -> list[StockRecord]:
        """Load latest analysis results, returning empty list on any error."""
        try:
            data = json.loads(ANALYSIS_JSON.read_text(encoding="utf-8"))
            return [StockRecord.model_validate(r) for r in data]
        except Exception:
            return []

    @staticmethod
    def current_prices(records: list[StockRecord]) -> dict[str, float]:
        """Return {ticker: price} from analysis records."""
        return {r.ticker: r.price for r in records}

    def load_ticker_aliases(self) -> dict[str, str]:
        """Load internal-ticker → Yahoo Finance symbol mappings."""
        try:
            return json.loads(TICKER_ALIASES_JSON.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    # --- FX ---------------------------------------------------------------

    @staticmethod
    def _fetch_gbpusd_rate() -> float | None:
        """Fetch live GBP/USD rate from yfinance; return None on failure."""
        try:
            import yfinance as yf

            return float(yf.Ticker("GBPUSD=X").fast_info.last_price)
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
            yf_sym = aliases.get(t, t)
            result = self._fetch_price_gbp(yf_sym, gbpusd)
            if (result is None or result[0] < 0.01) and t not in aliases:
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

    # --- chart data -------------------------------------------------------

    def _load_portfolio_history(self) -> dict:
        """Return chart-ready dicts with labels, values, costs, and cash."""
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

    def _trade_markers(self, chart_data: dict) -> tuple[list, list, list, list]:
        """Return (buy_values, sell_values, buy_labels, sell_labels) aligned to labels.

        Each array is len(labels) long with None at positions that have no trade.
        buy/sell_labels are tooltip strings for each non-None entry.
        """
        labels = chart_data["labels"]
        values = chart_data["values"]
        n = len(labels)

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

        trades = self._trader.get_trade_history()
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

        chart_data = self._load_portfolio_history()
        buy_vals, sell_vals, buy_tips, sell_tips = self._trade_markers(chart_data)
        return {
            "positions": positions,
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

    def default_portfolio_context(self) -> dict:
        """Build the portfolio partial context from cached prices + analysis.

        Used by ``GET /partials/portfolio`` and after a trade is recorded.
        """
        cached_prices, prices_as_of, display_info = self._trader.load_price_cache()
        analysis_prices = self.current_prices(self.load_analysis())
        prices = {**cached_prices, **analysis_prices}
        positions = self._trader.get_portfolio(prices or None, display_info or None)
        gbpusd = cached_prices.get("__GBPUSD__")
        cash_balance = self._trader.get_cash_balance()
        return self.portfolio_partial_context(
            positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=cash_balance,
        )
