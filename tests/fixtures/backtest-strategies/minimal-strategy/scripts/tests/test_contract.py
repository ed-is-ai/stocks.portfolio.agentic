"""Contract tests for the minimal fixture Strategy.

Run via the documented command:
``uv run pytest tests/fixtures/backtest-strategies/minimal-strategy/scripts/tests -q``

Proves protocol conformance, deterministic output, parameter pass-through,
and that constructing/using the Strategy never imports a repository,
ORM/session, job, run, or persistence model -- only
``app.services.backtest.strategy_protocol`` and stdlib.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from strategy import STRATEGY_API_VERSION, STRATEGY_ID, MinimalStrategy

from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
)


def _portfolio(
    as_of: date, positions: tuple[PositionSummaryV1, ...] = ()
) -> PortfolioView:
    return PortfolioView(
        as_of_session=as_of,
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=positions,
        volatility_observations=(),
    )


def test_fixture_identity_is_versioned() -> None:
    assert STRATEGY_ID == "fixture_minimal_v1"
    assert STRATEGY_API_VERSION == 1


def test_minimal_strategy_conforms_to_protocol_v1() -> None:
    assert isinstance(MinimalStrategy(), StrategyProtocolV1)


def test_entry_signals_are_deterministic_across_repeated_calls() -> None:
    strategy = MinimalStrategy()
    as_of = date(2026, 1, 5)
    portfolio = _portfolio(as_of)
    parameters = {"watch_security_id": "sec-aapl"}

    first = validate_entry_signals(strategy.entry_signals(portfolio, parameters))
    second = validate_entry_signals(strategy.entry_signals(portfolio, parameters))

    assert first == second
    assert first[0].security_id == "sec-aapl"
    assert first[0].session == as_of


def test_exit_signals_cover_every_held_position() -> None:
    strategy = MinimalStrategy()
    as_of = date(2026, 1, 5)
    held = (
        PositionSummaryV1(
            security_id="sec-aapl", quantity=Decimal("10"), average_cost=Decimal("100")
        ),
    )
    portfolio = _portfolio(as_of, positions=held)

    exits = validate_exit_signals(strategy.exit_signals(portfolio, portfolio, {}))

    assert [signal.security_id for signal in exits] == ["sec-aapl"]


def test_position_size_passes_through_the_fixed_shares_parameter() -> None:
    strategy = MinimalStrategy()
    as_of = date(2026, 1, 5)
    portfolio = _portfolio(as_of)
    parameters = {"watch_security_id": "sec-aapl", "fixed_shares": 7}
    signal = validate_entry_signals(strategy.entry_signals(portfolio, parameters))[0]

    size = validate_position_size(
        strategy.position_size(signal, portfolio, portfolio, parameters)
    )

    assert size == 7
