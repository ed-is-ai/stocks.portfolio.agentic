"""#82 (A): held positions settle in cash when their company exits.

One test per I/O matrix row of ``spec-gh-82a-exit-settlement.md``, using the
in-memory fixtures of ``test_backtest_engine.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.backtest_engine import (
    SettlingExitType,
    SimulationError,
    SkipReasonCode,
    SkippedSignalEventV1,
    TerminalExitV1,
    TerminalSettlementEventV1,
    run_simulation,
)
from app.services.backtest.metrics import calculate_metrics
from app.services.backtest.result_presenter import trade_log_view
from app.services.backtest.strategy_protocol import Signal, SignalSide
from tests.backtest.test_backtest_engine import (
    DIGEST_A,
    _ScriptedStrategy,
    _build_security,
    _fx_evidence,
    _manifest,
    _market_view_factory,
    _sessions,
)

START, END = date(2008, 5, 1), date(2008, 7, 1)
SESSIONS = _sessions("XNYS", START, END)
D0 = SESSIONS[0]


def _buy(session: date = D0) -> Signal:
    return Signal(
        security_id="sec-a", side=SignalSide.BUY, session=session, rule_id="b"
    )


def _exit(
    exit_session: date,
    exit_type: SettlingExitType = "acquisition",
    price: str | None = None,
) -> TerminalExitV1:
    return TerminalExitV1(
        security_id="sec-a",
        exit_session=exit_session,
        exit_type=exit_type,
        terminal_price_native=None if price is None else Decimal(price),
        source_digest=DIGEST_A,
    )


def _run(
    strategy: _ScriptedStrategy,
    exits: tuple[TerminalExitV1, ...],
    *,
    price_overrides: dict[date, tuple[float, float]] | None = None,
    dividend_by_session: dict[date, float] | None = None,
):
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        SESSIONS,
        revision=DIGEST_A,
        price_overrides=price_overrides,
        dividend_by_session=dividend_by_session,
    )
    return run_simulation(
        manifest=_manifest(
            securities=(pinned,),
            start_month="2008-05",
            end_month="2008-06",
            starting_capital=Decimal("10000"),
        ),
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
        terminal_exits=exits,
    )


def _settlements(output) -> list[TerminalSettlementEventV1]:
    return [e for e in output.events if isinstance(e, TerminalSettlementEventV1)]


def test_cash_acquisition_settles_converted_and_removes_position() -> None:
    exit_day = date(2008, 6, 2)
    market_data, pinned = _build_security(
        "sec-a", "XNYS", SESSIONS, revision=DIGEST_A, open_price=125.0
    )
    output = run_simulation(
        manifest=_manifest(
            securities=(pinned,),
            start_month="2008-05",
            end_month="2008-06",
            base_currency="GBP",
            starting_capital=Decimal("10000"),
        ),
        strategy=_ScriptedStrategy(entries={D0: [_buy()]}),
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
        fx_evidence=_fx_evidence(
            tuple((session, 1.25) for session in SESSIONS),
            start=D0 - timedelta(days=1),
            end=SESSIONS[-1] + timedelta(days=1),
        ),
        terminal_exits=(_exit(exit_day, price="45.50"),),
    )

    (event,) = _settlements(output)
    assert event.session == exit_day
    assert event.shares == Decimal(100)
    assert event.price_basis == "terminal_price"
    assert event.settlement_price_native == Decimal("45.50")
    assert event.proceeds_base == Decimal("3640.00000000")  # 4,550 USD / 1.25
    assert event.fx_rate == Decimal("1.25")
    assert output.final_open_positions == ()
    assert output.final_cash_base == Decimal("3640.00000000")
    point = next(p for p in output.equity_curve if p.session == exit_day)
    assert point.positions_value_base == Decimal(0)
    assert point.cash_base == Decimal("3640.00000000")


def test_bankruptcy_without_price_settles_at_last_close_on_or_before_exit() -> None:
    saturday = date(2008, 6, 7)
    # Fixture rows keep low = min - 1, so prices stay above 1.
    output = _run(
        _ScriptedStrategy(entries={D0: [_buy()]}),
        (_exit(saturday, "bankruptcy"),),
        price_overrides={date(2008, 6, 6): (2.50, 2.21), date(2008, 6, 9): (9, 9)},
    )

    (event,) = _settlements(output)
    assert event.price_basis == "last_close"
    assert event.settlement_price_native == Decimal("2.21")
    assert event.session == date(2008, 6, 9)


def test_exit_on_a_non_session_settles_on_the_next_session() -> None:
    saturday = date(2008, 6, 7)
    output = _run(_ScriptedStrategy(entries={D0: [_buy()]}), (_exit(saturday),))

    (event,) = _settlements(output)
    assert event.session == date(2008, 6, 9)
    assert event.exit_session == saturday


def test_exit_for_a_security_not_held_changes_nothing() -> None:
    strategy = _ScriptedStrategy()
    with_exit = _run(strategy, (_exit(date(2008, 6, 2)),))
    without = _run(strategy, ())

    assert with_exit == without
    assert _settlements(with_exit) == []


def test_buy_after_exit_is_skipped_as_security_exited() -> None:
    later = next(session for session in SESSIONS if session > date(2008, 6, 2))
    output = _run(
        _ScriptedStrategy(entries={later: [_buy(later)]}), (_exit(date(2008, 6, 2)),)
    )

    (skip,) = [e for e in output.events if isinstance(e, SkippedSignalEventV1)]
    assert skip.reason is SkipReasonCode.SECURITY_EXITED
    assert output.final_open_positions == ()


def test_pending_sell_on_exit_session_loses_to_settlement_after_actions() -> None:
    exit_day = date(2008, 6, 2)
    signal_day = SESSIONS[SESSIONS.index(exit_day) - 1]
    sell = Signal(
        security_id="sec-a", side=SignalSide.SELL, session=signal_day, rule_id="s"
    )
    output = _run(
        _ScriptedStrategy(
            entries={D0: [_buy()]}, exits={signal_day: [sell]}, size_by_rule={"s": -1}
        ),
        (_exit(exit_day, price="45.50"),),
        dividend_by_session={exit_day: 0.5},
    )

    kinds = [e.kind for e in output.events]
    start = kinds.index("dividend_applied")
    assert kinds[start : start + 3] == [
        "dividend_applied",
        "terminal_settlement",
        "skipped_signal",
    ]
    skip = output.events[start + 2]
    assert isinstance(skip, SkippedSignalEventV1)
    assert skip.reason is SkipReasonCode.SECURITY_EXITED
    assert skip.side is SignalSide.SELL
    assert not any(e.kind == "exit_fill" for e in output.events)


@pytest.mark.parametrize("exit_type", ["still_trading", "rename", "unknown"])
def test_non_exit_types_are_rejected_at_construction(exit_type: str) -> None:
    raw = _exit(date(2008, 6, 2)).model_dump() | {"exit_type": exit_type}
    with pytest.raises(ValueError, match="Input should be 'acquisition'"):
        TerminalExitV1.model_validate(raw)


def test_repeated_security_exit_is_rejected() -> None:
    exit_ = _exit(date(2008, 6, 2))
    with pytest.raises(SimulationError, match="more than one exit"):
        _run(_ScriptedStrategy(), (exit_, exit_))


def test_exit_for_an_unpinned_security_is_rejected() -> None:
    unpinned = _exit(date(2008, 6, 2)).model_copy(update={"security_id": "sec-z"})
    with pytest.raises(SimulationError, match="not pinned"):
        _run(_ScriptedStrategy(), (unpinned,))


def test_dividend_after_a_weekend_exit_is_not_applied() -> None:
    saturday, monday = date(2008, 6, 7), date(2008, 6, 9)
    output = _run(
        _ScriptedStrategy(entries={D0: [_buy()]}),
        (_exit(saturday, price="45.50"),),
        dividend_by_session={monday: 0.5},
    )
    kinds = [e.kind for e in output.events]
    assert "terminal_settlement" in kinds
    assert "dividend_applied" not in kinds


def test_settlement_round_trips_through_ledger_parser_and_presenter() -> None:
    output = _run(
        _ScriptedStrategy(entries={D0: [_buy()]}),
        (_exit(date(2008, 6, 2), price="45.50"),),
    )
    (event,) = _settlements(output)

    parsed = BacktestRepository._parse_trade_log_event(event.model_dump(mode="json"))
    assert parsed == event
    result = SimpleNamespace(events=output.events, base_currency="USD")
    view = trade_log_view(result, {})  # type: ignore[arg-type]
    row = next(r for r in view.rows if r.kind == "terminal_settlement")
    assert row.kind_label == "Exit settled"
    assert row.detail.startswith("Exit settled at 45.50 USD (terminal price)")


def test_schema_upgrade_admits_terminal_settlement_rows(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    BacktestRepository(db.make_connect(lambda: path)).ensure_schema()
    with sqlite3.connect(path) as conn:
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trade_log'"
        ).fetchone()
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='trade_log'",
            (sql.replace(", 'terminal_settlement'", ""),),
        )
    legacy_row = ("legacy", "r", 99, "exit_fill", "s", '{"kept": true}')
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO trade_log VALUES (?, ?, ?, ?, ?, ?)", legacy_row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trade_log VALUES ('x', 'r', 1, 'terminal_settlement', "
                "'s', '{}')"
            )

    BacktestRepository(db.make_connect(lambda: path)).ensure_schema()

    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO trade_log VALUES ('x', 'r', 1, 'terminal_settlement', "
            "'s', '{}')"
        )
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name='trade_log'"
            )
        }
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM trade_log")
        kept = conn.execute("SELECT * FROM trade_log WHERE sequence = 99").fetchall()
    assert kept == [legacy_row]  # rows written before the upgrade survive
    assert {
        "idx_trade_log_run_sequence",
        "trade_log_immutable_update",
        "trade_log_immutable_delete",
    } <= names


def test_settlement_counts_as_a_closed_trade_in_win_rate() -> None:
    output = _run(
        _ScriptedStrategy(entries={D0: [_buy()]}),
        (_exit(date(2008, 6, 7), "bankruptcy"),),
        price_overrides={date(2008, 6, 6): (2.50, 2.21), date(2008, 6, 9): (9, 9)},
    )
    (settlement,) = _settlements(output)
    assert settlement.realized_pnl_base < 0
    metrics = calculate_metrics(
        starting_capital=Decimal("10000"),
        equity_curve=output.equity_curve,
        closed_trades=(settlement,),
    )
    assert metrics.win_rate == 0.0
