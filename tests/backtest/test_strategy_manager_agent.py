"""The Agent submits jobs synchronously without taking over worker execution."""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.agents.base import Agent
from app.agents.strategy_manager import StrategyManagerAgent
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchCommandV1,
    BacktestLaunchService,
    BacktestLaunchValidationError,
    LaunchFieldError,
)
from app.services.backtest.strategy_job import (
    BacktestEnqueueResultV1,
    PreparationEnqueueResultV1,
    StrategyJobConflict,
)


def command() -> BacktestLaunchCommandV1:
    return BacktestLaunchCommandV1(
        strategy_id="buy-and-hold",
        rendered_profile_hash="a" * 64,
        start_month="2024-01",
        end_month="2024-03",
        base_currency="GBP",
        starting_capital=Decimal("10000"),
        parameters={},
    )


@pytest.mark.parametrize(
    "result_type", [BacktestEnqueueResultV1, PreparationEnqueueResultV1]
)
def test_run_returns_the_existing_enqueue_result_without_waiting(result_type):
    service = Mock(spec=BacktestLaunchService)
    result = Mock(spec=result_type)
    service.launch.return_value = result
    agent = StrategyManagerAgent(name="strategy_manager", launch_service=service)
    payload = command()

    assert isinstance(agent, Agent)
    assert agent.run(payload) is result
    service.launch.assert_called_once_with(payload)
    assert service.mock_calls == [("launch", (payload,), {})]


@pytest.mark.parametrize(
    "error",
    [
        BacktestLaunchValidationError(
            (LaunchFieldError("parameters", "Invalid value"),)
        ),
        StrategyJobConflict("Profile changed"),
    ],
)
def test_run_preserves_service_error_identity_without_retry(error):
    service = Mock(spec=BacktestLaunchService)
    service.launch.side_effect = error
    agent = StrategyManagerAgent(name="strategy_manager", launch_service=service)
    payload = command()

    with pytest.raises(type(error)) as caught:
        agent.run(payload)

    assert caught.value is error
    service.launch.assert_called_once_with(payload)
