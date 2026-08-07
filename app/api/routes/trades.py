"""Trade-action routes — record and delete trades (money-mutating)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import get_portfolio_service, get_trader_service
from app.api.params import optional_int
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]


def _reject_missing_portfolio(
    request: Request, portfolio: PortfolioService, portfolio_id: int | None
) -> HTMLResponse:
    """Render the portfolio partial with a "select a portfolio" error banner."""
    context = portfolio.default_portfolio_context(portfolio_id)
    context["error_message"] = (
        "Select a portfolio before recording trades. Create one if you have none."
    )
    return templates.TemplateResponse(
        request, "_portfolio.html", context=context, status_code=400
    )


@router.post("/trades", dependencies=[Depends(require_local_or_token)])
async def record_trade(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    ticker: Annotated[str, Form()],
    action: Annotated[str, Form()],
    shares: Annotated[float, Form()],
    price: Annotated[float, Form()],
    date: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    stop_loss: Annotated[float | None, Form()] = None,
    entry_price: Annotated[float | None, Form()] = None,
    portfolio_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Record a BUY, SELL, or CORRECT action and return the updated portfolio.

    The trade targets the selected portfolio; a missing or unknown id is
    rejected with an error banner and nothing is written (#147).
    """
    pid = optional_int(portfolio_id)
    if pid is None or not trader.portfolio_exists(pid):
        return _reject_missing_portfolio(request, portfolio, pid)
    if action == "BUY":
        trader.record_buy(
            ticker, shares, price, date, notes, stop_loss, entry_price, pid
        )
    elif action == "SELL":
        trader.record_sell(ticker, shares, price, date, notes, pid)
    elif action == "CORRECT":
        trader.correct_trade(
            ticker, shares, price, date, notes, stop_loss, entry_price, pid
        )
    else:
        logger.warning("unsupported trade action: %s", action)
    context = portfolio.default_portfolio_context(pid)
    return templates.TemplateResponse(request, "_portfolio.html", context=context)


@router.delete("/trades/{trade_id}", dependencies=[Depends(require_local_or_token)])
async def delete_trade(trade_id: int, trader: TraderDep) -> RedirectResponse:
    """Delete a trade by ID and redirect to the history partial."""
    trader.delete_trade(trade_id)
    return RedirectResponse("/partials/history", status_code=303)
