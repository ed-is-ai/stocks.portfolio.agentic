"""Typed Agent entry point for primary Strategy Manager job submission."""

from pydantic import ConfigDict

from app.agents.base import Agent
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchCommandV1,
    BacktestLaunchService,
)
from app.services.backtest.strategy_job import (
    BacktestEnqueueResultV1,
    PreparationEnqueueResultV1,
)


class StrategyManagerAgent(Agent):
    """Synchronously validate and dispatch through the existing launch service.

    ``run`` returns the durable enqueue result (including its job handle),
    not a completed backtest. The service owns validation and the choice of
    preparation or backtest; the existing dispatcher and worker own FIFO
    execution, leases, progress, cancellation and recovery. Bootstrap,
    initialization and polling keep their existing entry points.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    launch_service: BacktestLaunchService

    def run(
        self, payload: BacktestLaunchCommandV1
    ) -> BacktestEnqueueResultV1 | PreparationEnqueueResultV1:
        return self.launch_service.launch(payload)
