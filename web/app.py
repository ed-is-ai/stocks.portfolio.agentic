"""
FastAPI web app — serves the trader UI and delegates trade operations to TraderAgent.

Run with:
    python -m uvicorn web.app:app --reload
"""

import csv
import json
import sys
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
    return templates.TemplateResponse(
        request, "_portfolio.html", context={"positions": positions}
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

