"""Read-only view routes — the main page and htmx partials."""

import csv
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import get_portfolio_service, get_trader_service
from app.api.templating import templates
from app.core.config import PIPELINE_RUNS_CSV
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@router.get("/partials/watchlist", response_class=HTMLResponse)
async def partial_watchlist(
    request: Request, trader: TraderDep, portfolio: PortfolioDep
) -> HTMLResponse:
    records = portfolio.load_analysis()
    portfolio_tickers = {p.ticker for p in trader.get_portfolio()}
    return templates.TemplateResponse(
        request,
        "_watchlist.html",
        context={"records": records, "portfolio_tickers": portfolio_tickers},
    )


@router.get("/partials/portfolio", response_class=HTMLResponse)
async def partial_portfolio(request: Request, portfolio: PortfolioDep) -> HTMLResponse:
    context = portfolio.default_portfolio_context()
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.get("/partials/history", response_class=HTMLResponse)
async def partial_history(request: Request, trader: TraderDep) -> HTMLResponse:
    trades = trader.get_trade_history()
    return templates.TemplateResponse(
        request, "_history.html", context={"trades": trades}
    )


@router.get("/partials/runlog", response_class=HTMLResponse)
async def partial_runlog(request: Request) -> HTMLResponse:
    runs: list[dict] = []
    if PIPELINE_RUNS_CSV.exists():
        with open(PIPELINE_RUNS_CSV, newline="", encoding="utf-8") as fh:
            runs = list(csv.DictReader(fh))
    runs.reverse()  # most recent first
    return templates.TemplateResponse(request, "_runlog.html", context={"runs": runs})
