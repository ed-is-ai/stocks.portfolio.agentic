from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.repositories.backtest_repo import BacktestIntegrityError
from app.services.backtest.backtest_engine import (
    EntryFillEventV1,
    EquityCurvePointV1,
    ExitFillEventV1,
)
from app.services.backtest.result_presenter import (
    multi_comparison_equity_payload,
    performance_summary_view,
)


def _point(
    day: int, *, equity: str, cash: str, positions: str, sequence: int
) -> EquityCurvePointV1:
    return EquityCurvePointV1(
        session=date(2024, 1, day),
        cash_base=Decimal(cash),
        positions_value_base=Decimal(positions),
        total_equity_base=Decimal(equity),
        sequence=sequence,
    )


def _result(
    run_id: str,
    curve: tuple[EquityCurvePointV1, ...],
    events: tuple[object, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        strategy_id=f"strategy-{run_id}",
        strategy_api_version=1,
        starting_capital=Decimal("10000"),
        equity_curve=curve,
        events=events,
    )


def _entry() -> EntryFillEventV1:
    return EntryFillEventV1(
        security_id="security-1",
        signal_session=date(2024, 1, 2),
        fill_session=date(2024, 1, 3),
        rule_id="entry-rule",
        shares=10,
        fill_price_native=Decimal("400"),
        fill_currency="GBP",
        fill_quote_unit="1",
        cost_base=Decimal("4000"),
        sequence=1,
    )


def _exit() -> ExitFillEventV1:
    return ExitFillEventV1(
        security_id="security-1",
        signal_session=date(2024, 1, 20),
        fill_session=date(2024, 1, 21),
        rule_id="exit-rule",
        shares=10,
        fill_price_native=Decimal("450"),
        fill_currency="GBP",
        fill_quote_unit="1",
        proceeds_base=Decimal("4500"),
        cost_basis_base=Decimal("4000"),
        realized_pnl_base=Decimal("500"),
        sequence=2,
    )


def test_performance_summary_uses_equity_curve_and_executed_fills() -> None:
    result = _result(
        "run-1",
        (
            _point(1, equity="10000", cash="10000", positions="0", sequence=1),
            _point(2, equity="9000", cash="4500", positions="4500", sequence=2),
            _point(31, equity="11000", cash="6000", positions="5000", sequence=3),
        ),
        (_entry(), _exit()),
    )

    summary = performance_summary_view(result)  # type: ignore[arg-type]

    assert summary.cagr.value.startswith("+")
    assert summary.mean_invested_exposure.value == "31.82%"
    assert summary.time_invested.value == "66.67%"
    assert summary.turnover.value == "0.85×"
    assert summary.exit_count.value == "1"


def test_multi_comparison_indexes_each_curve_and_tracks_peak_drawdown() -> None:
    dates = (
        _point(1, equity="10000", cash="10000", positions="0", sequence=1),
        _point(2, equity="9000", cash="9000", positions="0", sequence=2),
        _point(31, equity="11000", cash="11000", positions="0", sequence=3),
    )
    peer_dates = (
        _point(1, equity="20000", cash="20000", positions="0", sequence=1),
        _point(2, equity="21000", cash="21000", positions="0", sequence=2),
        _point(31, equity="18000", cash="18000", positions="0", sequence=3),
    )

    payload = multi_comparison_equity_payload(
        (_result("run-1", dates), _result("run-2", peer_dates))  # type: ignore[arg-type]
    )

    assert payload["dates"] == ("2024-01-01", "2024-01-02", "2024-01-31")
    series = payload["series"]
    assert series[0]["values"] == (100.0, 90.0, 110.0)
    assert series[0]["drawdowns"] == (0.0, -10.0, 0.0)
    assert series[1]["values"] == (100.0, 105.0, 90.0)
    assert series[1]["equity_values"] == (20000.0, 21000.0, 18000.0)
    assert series[1]["drawdowns"] == (0.0, 0.0, -14.29)
    assert payload["table_rows"][1]["values"] == ("90.00", "105.00")
    assert payload["table_rows"][1]["equities"] == ("9,000.00", "21,000.00")
    assert payload["table_rows"][2]["drawdowns"] == ("0.00%", "-14.29%")


def test_multi_comparison_fails_closed_on_divergent_session_dates() -> None:
    left = _result(
        "run-1",
        (_point(1, equity="10000", cash="10000", positions="0", sequence=1),),
    )
    right = _result(
        "run-2",
        (_point(2, equity="10000", cash="10000", positions="0", sequence=1),),
    )

    with pytest.raises(BacktestIntegrityError, match="session dates do not match"):
        multi_comparison_equity_payload((left, right))  # type: ignore[arg-type]


def test_multi_comparison_fails_closed_when_equity_cannot_be_displayed() -> None:
    huge_curve = (
        _point(
            1,
            equity="1000000000000000000000000000000",
            cash="1000000000000000000000000000000",
            positions="0",
            sequence=1,
        ),
    )
    left = _result("run-1", huge_curve)
    right = _result("run-2", huge_curve)

    with pytest.raises(
        BacktestIntegrityError, match="cannot be represented for display"
    ):
        multi_comparison_equity_payload((left, right))  # type: ignore[arg-type]
