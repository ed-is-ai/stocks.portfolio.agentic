"""Read-only view routes — the main page and htmx partials."""

import csv
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_alerts_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.api.templating import templates
from app.api.watchlist_context import build_watchlist_context
from app.core.config import PIPELINE_RUNS_CSV
from app.repositories.alerts_repo import AlertsRepository
from app.schemas.source_health import SourceHealth
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
AlertsDep = Annotated[AlertsRepository, Depends(get_alerts_repository)]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@router.get("/partials/watchlist", response_class=HTMLResponse)
async def partial_watchlist(
    request: Request, trader: TraderDep, portfolio: PortfolioDep, alerts: AlertsDep
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_watchlist.html",
        context=build_watchlist_context(trader, portfolio, alerts),
    )


@router.get("/partials/portfolio", response_class=HTMLResponse)
async def partial_portfolio(
    request: Request, portfolio: PortfolioDep, portfolio_id: int | None = None
) -> HTMLResponse:
    context = portfolio.default_portfolio_context(portfolio_id)
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.get("/partials/history", response_class=HTMLResponse)
async def partial_history(request: Request, trader: TraderDep) -> HTMLResponse:
    # Trade History spans every portfolio; a name map feeds the Portfolio
    # column, disambiguating duplicate names with #id (#147).
    trades = trader.get_trade_history()
    portfolios = trader.list_portfolios()
    seen: dict[str, int] = {}
    for pf in portfolios:
        seen[pf.name] = seen.get(pf.name, 0) + 1
    names = {
        pf.id: (f"{pf.name} #{pf.id}" if seen[pf.name] > 1 else pf.name)
        for pf in portfolios
    }
    return templates.TemplateResponse(
        request,
        "_history.html",
        context={"trades": trades, "portfolio_names": names},
    )


@router.get("/partials/runlog", response_class=HTMLResponse)
async def partial_runlog(request: Request) -> HTMLResponse:
    runs: list[dict] = []
    if PIPELINE_RUNS_CSV.exists():
        with open(PIPELINE_RUNS_CSV, newline="", encoding="utf-8") as fh:
            runs = list(csv.DictReader(fh))
    for run in runs:
        # Legacy CSV rows (written before a header field existed) simply
        # lack that key rather than having it as "" — fill in safe defaults
        # so the template can render them without a KeyError/Undefined.
        for field in (
            "duration_seconds",
            "scanned",
            "analysed",
            "buy_alerts",
            "sell_alerts",
            "actionable",
        ):
            run.setdefault(field, "0")
        run.setdefault("errors", "")
        run.setdefault("sources", "")
        try:
            payload = json.loads(run.get("source_health_json") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("source health must be an object")
            run["source_health"] = [
                SourceHealth.model_validate(value) for value in payload.values()
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            run["source_health"] = []
    runs.reverse()  # most recent first
    return templates.TemplateResponse(request, "_runlog.html", context={"runs": runs})
