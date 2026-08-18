"""Minimal fixture Strategy proving ``StrategyProtocolV1`` conformance.

Deterministic and in-process: ``entry_signals`` emits one BUY for the
parameterized watch security; ``exit_signals`` emits one SELL per held
position in the supplied ``PortfolioView``; ``position_size`` returns a
parameterized fixed integer share count. This fixture exists only to
exercise the Story 2.1 protocol boundary end to end -- it is not a real
trading rule and is intentionally excluded from ``skills/`` so it can never
be discovered as a live Skill (Story 2.2's discovery mechanism).
"""

from __future__ import annotations

from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "fixture_minimal_v1"
STRATEGY_API_VERSION = 1


class MinimalStrategy:
    """Deterministic reference implementation of ``StrategyProtocolV1``."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        watch_security_id = parameters.get("watch_security_id")
        if watch_security_id is None:
            raise TypeError("watch_security_id parameter is required")
        watch_security_id = str(watch_security_id)
        return [
            Signal(
                security_id=watch_security_id,
                side=SignalSide.BUY,
                session=view.as_of_session,
                rule_id="fixture_entry_v1",
            )
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        return [
            Signal(
                security_id=position.security_id,
                side=SignalSide.SELL,
                session=view.as_of_session,
                rule_id="fixture_exit_v1",
            )
            for position in portfolio.positions
        ]

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        fixed_shares = parameters.get("fixed_shares", 1)
        if isinstance(fixed_shares, bool) or not isinstance(fixed_shares, int):
            raise TypeError("fixed_shares parameter must be a plain int")
        return fixed_shares
