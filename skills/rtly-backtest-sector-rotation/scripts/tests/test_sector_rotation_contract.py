"""Contract tests for the sector-rotation backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd

from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    SignalSide,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
)

_RUNTIME = Path(__file__).resolve().parents[1] / "strategy.py"
_SPEC = spec_from_file_location("sector_rotation_backtest_strategy", _RUNTIME)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SectorRotationStrategy = _MODULE.SectorRotationStrategy

REBALANCE = date(2026, 2, 2)
MID_MONTH = date(2026, 2, 3)
SESSIONS = [date(2026, 1, 29), date(2026, 1, 30), REBALANCE]
ASSIGNMENTS = {
    "a1": "Tech",
    "a2": "Tech",
    "b1": "Energy",
    "b2": "Energy",
    "c1": "Utilities",
    "c2": "Utilities",
}
# Lookback of 2 sessions -> total return = last / first - 1.
CLOSES = {
    "a1": ["100", "110", "130"],
    "a2": ["100", "105", "120"],
    "b1": ["100", "100", "110"],
    "b2": ["100", "100", "105"],
    "c1": ["100", "99", "98"],
    "c2": ["100", "99", "97"],
    "u1": ["100", "130", "200"],
}
PARAMETERS = {
    "selected_securities": sorted([*ASSIGNMENTS, "u1"]),
    "sector_assignments": ASSIGNMENTS,
    "top_n_sectors": 1,
    "momentum_lookback_sessions": 2,
    "rebalance_cadence": "monthly",
    "min_sector_members": 2,
    "maximum_loss_pct": 15,
}


class _View:
    def __init__(
        self,
        as_of: date = REBALANCE,
        closes: dict[str, list[str]] | None = None,
        sessions: list[date] | None = None,
    ) -> None:
        self.as_of_session = as_of
        self._closes = CLOSES if closes is None else closes
        self._sessions = SESSIONS if sessions is None else sessions

    def price_history(
        self,
        security_id: str,
        *,
        limit: int | None = None,
        columns: object | None = None,
    ) -> pd.DataFrame | None:
        if security_id not in self._closes:
            return None
        frame = pd.DataFrame(
            {"close": [Decimal(v) for v in self._closes[security_id]]},
            index=self._sessions,
        )
        return frame if limit is None else frame.iloc[-limit:]

    def scan_result(self, security_id: str) -> None:
        return None


def _portfolio(**held: str) -> PortfolioView:
    return PortfolioView(
        as_of_session=REBALANCE,
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=tuple(
            PositionSummaryV1(
                security_id=sid, quantity=Decimal("5"), average_cost=Decimal(cost)
            )
            for sid, cost in held.items()
        ),
        volatility_observations=(),
    )


def test_buys_only_leading_sector_members_ranked_by_own_return() -> None:
    strategy = SectorRotationStrategy()
    view = _View()

    signals = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert [s.security_id for s in signals] == ["a1", "a2"]
    assert all(s.side == SignalSide.BUY and s.explanation for s in signals)
    assert [s.priority for s in signals] == [Decimal(2), Decimal(1)]
    assert signals == validate_entry_signals(strategy.entry_signals(view, PARAMETERS))


def test_unmapped_security_is_excluded_even_with_best_return() -> None:
    signals = SectorRotationStrategy().entry_signals(_View(), PARAMETERS)

    assert "u1" not in {s.security_id for s in signals}


def test_thin_sector_is_not_rankable() -> None:
    params = {**PARAMETERS, "min_sector_members": 3}

    assert SectorRotationStrategy().entry_signals(_View(), params) == []


def test_missing_or_malformed_assignments_fail_closed() -> None:
    strategy = SectorRotationStrategy()
    without = {k: v for k, v in PARAMETERS.items() if k != "sector_assignments"}

    assert strategy.entry_signals(_View(), without) == []
    assert strategy.entry_signals(_View(), {**without, "sector_assignments": []}) == []
    assert (
        strategy.entry_signals(_View(), {**without, "sector_assignments": {"a1": 1}})
        == []
    )


def test_non_rebalance_session_emits_no_rotation_signal() -> None:
    sessions = [date(2026, 2, 1), date(2026, 2, 2), MID_MONTH]
    view = _View(as_of=MID_MONTH, sessions=sessions)
    strategy = SectorRotationStrategy()

    assert strategy.entry_signals(view, PARAMETERS) == []
    assert strategy.exit_signals(view, _portfolio(c1="100"), PARAMETERS) == []


def test_quarterly_cadence_ignores_month_change_within_quarter() -> None:
    params = {**PARAMETERS, "rebalance_cadence": "quarterly"}

    assert SectorRotationStrategy().entry_signals(_View(), params) == []


def test_held_security_sells_when_sector_leaves_leaders() -> None:
    exits = validate_exit_signals(
        SectorRotationStrategy().exit_signals(
            _View(), _portfolio(a1="100", c1="100"), PARAMETERS
        )
    )

    assert [(s.security_id, s.side) for s in exits] == [("c1", SignalSide.SELL)]
    assert exits[0].explanation is not None


def test_loss_stop_fires_on_non_rebalance_session() -> None:
    sessions = [date(2026, 2, 1), date(2026, 2, 2), MID_MONTH]
    view = _View(as_of=MID_MONTH, sessions=sessions)

    exits = validate_exit_signals(
        SectorRotationStrategy().exit_signals(
            view, _portfolio(a1="200", a2="100"), PARAMETERS
        )
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("a1", "sector_rotation_maximum_loss_stop_v1")
    ]


def test_stale_history_is_ignored() -> None:
    view = _View(sessions=[date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30)])

    assert SectorRotationStrategy().entry_signals(view, PARAMETERS) == []


def test_stop_level_and_position_size() -> None:
    strategy = SectorRotationStrategy()
    portfolio = _portfolio(a1="200")

    stop = strategy.stop_level(_View(), portfolio, PARAMETERS, "a1")
    buy = strategy.entry_signals(_View(), PARAMETERS)[0]
    sell = strategy.exit_signals(_View(), _portfolio(c1="100"), PARAMETERS)[0]

    assert stop is not None and stop.level == Decimal("170")
    assert strategy.stop_level(_View(), portfolio, PARAMETERS, "a2") is None
    assert strategy.position_size(buy, _View(), portfolio, PARAMETERS) == 0
    assert strategy.position_size(
        sell, _View(), _portfolio(c1="100"), PARAMETERS
    ) == Decimal("5")


def test_evidence_requirements_follow_lookback() -> None:
    requirements = SectorRotationStrategy().evidence_requirements(PARAMETERS)

    assert requirements.entry[0].minimum_sessions == 3
