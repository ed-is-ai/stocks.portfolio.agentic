"""FastAPI dependency providers for the service layer.

Routes obtain services via these providers (``Depends``), so they never
instantiate ``TraderAgent`` or reach into repositories directly. The providers
are cached so each service is a singleton for the process, matching the previous
module-level ``trader`` instance.
"""

from functools import lru_cache

from app.core.config import ALERTS_DB
from app.repositories import db
from app.repositories.alerts_repo import AlertsRepository
from app.services.pipeline_service import PipelineService
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


@lru_cache
def get_trader_service() -> TraderService:
    """Return the shared ``TraderService`` instance."""
    return TraderService()


@lru_cache
def get_alerts_repository() -> AlertsRepository:
    """Return the shared ``AlertsRepository`` instance.

    Used by the watchlist views to surface each ticker's alert
    cooldown/suppression state (#58) alongside ``AlertAgent``, which owns
    the table's schema and writes.
    """
    connect = db.make_connect(lambda: str(ALERTS_DB))
    repo = AlertsRepository(connect)
    repo.ensure_schema()
    return repo


@lru_cache
def get_portfolio_service() -> PortfolioService:
    """Return the shared ``PortfolioService`` instance."""
    return PortfolioService(get_trader_service())


@lru_cache
def get_pipeline_service() -> PipelineService:
    """Return the shared ``PipelineService`` instance."""
    return PipelineService()
