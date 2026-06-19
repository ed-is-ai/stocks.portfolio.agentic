"""Pipeline routes — trigger a data refresh (money-mutating guard)."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_pipeline_service,
    get_portfolio_service,
    get_trader_service,
)
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.services.pipeline_service import PipelineService
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
PipelineDep = Annotated[PipelineService, Depends(get_pipeline_service)]


@router.post(
    "/refresh-data",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def refresh_data(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    pipeline: PipelineDep,
) -> HTMLResponse:
    """Refresh the analysis dataset by running the pipeline once."""
    result = await asyncio.to_thread(pipeline.run_once)
    records = portfolio.load_analysis()
    portfolio_tickers = {p.ticker for p in trader.get_portfolio()}
    refresh_status = (
        "Data refreshed successfully" if result.success else "Data refresh failed"
    )
    return templates.TemplateResponse(
        request,
        "_watchlist.html",
        context={
            "records": records,
            "portfolio_tickers": portfolio_tickers,
            "refresh_status": refresh_status,
            "refresh_success": result.success,
            "refresh_details": result.details,
        },
    )
