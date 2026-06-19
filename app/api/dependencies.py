"""FastAPI dependency providers for the service layer.

Routes obtain services via these providers (``Depends``), so they never
instantiate ``TraderAgent`` or reach into repositories directly. The providers
are cached so each service is a singleton for the process, matching the previous
module-level ``trader`` instance.
"""

from functools import lru_cache

from app.services.pipeline_service import PipelineService
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


@lru_cache
def get_trader_service() -> TraderService:
    """Return the shared ``TraderService`` instance."""
    return TraderService()


@lru_cache
def get_portfolio_service() -> PortfolioService:
    """Return the shared ``PortfolioService`` instance."""
    return PortfolioService(get_trader_service())


@lru_cache
def get_pipeline_service() -> PipelineService:
    """Return the shared ``PipelineService`` instance."""
    return PipelineService()
