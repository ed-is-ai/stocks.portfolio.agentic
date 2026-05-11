"""
FastAPI web app — serves the trader UI and delegates trade operations to TraderAgent.

Run with:
    python -m uvicorn web.app:app --reload
"""

import csv
import json
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


@app.post("/api/portfolio/refresh", response_class=HTMLResponse)
async def refresh_portfolio_prices(request: Request) -> HTMLResponse:
    """Fetch live prices from yfinance and return updated portfolio partial."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        import yfinance as yf

        portfolio = trader.get_portfolio()
        logger.info(f"Refreshing {len(portfolio)} positions")
        if not portfolio:
            return templates.TemplateResponse(
                request, "_portfolio.html",
                context={
                    "positions": [],
                    "chart_labels": json.dumps([]),
                    "chart_values": json.dumps([]),
                    "chart_costs": json.dumps([]),
                    "chart_points": 0,
                    "chart_buys": json.dumps([]),
                    "chart_sells": json.dumps([]),
                    "chart_buy_tips": json.dumps([]),
                    "chart_sell_tips": json.dumps([]),
                    "error_message": "No positions to refresh",
                },
                status_code=400,
            )

        tickers = [p.ticker for p in portfolio]
        tickers_str = " ".join(tickers)
        yf_data = yf.download(tickers_str, period="1d", progress=False)

        prices = {}
        if len(tickers) == 1:
            if not yf_data.empty:
                prices[tickers[0]] = round(float(yf_data["Close"].iloc[-1]), 2)
        else:
            for ticker in tickers:
                if ticker in yf_data["Close"].columns:
                    prices[ticker] = round(
                        float(yf_data["Close"][ticker].iloc[-1]), 2
                    )

        updated_positions = trader.refresh_portfolio_prices(prices)
        records = _load_analysis()
        analysis_map = {r.ticker: r for r in records}
        for pos in updated_positions:
            stock = analysis_map.get(pos.ticker)
            pos.exit_signal = _evaluator.evaluate(pos, stock)
            if stock and stock.analysis:
                pos.next_pivot = stock.analysis.entry_price

        chart_data = _load_portfolio_history()
        buy_vals, sell_vals, buy_tips, sell_tips = _trade_markers(chart_data)

        logger.info(f"Refreshed {len(updated_positions)} positions successfully")
        return templates.TemplateResponse(
            request, "_portfolio.html",
            context={
                "positions": updated_positions,
                "chart_labels": json.dumps(chart_data["labels"]),
                "chart_values": json.dumps(chart_data["values"]),
                "chart_costs": json.dumps(chart_data["costs"]),
                "chart_points": len(chart_data["values"]),
                "chart_buys": json.dumps(buy_vals),
                "chart_sells": json.dumps(sell_vals),
                "chart_buy_tips": json.dumps(buy_tips),
                "chart_sell_tips": json.dumps(sell_tips),
            },
        )
    except Exception as e:
        logger.error(f"Failed to refresh portfolio prices: {str(e)}")
        return templates.TemplateResponse(
            request, "_portfolio.html",
            context={
                "positions": [],
                "chart_labels": json.dumps([]),
                "chart_values": json.dumps([]),
                "chart_costs": json.dumps([]),
                "chart_points": 0,
                "chart_buys": json.dumps([]),
                "chart_sells": json.dumps([]),
                "chart_buy_tips": json.dumps([]),
                "chart_sell_tips": json.dumps([]),
                "error_message": f"Failed to fetch prices: {str(e)}",
            },
            status_code=500,
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
    """Return chart-ready dicts with labels, values, and costs from the value log."""
    if not _PORTFOLIO_VALUE_CSV.exists():
        return {"labels": [], "values": [], "costs": []}
    with open(_PORTFOLIO_VALUE_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rows = rows[-180:]
    return {
        "labels": [r["timestamp"][:16].replace("T", " ") for r in rows],
        "values": [float(r["total_value"]) for r in rows],
        "costs":  [float(r["total_cost"])  for r in rows],
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


@app.get("/partials/portfolio", response_class=HTMLResponse)
async def partial_portfolio(request: Request) -> HTMLResponse:
    records = _load_analysis()
    prices = _current_prices(records)
    analysis_map = {r.ticker: r for r in records}
    positions = trader.get_portfolio(prices)
    for pos in positions:
        stock = analysis_map.get(pos.ticker)
        pos.exit_signal = _evaluator.evaluate(pos, stock)
        if stock and stock.analysis:
            pos.next_pivot = stock.analysis.entry_price
    chart_data = _load_portfolio_history()
    buy_vals, sell_vals, buy_tips, sell_tips = _trade_markers(chart_data)
    return templates.TemplateResponse(
        request, "_portfolio.html",
        context={
            "positions": positions,
            "chart_labels": json.dumps(chart_data["labels"]),
            "chart_values": json.dumps(chart_data["values"]),
            "chart_costs":  json.dumps(chart_data["costs"]),
            "chart_points": len(chart_data["values"]),
            "chart_buys":   json.dumps(buy_vals),
            "chart_sells":  json.dumps(sell_vals),
            "chart_buy_tips":  json.dumps(buy_tips),
            "chart_sell_tips": json.dumps(sell_tips),
        },
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

