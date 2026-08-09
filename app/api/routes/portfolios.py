"""Portfolio-management routes — create / rename / delete accounts (#147).

Selection is client-side (localStorage); these endpoints own the server-side
lifecycle. Each returns the scoped Portfolio partial so htmx can swap the tab
straight after a mutation. A newly created portfolio is returned as the active
one; the template exposes its id so the browser can persist the selection.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
NotificationsDep = Annotated[
    NotificationsRepository, Depends(get_notifications_repository)
]


def _render(
    request: Request, portfolio: PortfolioService, portfolio_id: int | None
) -> HTMLResponse:
    """Render the Portfolio partial scoped to ``portfolio_id``."""
    context = portfolio.default_portfolio_context(portfolio_id)
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.post(
    "/portfolios",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def create_portfolio(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    name: Annotated[str, Form()],
    opening_cash: Annotated[float, Form()] = 0.0,
) -> HTMLResponse:
    """Create a portfolio (optionally with opening cash) and show it."""
    name = name.strip()
    if not name:
        context = portfolio.default_portfolio_context()
        context["error_message"] = "Enter a name for the new portfolio."
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=400
        )
    created = trader.create_portfolio(name, max(0.0, opening_cash))
    logger.info("Created portfolio %s (id=%s)", created.name, created.id)
    return _render(request, portfolio, created.id)


@router.post(
    "/portfolios/{portfolio_id}/rename",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def rename_portfolio(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    portfolio_id: int,
    name: Annotated[str, Form()],
) -> HTMLResponse:
    """Rename a portfolio (duplicate names allowed)."""
    if not trader.rename_portfolio(portfolio_id, name):
        context = portfolio.default_portfolio_context()
        context["error_message"] = "That portfolio no longer exists."
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=404
        )
    return _render(request, portfolio, portfolio_id)


@router.post(
    "/portfolios/{portfolio_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def delete_portfolio(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    notifications: NotificationsDep,
    portfolio_id: int,
) -> HTMLResponse:
    """Hard-delete a portfolio's trades/cash flows/balance, then show what
    remains.

    Deleting the last portfolio is allowed and lands on the empty state.
    Prior notification-centre events for this account (e.g. SIPP import
    events) are kept, not deleted — they're history, not live account
    data — and a new event records the deletion itself for an audit trail
    (#186).
    """
    meta = trader.get_portfolio_meta(portfolio_id)
    cash_balance = trader.get_cash_balance(portfolio_id)
    trader.delete_portfolio(portfolio_id)
    try:
        name = meta.name if meta else f"portfolio {portfolio_id}"
        trade_count = meta.trade_count if meta else 0
        cash_flow_count = meta.cash_flow_count if meta else 0
        notifications.record(
            NotificationCategory.PORTFOLIO,
            "portfolio_deleted",
            f"Portfolio deleted — {name}",
            severity=NotificationSeverity.WARNING,
            body=(
                f"Removed {trade_count} trade(s), {cash_flow_count} cash "
                f"flow(s); cash balance was £{cash_balance or 0:,.2f}."
            ),
            portfolio_id=portfolio_id,
        )
    except Exception:
        logger.exception("Failed to record portfolio-deletion notification")
    logger.info("Deleted portfolio id=%s", portfolio_id)
    # Fall back to the first remaining portfolio (or the empty state).
    return _render(request, portfolio, None)
