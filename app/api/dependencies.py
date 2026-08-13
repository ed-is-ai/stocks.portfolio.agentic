"""FastAPI dependency providers for the service layer.

Routes obtain services via these providers (``Depends``), so they never
instantiate ``TraderAgent`` or reach into repositories directly. The providers
are cached so each service is a singleton for the process, matching the previous
module-level ``trader`` instance.
"""

from functools import lru_cache

from app.core import config
from app.core.config import ALERTS_DB
from app.repositories import db
from app.repositories.alerts_repo import AlertsRepository
from app.repositories.notifications_repo import NotificationsRepository
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.integration_config_service import IntegrationConfigService
from app.services.pipeline_service import PipelineService
from app.services.portfolio_service import PortfolioService
from app.services.realised_pnl_service import RealisedPnlService
from app.services.trader_service import TraderService
from app.services.backtest.strategy_job_service import StrategyJobService


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
def get_notifications_repository() -> NotificationsRepository:
    """Return the shared ``NotificationsRepository`` instance (#80).

    Backs the notification-centre bell/dropdown. The orchestrator and
    ``AlertAgent`` write to the same store via
    ``build_notifications_repository``.
    """
    connect = db.make_connect(lambda: str(config.NOTIFICATIONS_DB))
    repo = NotificationsRepository(connect)
    repo.ensure_schema()
    return repo


@lru_cache
def get_backtest_repository() -> BacktestRepository:
    """Return the process-shared durable Strategy Manager repository."""
    repo = BacktestRepository(db.make_connect(lambda: str(config.BACKTEST_DB)))
    repo.ensure_schema()
    return repo


@lru_cache
def get_historical_price_repository() -> HistoricalPriceRepository:
    """Return the immutable provider-evidence repository."""
    repo = HistoricalPriceRepository(
        db.make_connect(lambda: str(config.HISTORICAL_PRICE_CACHE))
    )
    repo.ensure_schema()
    return repo


@lru_cache
def get_strategy_job_service() -> StrategyJobService:
    """Return the one process-local dispatcher over the durable FIFO ledger."""
    return StrategyJobService(get_backtest_repository())


@lru_cache
def get_portfolio_service() -> PortfolioService:
    """Return the shared ``PortfolioService`` instance."""
    return PortfolioService(get_trader_service())


@lru_cache
def get_realised_pnl_service() -> RealisedPnlService:
    """Return the shared ``RealisedPnlService`` instance."""
    return RealisedPnlService(get_trader_service(), get_portfolio_service())


@lru_cache
def get_pipeline_service() -> PipelineService:
    """Return the shared ``PipelineService`` instance."""
    return PipelineService()


@lru_cache
def get_integration_config_service() -> IntegrationConfigService:
    """Return the shared ``IntegrationConfigService`` instance."""
    return IntegrationConfigService()
