"""Pipeline routes — trigger a data refresh (money-mutating guard)."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_alerts_repository,
    get_pipeline_service,
    get_portfolio_service,
    get_trader_service,
)
from app.api.templating import templates
from app.api.watchlist_context import (
    build_freshness_context,
    build_watchlist_context,
    load_source_health,
)
from app.core.security import require_local_or_token
from app.repositories.alerts_repo import AlertsRepository
from app.services.pipeline_service import PipelineService
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
PipelineDep = Annotated[PipelineService, Depends(get_pipeline_service)]
AlertsDep = Annotated[AlertsRepository, Depends(get_alerts_repository)]


@router.get("/pipeline-status", response_class=HTMLResponse)
async def pipeline_status(request: Request) -> HTMLResponse:
    """Render the bottom status bar without blocking a running refresh."""
    return templates.TemplateResponse(
        request,
        "_pipeline_status.html",
        {
            "status": PipelineService.status(),
            "source_health": list(load_source_health().values()),
            **build_freshness_context(),
        },
    )


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
    alerts: AlertsDep,
    confirm_missing: bool = Form(False),
    extract: bool = Form(False),
) -> HTMLResponse:
    """Refresh the analysis dataset by running the pipeline once.

    When ``extract`` is set the run refreshes institutional sources
    (WhaleWisdom/StockTwits) instead of reusing the cached extraction file.
    """
    warnings = pipeline.missing_configuration()
    if warnings and not confirm_missing:
        response = templates.TemplateResponse(
            request,
            "_pipeline_confirmation.html",
            context={"warnings": warnings, "extract": extract},
        )
        response.headers["HX-Retarget"] = "#pipeline-confirmation"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    result = await asyncio.to_thread(pipeline.run_once, extract)
    refresh_status = (
        "Data refreshed successfully" if result.success else "Data refresh failed"
    )
    return templates.TemplateResponse(
        request,
        "_watchlist.html",
        context=build_watchlist_context(
            trader,
            portfolio,
            alerts,
            refresh_status=refresh_status,
            refresh_success=result.success,
            refresh_details="" if result.success else result.details,
            refresh_stages=(
                [
                    "Sources and market data collected",
                    "Stocks analysed and scored",
                    "Alerts, history, and exports updated",
                ]
                if result.success
                else []
            ),
        ),
    )
