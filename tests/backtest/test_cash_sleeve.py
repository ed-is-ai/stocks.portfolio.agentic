"""#140: idle cash held in a benchmark sleeve (SPY) by the backtest engine."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from app.services.backtest.backtest_engine import (
    CashSleeveTradeEventV1,
    DividendAppliedEventV1,
    EntryFillEventV1,
    SimulationError,
    SimulationOutputV1,
    run_simulation,
)
from app.services.backtest.strategy_protocol import SignalSide
from tests.backtest.test_backtest_engine import (
    _ScriptedStrategy,
    _build_security,
    _buy,
    _manifest,
    _market_view_factory,
    _month_str,
    _sessions,
)

MARCH = _sessions("XNYS", date(2024, 3, 1), date(2024, 4, 1))
D0, D1, D2 = MARCH[0], MARCH[1], MARCH[2]


def _revision(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _run(
    *,
    entries=None,
    sleeve: bool = True,
    sleeve_prices=None,
    sleeve_dividends=None,
    sleeve_id: str = "spy",
) -> SimulationOutputV1:
    """March 2024, one flat 100.00 stock, 10,000 USD, ten equal slots."""
    stock, pinned = _build_security(
        "sec-a", "XNYS", MARCH, revision=_revision("sec-a"), open_price=100.0
    )
    spy, _ = _build_security(
        sleeve_id,
        "XNYS",
        MARCH,
        revision=_revision("spy"),
        open_price=50.0,
        close_price=50.0,
        price_overrides=sleeve_prices,
        dividend_by_session=sleeve_dividends,
    )
    return run_simulation(
        manifest=_manifest(
            securities=(pinned,),
            start_month=_month_str(D0),
            end_month=_month_str(D0),
            starting_capital=Decimal("10000"),
            parameters={"max_concurrent_positions": 10},
        ),
        strategy=_ScriptedStrategy(entries=entries or {}, default_size=-1),
        market_view_factory=_market_view_factory(),
        security_market_data=(stock,),
        cash_sleeve=spy if sleeve else None,
    )


def _sleeve_trades(output: SimulationOutputV1) -> list[tuple[date, SignalSide, int]]:
    return [
        (event.session, event.side, event.shares)
        for event in output.cash_sleeve_events
        if isinstance(event, CashSleeveTradeEventV1)
    ]


def test_without_a_sleeve_nothing_is_reported_and_cash_stays_idle() -> None:
    output = _run(sleeve=False)

    assert output.cash_sleeve_events == ()
    assert output.cash_sleeve_values == ()
    assert output.final_cash_base == Decimal("10000.00000000")


def test_idle_cash_buys_whole_sleeve_shares_at_the_first_open() -> None:
    output = _run()

    assert _sleeve_trades(output)[0] == (D0, SignalSide.BUY, 200)
    first = output.equity_curve[0]
    assert first.cash_base == Decimal("0")
    assert first.positions_value_base == Decimal("0")
    assert first.total_equity_base == Decimal("10000.00000000")
    assert output.cash_sleeve_values[0].shares == Decimal(200)


def test_sleeve_gains_count_in_total_equity_but_not_strategy_positions() -> None:
    output = _run(sleeve_prices={D1: (50.0, 55.0)})

    point = output.equity_curve[1]
    assert point.session == D1
    assert point.positions_value_base == Decimal("0")
    assert point.total_equity_base == Decimal("11000.00000000")


def test_entry_is_funded_by_selling_just_enough_sleeve_shares() -> None:
    output = _run(entries={D0: [_buy("sec-a", D0)]})

    fills = [e for e in output.events if isinstance(e, EntryFillEventV1)]
    assert [(e.fill_session, e.shares) for e in fills] == [(D1, 10)]
    # 1,000 slot / 50.00 a sleeve share: sell 20 at D1's open, buy 10 sec-a.
    assert _sleeve_trades(output)[:2] == [
        (D0, SignalSide.BUY, 200),
        (D1, SignalSide.SELL, 20),
    ]
    after = output.equity_curve[1]
    # 10 shares bought at the 100.00 open, marked at the 101.00 close.
    assert after.positions_value_base == Decimal("1010.00000000")
    assert after.total_equity_base == Decimal("10010.00000000")
    # Sleeve activity never appears in the Strategy's own Trade Log.
    assert not any(e.security_id == "spy" for e in output.events)


def test_sleeve_dividend_is_credited_then_swept_back_in() -> None:
    output = _run(sleeve_dividends={D2: 1.0})

    dividends = [
        e for e in output.cash_sleeve_events if isinstance(e, DividendAppliedEventV1)
    ]
    assert [(e.session, e.cash_credit_base) for e in dividends] == [
        (D2, Decimal("200.00000000"))
    ]
    # 200 of cash buys 4 more shares at D2's 50.00 open.
    assert (D2, SignalSide.BUY, 4) in _sleeve_trades(output)
    assert output.cash_sleeve_values[2].shares == Decimal(204)


def test_cash_never_goes_negative_through_sleeve_trades() -> None:
    output = _run(
        entries={D0: [_buy("sec-a", D0)]},
        sleeve_prices={D1: (53.0, 48.0)},
    )

    assert all(point.cash_base >= 0 for point in output.equity_curve)


def test_sleeve_that_is_also_a_traded_security_is_rejected() -> None:
    with pytest.raises(SimulationError, match="must not also be a pinned"):
        _run(sleeve_id="sec-a")
