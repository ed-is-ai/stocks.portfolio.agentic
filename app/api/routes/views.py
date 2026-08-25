"""Read-only view routes — the main page and htmx partials."""

import csv
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_alerts_repository,
    get_portfolio_service,
    get_realised_pnl_service,
    get_trader_service,
)
from app.api.params import optional_int
from app.api.templating import templates
from app.api.stock_scanner_context import build_stock_scanner_context
from app.core.config import PIPELINE_RUNS_CSV
from app.core.security import require_local_or_token
from app.repositories.alerts_repo import AlertsRepository
from app.schemas.source_health import SourceHealth
from app.services.portfolio_service import PortfolioService
from app.services.realised_pnl_service import RealisedPnlService
from app.services.trader_service import TraderService

router = APIRouter()

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
AlertsDep = Annotated[AlertsRepository, Depends(get_alerts_repository)]
RealisedPnlDep = Annotated[RealisedPnlService, Depends(get_realised_pnl_service)]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@router.get("/partials/stock-scanner", response_class=HTMLResponse)
async def partial_stock_scanner(
    request: Request, trader: TraderDep, portfolio: PortfolioDep, alerts: AlertsDep
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_stock_scanner.html",
        context=build_stock_scanner_context(trader, portfolio, alerts),
    )


@router.get("/partials/portfolio", response_class=HTMLResponse)
async def partial_portfolio(
    request: Request, portfolio: PortfolioDep, portfolio_id: str | None = None
) -> HTMLResponse:
    # Accept a raw string: the client sends an empty ``portfolio_id=`` when no
    # account is selected, which an ``int | None`` param rejects with 422 and
    # breaks the tab (#147 regression).
    context = portfolio.default_portfolio_context(optional_int(portfolio_id))
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.get("/partials/realised-pnl", response_class=HTMLResponse)
async def partial_realised_pnl(
    request: Request,
    trader: TraderDep,
    realised_pnl: RealisedPnlDep,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    # Accept a raw string: the client sends an empty ``portfolio_id=`` when no
    # account is selected, which an ``int | None`` param rejects with 422 and
    # breaks the tab (#147 regression).
    pid = optional_int(portfolio_id)
    portfolios = trader.list_portfolios()
    if not portfolios:
        return templates.TemplateResponse(
            request, "_realised_pnl.html", context={"no_portfolios": True}
        )
    # Resolve the active portfolio: an unknown/None id falls back to the
    # first portfolio, matching PortfolioService.default_portfolio_context.
    active_id = pid
    if active_id is None or not any(p.id == active_id for p in portfolios):
        active_id = portfolios[0].id
    active_portfolio = next(p for p in portfolios if p.id == active_id)
    summary = realised_pnl.compute_summary(active_id)
    return templates.TemplateResponse(
        request,
        "_realised_pnl.html",
        context={
            "portfolios": portfolios,
            "active_portfolio": active_portfolio,
            "summary": summary,
            "unmatched_sells": summary.unmatched_sells,
        },
    )


@router.post(
    "/trades/{trade_id}/ack",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def ack_unmatched_sell(
    request: Request,
    trade_id: int,
    trader: TraderDep,
    realised_pnl: RealisedPnlDep,
    portfolio_id: str | None = None,
) -> HTMLResponse:
    """Toggle one unmatched sell's acknowledgment; re-render its fragment only.

    Bodyless per AD-8 -- ``portfolio_id`` travels as a query-string param on
    the ``hx-post`` URL (not a form field/body), only so the response can be
    re-scoped to the same Account; the ack value itself is never supplied by
    the client, only toggled server-side.
    """
    pid = optional_int(portfolio_id)
    portfolios = trader.list_portfolios()
    if not portfolios:
        return templates.TemplateResponse(
            request, "_unmatched_sells.html", context={"unmatched_sells": []}
        )
    active_id = pid
    if active_id is None or not any(p.id == active_id for p in portfolios):
        active_id = portfolios[0].id
    active_portfolio = next(p for p in portfolios if p.id == active_id)
    summary = realised_pnl.toggle_unmatched_sell_ack(trade_id, active_id)
    return templates.TemplateResponse(
        request,
        "_unmatched_sells.html",
        context={
            "unmatched_sells": summary.unmatched_sells,
            "active_portfolio": active_portfolio,
        },
    )


@router.get("/partials/history", response_class=HTMLResponse)
async def partial_history(
    request: Request, trader: TraderDep, realised_pnl: RealisedPnlDep
) -> HTMLResponse:
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
    # Story 2.4 (AC6/AC7/AC8): an Opening Lot row is labelled "Manually
    # entered" and its edit/delete actions are gated on a fresh
    # consumed/unconsumed check -- computed here (not persisted) so the
    # template can render a read-only state for a consumed lot without a
    # second round trip.
    opening_lot_status = {
        t.id: realised_pnl.opening_lot_status(t.id, t.portfolio_id)
        for t in trades
        if t.source == "opening_lot" and t.id is not None and t.portfolio_id is not None
    }
    return templates.TemplateResponse(
        request,
        "_history.html",
        context={
            "trades": trades,
            "portfolio_names": names,
            "opening_lot_status": opening_lot_status,
        },
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
