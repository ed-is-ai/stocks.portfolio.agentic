"""Portfolio routes — live price refresh."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import get_portfolio_service, get_trader_service
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

router = APIRouter()
logger = logging.getLogger(__name__)

TraderDep = Annotated[TraderService, Depends(get_trader_service)]
PortfolioDep = Annotated[PortfolioService, Depends(get_portfolio_service)]


@router.post(
    "/api/portfolio/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def refresh_portfolio_prices(
    request: Request, trader: TraderDep, portfolio: PortfolioDep
) -> HTMLResponse:
    """Fetch live prices from yfinance and return the updated portfolio partial."""
    try:
        positions = trader.get_portfolio()
        logger.info("Refreshing %d positions", len(positions))
        if not positions:
            context = portfolio.portfolio_partial_context(
                [], error_message="No positions to refresh"
            )
            return templates.TemplateResponse(
                request, "_portfolio.html", context=context, status_code=400
            )

        gbpusd = portfolio.gbpusd_rate()
        tickers = [p.ticker for p in positions]
        gbp_prices, display_info = portfolio.fetch_all_prices(
            tickers, portfolio.load_ticker_aliases(), gbpusd
        )

        trader.save_price_cache(gbp_prices, display_info)
        _, prices_as_of, _ = trader.load_price_cache()
        updated_positions = trader.refresh_portfolio_prices(gbp_prices, display_info)
        logger.info("Refreshed %d positions successfully", len(updated_positions))
        cash_balance = trader.get_cash_balance()
        trader.update_portfolio_snapshot(cash_balance)
        context = portfolio.portfolio_partial_context(
            updated_positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            cash_balance=cash_balance,
        )
        return templates.TemplateResponse(request, "_portfolio.html", context=context)
    except Exception as e:
        logger.exception("Failed to refresh portfolio prices: %s", e)
        cached_prices, prices_as_of, display_info = trader.load_price_cache()
        cached_positions = trader.get_portfolio(
            cached_prices or None, display_info or None
        )
        gbpusd = cached_prices.get("__GBPUSD__")
        context = portfolio.portfolio_partial_context(
            cached_positions,
            prices_as_of=prices_as_of,
            gbpusd_rate=gbpusd,
            error_message=f"Failed to fetch prices: {e}",
        )
        return templates.TemplateResponse(
            request, "_portfolio.html", context=context, status_code=500
        )
