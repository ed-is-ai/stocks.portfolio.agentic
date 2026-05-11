"""
FastAPI web app — serves the trader UI and delegates trade operations to TraderAgent.

Run with:
    python -m uvicorn web.app:app --reload
"""

import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Allow imports from project root regardless of working directory
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.analyst.exit_evaluator import ExitEvaluator  # noqa: E402
from agents.trader.trader_agent import TraderAgent  # noqa: E402
from models import StockRecord  # noqa: E402

_ANALYSIS_JSON = _ROOT / "agents" / "analyst" / "analysis_results.json"
_RUN_LOG_CSV = _ROOT / "pipeline_runs.csv"
_PORTFOLIO_VALUE_CSV = _ROOT / "portfolio_value.csv"

app = FastAPI(title="Stock Trader")
templates = Jinja2Templates(directory=str(_ROOT / "web" / "templates"))
trader = TraderAgent(name="TraderAgent")
_evaluator = ExitEvaluator()


_TICKER_ALIASES_JSON = _ROOT / "data" / "ticker_aliases.json"


def _load_ticker_aliases() -> dict[str, str]:
    """Load internal-ticker → Yahoo Finance symbol mappings from data/ticker_aliases.json."""
    try:
        return json.loads(_TICKER_ALIASES_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _fetch_gbpusd_rate() -> float | None:
    """Fetch live GBP/USD rate from yfinance; return None on failure."""
    try:
        import yfinance as yf
        return float(yf.Ticker("GBPUSD=X").fast_info.last_price)
    except Exception:
        return None


def _get_gbpusd_rate() -> float:
    """Return live GBP/USD rate, falling back to last cached value."""
    logger = logging.getLogger(__name__)
    rate = _fetch_gbpusd_rate()
    if rate:
        trader.save_price_cache({"__GBPUSD__": rate})
        return rate
    cached, _, _disp = trader.load_price_cache()
    if "__GBPUSD__" in cached:
        logger.warning("GBPUSD rate fetch failed; using cached rate")
        return cached["__GBPUSD__"]
    logger.warning("GBPUSD rate unavailable; defaulting to 1.35")
    return 1.35


def _load_analysis() -> list[StockRecord]:
    """Load latest analysis results, returning empty list on any error."""
    try:
        data = json.loads(_ANALYSIS_JSON.read_text(encoding="utf-8"))
        return [StockRecord.model_validate(r) for r in data]
    except Exception:
        return []


def _current_prices(records: list[StockRecord]) -> dict[str, float]:
    return {r.ticker: r.price for r in records}


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


# ---------------------------------------------------------------------------
# htmx partials
# ---------------------------------------------------------------------------

@app.get("/partials/watchlist", response_class=HTMLResponse)
async def partial_watchlist(request: Request) -> HTMLResponse:
    records = _load_analysis()
    portfolio_tickers = {p.ticker for p in trader.get_portfolio()}
    return templates.TemplateResponse(
        request, "_watchlist.html",
        context={"records": records, "portfolio_tickers": portfolio_tickers},
    )


_logger = logging.getLogger(__name__)


def _fetch_price_gbp(
    yf_sym: str, gbpusd: float
) -> tuple[float, float, str] | None:
    """Fetch the most recent closing price for a yfinance symbol.

    Returns (gbp_price, original_price, currency_code) or None on failure.
    gbp_price is always in GBP; original_price is in the native currency.
    """
    import math
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
        _logger.warning(f"Could not determine currency for {yf_sym}; using raw price")
    return gbp_price, orig_price, currency


def _fetch_all_prices(
    tickers: list[str], aliases: dict[str, str], gbpusd: float
) -> tuple[dict[str, float], dict[str, tuple[float, str]]]:
    """Fetch GBP-normalised prices for all portfolio tickers.

    Returns (gbp_prices, display_info) where:
    - gbp_prices: {ticker: gbp_price} for portfolio calculations
    - display_info: {ticker: (original_price, currency)} for the UI
    """
    gbp_prices: dict[str, float] = {}
    display_info: dict[str, tuple[float, str]] = {}
    for t in tickers:
        yf_sym = aliases.get(t, t)
        result = _fetch_price_gbp(yf_sym, gbpusd)
        if (result is None or result[0] < 0.01) and t not in aliases:
            result = _fetch_price_gbp(f"{t}.L", gbpusd)
        if result is not None and result[0] >= 0.01:
            gbp_price, orig_price, currency = result
            gbp_prices[t] = gbp_price
            display_info[t] = (orig_price, currency)
    return gbp_prices, display_info


@app.post("/api/portfolio/refresh", response_class=HTMLResponse)
async def refresh_portfolio_prices(request: Request) -> HTMLResponse:
    """Fetch live prices from yfinance and return updated portfolio partial."""
    try:
        portfolio = trader.get_portfolio()
        _logger.info(f"Refreshing {len(portfolio)} positions")
        if not portfolio:
            return _render_portfolio(request, [], error_message="No positions to refresh", status_code=400)

        gbpusd = _get_gbpusd_rate()
        tickers = [p.ticker for p in portfolio]
        gbp_prices, display_info = _fetch_all_prices(tickers, _load_ticker_aliases(), gbpusd)

        trader.save_price_cache(gbp_prices, display_info)
        _, prices_as_of, _ = trader.load_price_cache()
        updated_positions = trader.refresh_portfolio_prices(gbp_prices, display_info)
        _logger.info(f"Refreshed {len(updated_positions)} positions successfully")
        cash_balance = trader.get_cash_balance()
        trader.update_portfolio_snapshot(cash_balance)
        return _render_portfolio(request, updated_positions, prices_as_of=prices_as_of, gbpusd_rate=gbpusd, cash_balance=cash_balance)
    except Exception as e:
        _logger.exception(f"Failed to refresh portfolio prices: {e}")
        cached_prices, prices_as_of, display_info = trader.load_price_cache()
        cached_positions = trader.get_portfolio(cached_prices or None, display_info or None)
        gbpusd = cached_prices.get("__GBPUSD__")
        return _render_portfolio(
            request, cached_positions, prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd, error_message=f"Failed to fetch prices: {e}", status_code=500,
        )


@app.post("/refresh-data", response_class=HTMLResponse)
async def refresh_data(request: Request) -> HTMLResponse:
    """Refresh the analysis dataset by running the orchestrator once."""
    import asyncio
    import subprocess

    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(_ROOT / "orchestrator.py"), "--once"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    records = _load_analysis()
    portfolio_tickers = {p.ticker for p in trader.get_portfolio()}
    refresh_success = result.returncode == 0
    refresh_status = "Data refreshed successfully" if refresh_success else "Data refresh failed"
    refresh_details = (result.stdout or result.stderr).strip()
    return templates.TemplateResponse(
        request, "_watchlist.html",
        context={
            "records": records,
            "portfolio_tickers": portfolio_tickers,
            "refresh_status": refresh_status,
            "refresh_success": refresh_success,
            "refresh_details": refresh_details,
        },
    )


def _load_portfolio_history() -> dict:
    """Return chart-ready dicts with labels, values, costs, and cash from the value log."""
    if not _PORTFOLIO_VALUE_CSV.exists():
        return {"labels": [], "values": [], "costs": [], "cash_values": []}
    with open(_PORTFOLIO_VALUE_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rows = rows[-180:]

    def _cash(r: dict) -> float | None:
        v = r.get("cash_balance")
        return float(v) if v not in (None, "") else None

    return {
        "labels":      [r["timestamp"][:16].replace("T", " ") for r in rows],
        "values":      [float(r["total_value"]) for r in rows],
        "costs":       [float(r["total_cost"])  for r in rows],
        "cash_values": [_cash(r) for r in rows],
    }


def _trade_markers(chart_data: dict) -> tuple[list, list, list, list]:
    """Return (buy_values, sell_values, buy_labels, sell_labels) aligned to chart labels.

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

    buy_vals:    list = [None] * n
    sell_vals:   list = [None] * n
    buy_tips:    list = [None] * n
    sell_tips:   list = [None] * n

    from agents.trader.trader_agent import TraderAgent as _TA
    trades = _TA(name="TraderAgent").get_trade_history()
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
        tip = f"{trade.action} {trade.shares:g} {trade.ticker} @ ${trade.price:.2f} ({trade.date})"
        if trade.action == "BUY":
            buy_vals[best_idx] = values[best_idx]
            buy_tips[best_idx] = tip
        else:
            sell_vals[best_idx] = values[best_idx]
            sell_tips[best_idx] = tip

    return buy_vals, sell_vals, buy_tips, sell_tips


def _to_gbp(amount: float, currency: str, gbpusd: float) -> float:
    """Convert amount to GBP using current rate if currency is USD."""
    return amount / gbpusd if currency == "USD" else amount


def _render_portfolio(
    request: Request,
    positions: list,
    prices_as_of: str | None = None,
    gbpusd_rate: float | None = None,
    cash_balance: float | None = None,
    error_message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the portfolio partial with chart data."""
    records = _load_analysis()
    analysis_map = {r.ticker: r for r in records}
    for pos in positions:
        stock = analysis_map.get(pos.ticker)
        pos.exit_signal = _evaluator.evaluate(pos, stock)
        if stock and stock.analysis:
            pos.next_pivot = stock.analysis.entry_price

    # Compute GBP-equivalent totals for summary cards (including cash)
    fx = gbpusd_rate or 1.35
    total_cost_gbp = sum(_to_gbp(p.total_cost, p.price_currency, fx) for p in positions)
    if cash_balance is not None:
        total_cost_gbp += cash_balance
    positions_with_value = [p for p in positions if p.current_value is not None]
    total_value_gbp = sum(
        _to_gbp(p.current_value, p.price_currency, fx)  # type: ignore[arg-type]
        for p in positions_with_value
    )
    if cash_balance is not None:
        total_value_gbp += cash_balance
    total_cost_gbp_valued = sum(
        _to_gbp(p.total_cost, p.price_currency, fx) for p in positions_with_value
    )
    total_pnl_gbp = total_value_gbp - total_cost_gbp_valued - (cash_balance or 0)

    chart_data = _load_portfolio_history()
    buy_vals, sell_vals, buy_tips, sell_tips = _trade_markers(chart_data)
    return templates.TemplateResponse(
        request, "_portfolio.html",
        context={
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
            "chart_costs":  json.dumps(chart_data["costs"]),
            "chart_cash":   json.dumps(chart_data["cash_values"]),
            "chart_points": len(chart_data["values"]),
            "chart_buys":   json.dumps(buy_vals),
            "chart_sells":  json.dumps(sell_vals),
            "chart_buy_tips":  json.dumps(buy_tips),
            "chart_sell_tips": json.dumps(sell_tips),
        },
        status_code=status_code,
    )


@app.get("/partials/portfolio", response_class=HTMLResponse)
async def partial_portfolio(request: Request) -> HTMLResponse:
    cached_prices, prices_as_of, display_info = trader.load_price_cache()
    analysis_prices = _current_prices(_load_analysis())
    prices = {**cached_prices, **analysis_prices}
    positions = trader.get_portfolio(prices or None, display_info or None)
    gbpusd = cached_prices.get("__GBPUSD__")
    cash_balance = trader.get_cash_balance()
    return _render_portfolio(
        request, positions,
        prices_as_of=prices_as_of, gbpusd_rate=gbpusd, cash_balance=cash_balance,
    )


@app.get("/partials/history", response_class=HTMLResponse)
async def partial_history(request: Request) -> HTMLResponse:
    trades = trader.get_trade_history()
    return templates.TemplateResponse(
        request, "_history.html", context={"trades": trades}
    )


@app.get("/partials/runlog", response_class=HTMLResponse)
async def partial_runlog(request: Request) -> HTMLResponse:
    runs: list[dict] = []
    if _RUN_LOG_CSV.exists():
        with open(_RUN_LOG_CSV, newline="", encoding="utf-8") as fh:
            runs = list(csv.DictReader(fh))
    runs.reverse()  # most recent first
    return templates.TemplateResponse(
        request, "_runlog.html", context={"runs": runs}
    )


# ---------------------------------------------------------------------------
# Trade actions
# ---------------------------------------------------------------------------

@app.post("/trades")
async def record_trade(
    request: Request,
    ticker: Annotated[str, Form()],
    action: Annotated[str, Form()],
    shares: Annotated[float, Form()],
    price: Annotated[float, Form()],
    date: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    stop_loss: Annotated[float | None, Form()] = None,
    entry_price: Annotated[float | None, Form()] = None,
) -> HTMLResponse:
    """Record a BUY, SELL, or CORRECT action and return the updated portfolio partial."""
    if action == "BUY":
        trader.record_buy(ticker, shares, price, date, notes, stop_loss, entry_price)
    elif action == "SELL":
        trader.record_sell(ticker, shares, price, date, notes)
    elif action == "CORRECT":
        trader.correct_trade(ticker, shares, price, date, notes, stop_loss, entry_price)
    else:
        print(f"[trades] unsupported action: {action}")
    return await partial_portfolio(request)


@app.delete("/trades/{trade_id}")
async def delete_trade(trade_id: int) -> RedirectResponse:
    """Delete a trade by ID and redirect to history partial."""
    trader.delete_trade(trade_id)
    return RedirectResponse("/partials/history", status_code=303)

