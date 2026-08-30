"""Portfolio-management routes — create / rename / delete accounts (#147).

Selection is client-side (localStorage); these endpoints own the server-side
lifecycle. Each returns the scoped Portfolio partial so htmx can swap the tab
straight after a mutation. A newly created portfolio is returned as the active
one; the template exposes its id so the browser can persist the selection.
"""

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_strategy_assignment_service,
    get_trader_service,
)
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.services.portfolio_service import PortfolioService
from app.services.strategy_assignment_service import (
    IncompatibleStrategyError,
    StrategyAssignmentService,
    UnknownStrategyError,
)
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]
NotificationsDep = Annotated[
    NotificationsRepository, Depends(get_notifications_repository)
]
StrategyAssignmentDep = Annotated[
    StrategyAssignmentService, Depends(get_strategy_assignment_service)
]


def _render(
    request: Request, portfolio: PortfolioService, portfolio_id: int | None
) -> HTMLResponse:
    """Render the Portfolio partial scoped to ``portfolio_id``."""
    context = portfolio.default_portfolio_context(portfolio_id)
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


def _strategy_warning(
    request: Request,
    portfolio: PortfolioService,
    portfolio_id: int | None,
    message: str,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Re-render the Portfolio partial with a visible warning (#440).

    Assignment failures are user-visible states, never 500s: the stored
    assignment is left untouched and the tab re-renders with the message.
    """
    context = portfolio.default_portfolio_context(portfolio_id)
    context["warning_message"] = message
    return templates.TemplateResponse(
        request, "_portfolio.html", context=context, status_code=status_code
    )


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


@router.post(
    "/portfolios/{portfolio_id}/strategy",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def assign_strategy(
    request: Request,
    portfolio: PortfolioDep,
    assignment: StrategyAssignmentDep,
    portfolio_id: int,
    strategy_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Assign one Strategy to the portfolio and re-render the partial.

    Persistence + validation only — never launches a backtest, scan, email,
    or trade. An unknown/incompatible Strategy, a missing form field, or a
    portfolio that no longer exists re-renders the partial with a visible
    warning (200, never 500) and leaves any stored assignment untouched
    (#440).
    """
    if not strategy_id or not strategy_id.strip():
        return _strategy_warning(
            request, portfolio, portfolio_id, "No Strategy selected."
        )
    try:
        assignment.assign(portfolio_id, strategy_id.strip())
    except sqlite3.IntegrityError:
        logger.warning(
            "Strategy assignment rejected: portfolio id=%s no longer exists",
            portfolio_id,
        )
        return _strategy_warning(
            request,
            portfolio,
            portfolio_id,
            "That portfolio no longer exists.",
            status_code=404,
        )
    except (UnknownStrategyError, IncompatibleStrategyError) as exc:
        logger.warning(
            "Strategy assignment rejected for portfolio %s: %s", portfolio_id, exc
        )
        return _strategy_warning(
            request, portfolio, portfolio_id, f"Could not assign Strategy: {exc}"
        )
    logger.info(
        "Assigned Strategy %r to portfolio id=%s", strategy_id.strip(), portfolio_id
    )
    return _render(request, portfolio, portfolio_id)


@router.post(
    "/portfolios/{portfolio_id}/strategy/clear",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def clear_strategy(
    request: Request,
    portfolio: PortfolioDep,
    assignment: StrategyAssignmentDep,
    portfolio_id: int,
) -> HTMLResponse:
    """Clear the portfolio's Strategy assignment (idempotent) and re-render."""
    assignment.clear(portfolio_id)
    logger.info("Cleared Strategy assignment for portfolio id=%s", portfolio_id)
    return _render(request, portfolio, portfolio_id)
