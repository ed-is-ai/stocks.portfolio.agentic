"""Story 2.4 coverage: the full I/O matrix plus market-mechanics scenarios
for the deterministic Backtest simulation engine (AD-3/AD-6/AD-10/AD-12/
AD-13/AD-20) -- calendars, warm-up, cash reuse, duplicates, floor rounding,
insufficient cash/position conflict, beyond-end fills, corporate actions,
FX, final open marks, and determinism under reordered Strategy output."""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping

import pandas as pd
import pytest

import app.services.backtest.backtest_engine as backtest_engine
from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.backtest_engine import (
    DividendAppliedEventV1,
    EntryFillEventV1,
    ExitFillEventV1,
    OpenPositionMarkEventV1,
    SecurityMarketDataV1,
    SimulationError,
    SkipReasonCode,
    SkippedSignalEventV1,
    SplitAppliedEventV1,
    run_simulation,
)
from app.services.backtest.run_input_manifest import (
    ENGINE_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    DetectorSourceDigestV1,
    PinnedSecurityEvidenceV1,
    RunInputManifestV1,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)
from app.services.backtest.trading_calendar import TradingCalendar

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
CALENDAR = TradingCalendar()


# ---------------------------------------------------------------------------
# Evidence-building helpers
# ---------------------------------------------------------------------------


def _hex(value: float) -> str:
    return float(value).hex()


def _request_contract(start: date, end: date) -> dict[str, object]:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interval": "1d",
        "prepost": False,
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "timeout": 15,
        "raise_errors": True,
    }


def _row(
    session: date,
    *,
    open_: float,
    close_: float,
    dividend: float = 0.0,
    split: float = 0.0,
) -> dict[str, object]:
    high = max(open_, close_) + 1
    low = min(open_, close_) - 1
    return {
        "session": session.isoformat(),
        "open": _hex(open_),
        "high": _hex(high),
        "low": _hex(low),
        "close": _hex(close_),
        "adj_close": _hex(close_),
        "volume": _hex(1_000.0),
        "dividends": _hex(dividend),
        "stock_splits": _hex(split),
    }


def _month_str(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _sessions(mic: str, start: date, end_exclusive: date) -> tuple[date, ...]:
    return CALENDAR.sessions_in_range(mic, start, end_exclusive)


def _build_security(
    security_id: str,
    mic: str,
    sessions: tuple[date, ...],
    *,
    revision: str,
    currency: str = "USD",
    quote_unit: str = "USD",
    quote_unit_scale: str = "1",
    open_price: float = 100.0,
    close_price: float = 101.0,
    price_overrides: Mapping[date, tuple[float, float]] | None = None,
    dividend_by_session: Mapping[date, float] | None = None,
    split_by_session: Mapping[date, float] | None = None,
) -> tuple[SecurityMarketDataV1, PinnedSecurityEvidenceV1]:
    timezone = "America/New_York" if mic == "XNYS" else "Europe/London"
    overrides = price_overrides or {}
    dividends = dividend_by_session or {}
    splits = split_by_session or {}
    rows = tuple(
        _row(
            session,
            open_=overrides.get(session, (open_price, close_price))[0],
            close_=overrides.get(session, (open_price, close_price))[1],
            dividend=dividends.get(session, 0.0),
            split=splits.get(session, 0.0),
        )
        for session in sessions
    )
    actions = tuple(
        sorted(
            [
                {
                    "session": session.isoformat(),
                    "action_type": "dividend",
                    "value": _hex(value),
                }
                for session, value in dividends.items()
            ]
            + [
                {
                    "session": session.isoformat(),
                    "action_type": "split",
                    "value": _hex(value),
                }
                for session, value in splits.items()
            ],
            key=lambda item: (item["session"], item["action_type"]),
        )
    )
    start = sessions[0] - timedelta(days=1)
    end = sessions[-1] + timedelta(days=1)
    evidence = StoredHistoricalEvidence(
        data_revision=revision,
        security_id=security_id,
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version="YFinanceDailyProviderNativeV1",
        requested_symbol=security_id,
        observed_symbol=security_id,
        alias_revision=DIGEST_B,
        currency=currency,
        quote_unit=quote_unit,
        quote_unit_scale=quote_unit_scale,
        exchange_timezone=timezone,
        start=start.isoformat(),
        end=end.isoformat(),
        request_contract=_request_contract(start, end),
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json="{}",
        rows=rows,
        actions=actions,
    )
    market_data = SecurityMarketDataV1(security_id=security_id, price_evidence=evidence)
    pinned = PinnedSecurityEvidenceV1(
        security_id=security_id,
        price_revision=revision,
        action_revision=revision,
        fx_revision=None,
    )
    return market_data, pinned


def _fx_evidence(
    closes: tuple[tuple[date, float], ...], *, start: date, end: date
) -> StoredHistoricalEvidence:
    rows = tuple(_row(session, open_=close, close_=close) for session, close in closes)
    return StoredHistoricalEvidence(
        data_revision="f" * 64,
        security_id="fx-gbpusd",
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version="YFinanceDailyProviderNativeV1",
        requested_symbol="GBPUSD=X",
        observed_symbol="GBPUSD=X",
        alias_revision=None,
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
        exchange_timezone="UTC",
        start=start.isoformat(),
        end=end.isoformat(),
        request_contract=_request_contract(start, end),
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json="{}",
        rows=rows,
        actions=(),
    )


# ---------------------------------------------------------------------------
# Manifest / strategy / market-view helpers
# ---------------------------------------------------------------------------


def _detector_digests() -> tuple[DetectorSourceDigestV1, ...]:
    return (
        DetectorSourceDigestV1(
            detector_id="technical_indicators_v1", source_digest=DIGEST_A
        ),
        DetectorSourceDigestV1(
            detector_id="weinstein_stage_v1", source_digest=DIGEST_A
        ),
        DetectorSourceDigestV1(detector_id="vcp_v1", source_digest=DIGEST_A),
    )


def _manifest(
    *,
    securities: tuple[PinnedSecurityEvidenceV1, ...],
    start_month: str,
    end_month: str,
    base_currency: Literal["GBP", "USD"] = "USD",
    starting_capital: Decimal = Decimal("100000"),
) -> RunInputManifestV1:
    return RunInputManifestV1(
        schema_version="run_input_manifest.v1",
        engine_version=ENGINE_VERSION,
        protocol_schema_version=PROTOCOL_SCHEMA_VERSION,
        market_view_source_digest=DIGEST_A,
        ledger_action_metrics_digest=DIGEST_A,
        numeric_rounding_policy="HistoricalMarketPlanesV1",
        runtime_lock_digest=DIGEST_A,
        calendar_session_table_digest=DIGEST_A,
        python_runtime="3.13",
        timezone_dataset_version="2026.2",
        strategy_id="test-strategy",
        strategy_api_version=1,
        strategy_source_digest=DIGEST_A,
        detector_source_digests=_detector_digests(),
        parameters={},
        alias_revision=DIGEST_A,
        securities=securities,
        profile_hash=DIGEST_A,
        start_month=start_month,
        end_month=end_month,
        ordered_month_digest=DIGEST_A,
        base_currency=base_currency,
        starting_capital=starting_capital,
    )


@dataclass(frozen=True)
class _FakeMarketView:
    """Minimal ``MarketViewV1``-conforming stub -- the fixture Strategies
    below never read price history, so this simply carries the session."""

    as_of_session: date

    def price_history(self, security_id: str) -> pd.DataFrame:
        del security_id
        return pd.DataFrame()

    def scan_result(self, security_id: str):
        del security_id
        return None


def _market_view_factory():
    return lambda session: _FakeMarketView(as_of_session=session)


class _ScriptedStrategy:
    """A ``StrategyProtocolV1`` implementation whose entries/exits are
    fully scripted per session, and whose position size is scripted per
    ``rule_id`` -- gives tests exact control over signal timing without
    depending on indicator/price logic."""

    def __init__(
        self,
        *,
        entries: Mapping[date, list[Signal]] | None = None,
        exits: Mapping[date, list[Signal]] | None = None,
        size_by_rule: Mapping[str, int] | None = None,
        default_size: int = 1,
    ) -> None:
        self._entries = entries or {}
        self._exits = exits or {}
        self._size_by_rule = size_by_rule or {}
        self._default_size = default_size

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        del parameters
        return list(self._entries.get(view.as_of_session, []))

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        del portfolio, parameters
        return list(self._exits.get(view.as_of_session, []))

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        del view, portfolio, parameters
        return self._size_by_rule.get(signal.rule_id, self._default_size)


# ---------------------------------------------------------------------------
# 1. Valid BUY signal fills at next session open, integer floor shares
# ---------------------------------------------------------------------------


def test_valid_buy_signal_fills_at_next_session_open_with_integer_floor_shares() -> (
    None
):
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=105.0,
    )
    d0 = sessions[0]
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="r1"
                )
            ]
        },
        size_by_rule={"r1": 10},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(fills) == 1
    fill = fills[0]
    assert fill.fill_session == sessions[1]
    assert fill.shares == 10
    assert fill.fill_price_native == Decimal("100")
    assert fill.cost_base == Decimal("1000.00000000")
    assert fill.fx_rate is None
    assert output.final_cash_base == Decimal("100000.00000000") - Decimal(
        "1000.00000000"
    )
    assert output.manifest_digest == manifest.digest()


# ---------------------------------------------------------------------------
# 2. Duplicate signal
# ---------------------------------------------------------------------------


def test_duplicate_signal_is_skipped_once_and_earlier_valid_signal_still_commits() -> (
    None
):
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    d0 = sessions[0]
    duplicate_signal = Signal(
        security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="r1"
    )
    strategy = _ScriptedStrategy(
        entries={d0: [duplicate_signal, duplicate_signal]}, size_by_rule={"r1": 5}
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    duplicate_skips = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.DUPLICATE_SIGNAL
    ]
    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(duplicate_skips) == 1
    assert len(fills) == 1
    assert fills[0].shares == 5


# ---------------------------------------------------------------------------
# 3. Contradictory BUY+SELL same security/session -- SELL processed first
# ---------------------------------------------------------------------------


def test_contradictory_buy_and_sell_same_session_processes_sell_first() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    d0 = sessions[0]
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        exits={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.SELL, session=d0, rule_id="rs"
                )
            ]
        },
        size_by_rule={"rb": 3, "rs": 3},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    position_conflicts = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.POSITION_CONFLICT
        and event.side is SignalSide.SELL
    ]
    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(position_conflicts) == 1  # nothing was held yet, so SELL is rejected
    assert len(fills) == 1  # BUY still schedules and fills normally
    assert fills[0].rule_id == "rb"


# ---------------------------------------------------------------------------
# 4. Insufficient cash at fill time
# ---------------------------------------------------------------------------


def test_insufficient_cash_at_fill_time_skips_and_continues() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security(
        "sec-a", "XNYS", sessions, revision=DIGEST_A, open_price=100.0
    )
    d0 = sessions[0]
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="r1"
                )
            ]
        },
        size_by_rule={"r1": 10},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        starting_capital=Decimal("50"),
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    skips = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.INSUFFICIENT_CASH
    ]
    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(skips) == 1
    assert not fills
    assert output.final_cash_base == Decimal("50.00000000")


# ---------------------------------------------------------------------------
# 5. SELL exceeding held quantity
# ---------------------------------------------------------------------------


def test_sell_exceeding_held_quantity_is_rejected_and_cash_never_negative() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=100.0,
    )
    d0, d2 = sessions[0], sessions[2]
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        exits={
            d2: [
                Signal(
                    security_id="sec-a", side=SignalSide.SELL, session=d2, rule_id="rs"
                )
            ]
        },
        size_by_rule={"rb": 5, "rs": 10},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    oversell_skips = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.POSITION_CONFLICT
        and event.side is SignalSide.SELL
    ]
    assert len(oversell_skips) == 1
    assert not [event for event in output.events if isinstance(event, ExitFillEventV1)]
    assert output.final_cash_base >= 0
    # The original 5-share position survives, untouched, to the final mark.
    marks = output.final_open_positions
    assert len(marks) == 1 and marks[0].shares == 5


# ---------------------------------------------------------------------------
# 6. Split effective on D applied exactly once, before fills
# ---------------------------------------------------------------------------


def test_split_effective_on_d_applied_exactly_once_before_signals_fills() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0, d2 = sessions[0], sessions[2]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=100.0,
        split_by_session={d2: 2.0},
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 4},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    split_events = [
        event for event in output.events if isinstance(event, SplitAppliedEventV1)
    ]
    assert len(split_events) == 1
    split_event = split_events[0]
    assert split_event.session == d2
    assert split_event.shares_before == Decimal("4")
    assert split_event.shares_after == Decimal("8")
    assert split_event.ratio == Decimal("2")
    marks = output.final_open_positions
    assert len(marks) == 1 and marks[0].shares == 8


def test_full_exit_sells_current_post_split_shares_not_stale_scheduled_quantity() -> (
    None
):
    """A split effective exactly on a pending SELL's fill session must not
    strand the exit: the fill must sell whatever is actually held at fill
    time (post-split), not the (now stale) quantity snapshotted when the
    signal was scheduled a session earlier."""
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0, d1, d2 = sessions[0], sessions[1], sessions[2]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=100.0,
        split_by_session={d2: 2.0},
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        exits={
            d1: [
                Signal(
                    security_id="sec-a", side=SignalSide.SELL, session=d1, rule_id="re"
                )
            ]
        },
        size_by_rule={"rb": 4, "re": 4},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    skipped_conflicts = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.POSITION_CONFLICT
    ]
    assert not skipped_conflicts
    exits = [event for event in output.events if isinstance(event, ExitFillEventV1)]
    assert len(exits) == 1
    assert exits[0].shares == 8
    assert exits[0].fill_session == d2
    assert output.final_open_positions == ()


def test_split_requiring_fractional_shares_is_fatal_unsupported_corporate_action() -> (
    None
):
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0, d2 = sessions[0], sessions[2]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=100.0,
        split_by_session={d2: 0.25},
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 1},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
        )

    assert exc_info.value.code == "unsupported_corporate_action"
    assert exc_info.value.session == d2


# ---------------------------------------------------------------------------
# 7. Dividend effective on D
# ---------------------------------------------------------------------------


def test_dividend_effective_on_d_credits_cash_with_policy_evidence_recorded() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0, d2 = sessions[0], sessions[2]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=100.0,
        dividend_by_session={d2: 0.5},
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 4},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    dividends = [
        event for event in output.events if isinstance(event, DividendAppliedEventV1)
    ]
    assert len(dividends) == 1
    dividend = dividends[0]
    assert dividend.session == d2
    assert dividend.per_share_amount == Decimal("0.5")
    assert dividend.shares_carried == Decimal("4")
    assert dividend.cash_credit_native == Decimal("2.00000000")
    assert dividend.cash_credit_base == Decimal("2.00000000")
    assert dividend.evidence_revision == DIGEST_A
    # 100000 starting - 400 buy cost + 2 dividend credit.
    assert output.final_cash_base == Decimal("100000") - Decimal("400") + Decimal("2")


# ---------------------------------------------------------------------------
# 8-10. Missing / stale / ambiguous FX aborts fatal
# ---------------------------------------------------------------------------


def test_missing_fx_aborts_fatal_with_session_and_month_context() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a", "XNYS", sessions, revision=DIGEST_A, currency="GBP", quote_unit="GBP"
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 1},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        base_currency="USD",
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
            fx_evidence=None,
        )

    assert exc_info.value.code == "fx_missing"
    assert exc_info.value.session == sessions[1]
    assert exc_info.value.month == _month_str(sessions[1])


def test_stale_fx_aborts_fatal() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a", "XNYS", sessions, revision=DIGEST_A, currency="GBP", quote_unit="GBP"
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 1},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        base_currency="USD",
    )
    stale_close_date = sessions[1] - timedelta(days=30)
    fx_evidence = _fx_evidence(
        ((stale_close_date, 1.25),),
        start=stale_close_date - timedelta(days=1),
        end=sessions[1] + timedelta(days=1),
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
            fx_evidence=fx_evidence,
        )

    assert exc_info.value.code == "fx_stale"


def test_ambiguous_fx_aborts_fatal() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a", "XNYS", sessions, revision=DIGEST_A, currency="GBP", quote_unit="GBP"
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 1},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        base_currency="USD",
    )
    fx_start = sessions[1] - timedelta(days=5)
    fx_end = sessions[1] + timedelta(days=1)
    fx_evidence = _fx_evidence(
        ((sessions[1], 1.25), (sessions[1], 1.30)), start=fx_start, end=fx_end
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
            fx_evidence=fx_evidence,
        )

    assert exc_info.value.code == "fx_ambiguous"


# ---------------------------------------------------------------------------
# 11. Fill beyond normalized end
# ---------------------------------------------------------------------------


def test_fill_beyond_normalized_end_records_skip_and_never_executes() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    last_session = sessions[-1]
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    strategy = _ScriptedStrategy(
        entries={
            last_session: [
                Signal(
                    security_id="sec-a",
                    side=SignalSide.BUY,
                    session=last_session,
                    rule_id="rb",
                )
            ]
        },
        size_by_rule={"rb": 1},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    beyond_end = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.FILL_BEYOND_END
    ]
    assert len(beyond_end) == 1
    assert not [event for event in output.events if isinstance(event, EntryFillEventV1)]


# ---------------------------------------------------------------------------
# 12. Final session open position marked -- never a fabricated exit/win
# ---------------------------------------------------------------------------


def test_final_session_open_position_marked_and_never_fabricated_as_a_win() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=110.0,
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 2},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    assert not [event for event in output.events if isinstance(event, ExitFillEventV1)]
    marks = [
        event for event in output.events if isinstance(event, OpenPositionMarkEventV1)
    ]
    assert len(marks) == 1
    mark = marks[0]
    assert mark.session == sessions[-1]
    assert mark.shares == 2
    assert mark.mark_price_native == Decimal("110")
    assert mark.market_value_base == Decimal("220.00000000")
    assert output.final_open_positions == (mark,)
    assert output.equity_curve[-1].positions_value_base == mark.market_value_base
    assert output.equity_curve[-1].total_equity_base == (
        output.final_cash_base + mark.market_value_base
    )


# ---------------------------------------------------------------------------
# 13. Warm-up window
# ---------------------------------------------------------------------------


def test_warm_up_window_produces_no_rows_before_normalized_start() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    wide_start = date(2023, 1, 1)
    sessions = _sessions("XNYS", wide_start, end_exclusive)
    normalized_sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    strategy = _ScriptedStrategy()  # no signals at all
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    curve_sessions = [point.session for point in output.equity_curve]
    assert curve_sessions == list(normalized_sessions)
    assert curve_sessions[0] == normalized_sessions[0]
    assert all(session >= start for session in curve_sessions)


# ---------------------------------------------------------------------------
# 14. Determinism under reordered Strategy output
# ---------------------------------------------------------------------------


def test_determinism_under_reordered_strategy_signal_output() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    signal_a = Signal(
        security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="ra"
    )
    signal_b = Signal(
        security_id="sec-b", side=SignalSide.BUY, session=d0, rule_id="rb"
    )

    def _run(order: list[Signal]) -> tuple:
        market_a, pinned_a = _build_security(
            "sec-a", "XNYS", sessions, revision=DIGEST_A
        )
        market_b, pinned_b = _build_security(
            "sec-b", "XNYS", sessions, revision=DIGEST_B
        )
        strategy = _ScriptedStrategy(
            entries={d0: order}, size_by_rule={"ra": 3, "rb": 4}
        )
        manifest = _manifest(
            securities=(pinned_a, pinned_b),
            start_month=_month_str(start),
            end_month=_month_str(start),
        )
        output = run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(market_a, market_b),
        )
        return output.events, output.equity_curve

    forward_events, forward_curve = _run([signal_a, signal_b])
    reversed_events, reversed_curve = _run([signal_b, signal_a])

    assert forward_events == reversed_events
    assert forward_curve == reversed_curve


# ---------------------------------------------------------------------------
# 15. US/UK calendar holiday divergence -- union calendar timeline
# ---------------------------------------------------------------------------


def test_union_calendar_covers_us_and_uk_holiday_divergence() -> None:
    start, end_exclusive = date(2024, 12, 1), date(2025, 1, 1)
    xnys_sessions = _sessions("XNYS", start, end_exclusive)
    xlon_sessions = _sessions("XLON", start, end_exclusive)
    # Boxing Day (26 Dec) is a UK-only holiday -- proves the two calendars
    # genuinely diverge for this window before asserting the union.
    assert set(xlon_sessions) != set(xnys_sessions)

    market_a, pinned_a = _build_security(
        "sec-us", "XNYS", xnys_sessions, revision=DIGEST_A
    )
    market_b, pinned_b = _build_security(
        "sec-uk", "XLON", xlon_sessions, revision=DIGEST_B
    )
    strategy = _ScriptedStrategy()
    manifest = _manifest(
        securities=(pinned_a, pinned_b),
        start_month=_month_str(start),
        end_month=_month_str(start),
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_a, market_b),
    )

    curve_sessions = {point.session for point in output.equity_curve}
    assert curve_sessions == set(xnys_sessions) | set(xlon_sessions)


# ---------------------------------------------------------------------------
# 16. SELL-before-BUY enables same-session cash reuse
# ---------------------------------------------------------------------------


def test_sell_before_buy_enables_same_session_cash_reuse() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0, d_signal = sessions[0], sessions[2]
    market_a, pinned_a = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        open_price=100.0,
        close_price=100.0,
    )
    market_b, pinned_b = _build_security(
        "sec-b", "XNYS", sessions, revision=DIGEST_B, open_price=90.0, close_price=90.0
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb0"
                )
            ],
            d_signal: [
                Signal(
                    security_id="sec-b",
                    side=SignalSide.BUY,
                    session=d_signal,
                    rule_id="rb1",
                )
            ],
        },
        exits={
            d_signal: [
                Signal(
                    security_id="sec-a",
                    side=SignalSide.SELL,
                    session=d_signal,
                    rule_id="rs",
                )
            ]
        },
        size_by_rule={"rb0": 10, "rb1": 10, "rs": 10},
    )
    # Exactly enough capital for the first buy (10 * 100 = 1000); nothing
    # left over for the second buy (10 * 90 = 900) unless the same-session
    # SELL's proceeds are credited first.
    manifest = _manifest(
        securities=(pinned_a, pinned_b),
        start_month=_month_str(start),
        end_month=_month_str(start),
        starting_capital=Decimal("1000"),
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_a, market_b),
    )

    exit_fills = [
        event for event in output.events if isinstance(event, ExitFillEventV1)
    ]
    entry_fills_b = [
        event
        for event in output.events
        if isinstance(event, EntryFillEventV1) and event.security_id == "sec-b"
    ]
    insufficient_cash = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.INSUFFICIENT_CASH
    ]
    assert len(exit_fills) == 1
    assert len(entry_fills_b) == 1  # succeeded only because SELL ran first
    assert not insufficient_cash


# ---------------------------------------------------------------------------
# 17. GBp (pence) quote-unit scaling
# ---------------------------------------------------------------------------


def test_gbp_pence_quote_unit_scales_before_valuation() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        currency="GBP",
        quote_unit="GBp",
        quote_unit_scale="0.01",
        open_price=1000.0,
        close_price=1000.0,
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 5},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        base_currency="GBP",
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(fills) == 1
    # 1000 pence/share * 5 shares = 5000 pence = GBP 50.00, no FX needed.
    assert fills[0].cost_base == Decimal("50.00000000")
    assert fills[0].fx_rate is None


# ---------------------------------------------------------------------------
# 18-19. FX directions
# ---------------------------------------------------------------------------


def test_fx_direction_gbp_security_into_usd_base_multiplies_by_rate() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        currency="GBP",
        quote_unit="GBP",
        open_price=100.0,
        close_price=100.0,
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 2},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        base_currency="USD",
    )
    fx_evidence = _fx_evidence(
        tuple((session, 1.25) for session in sessions),
        start=sessions[0] - timedelta(days=1),
        end=sessions[-1] + timedelta(days=1),
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
        fx_evidence=fx_evidence,
    )

    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(fills) == 1
    # 100 GBP/share * 2 shares = 200 GBP * 1.25 = 250 USD.
    assert fills[0].cost_base == Decimal("250.00000000")
    assert fills[0].fx_rate == Decimal("1.25")


def test_fx_direction_usd_security_into_gbp_base_divides_by_rate() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security(
        "sec-a",
        "XNYS",
        sessions,
        revision=DIGEST_A,
        currency="USD",
        quote_unit="USD",
        open_price=125.0,
        close_price=125.0,
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 2},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
        base_currency="GBP",
    )
    fx_evidence = _fx_evidence(
        tuple((session, 1.25) for session in sessions),
        start=sessions[0] - timedelta(days=1),
        end=sessions[-1] + timedelta(days=1),
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
        fx_evidence=fx_evidence,
    )

    fills = [event for event in output.events if isinstance(event, EntryFillEventV1)]
    assert len(fills) == 1
    # 125 USD/share * 2 shares = 250 USD / 1.25 = 200 GBP.
    assert fills[0].cost_base == Decimal("200.00000000")
    assert fills[0].fx_rate == Decimal("1.25")


# ---------------------------------------------------------------------------
# 20. Unsupported corporate action fails fatal
# ---------------------------------------------------------------------------


def test_unsupported_corporate_action_fails_fatal() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    tampered_evidence = replace(
        market_data.price_evidence,
        actions=(
            {
                "session": sessions[2].isoformat(),
                "action_type": "merger",
                "value": _hex(1.0),
            },
        ),
    )
    tampered_market_data = SecurityMarketDataV1(
        security_id="sec-a", price_evidence=tampered_evidence
    )
    strategy = _ScriptedStrategy()
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(tampered_market_data,),
        )

    assert exc_info.value.code == "unsupported_corporate_action"


# ---------------------------------------------------------------------------
# 21. position_size_zero
# ---------------------------------------------------------------------------


def test_position_size_zero_is_skipped() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 0},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    zero_skips = [
        event
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
        and event.reason is SkipReasonCode.POSITION_SIZE_ZERO
    ]
    assert len(zero_skips) == 1
    assert not [event for event in output.events if isinstance(event, EntryFillEventV1)]


# ---------------------------------------------------------------------------
# 22. Signal session mismatch and ineligible security
# ---------------------------------------------------------------------------


def test_signal_session_mismatch_and_ineligible_security_are_skipped() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    d0 = sessions[0]
    market_data, pinned = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    mismatched_session_signal = Signal(
        security_id="sec-a", side=SignalSide.BUY, session=sessions[1], rule_id="rm"
    )
    ineligible_security_signal = Signal(
        security_id="sec-unknown", side=SignalSide.BUY, session=d0, rule_id="ri"
    )
    strategy = _ScriptedStrategy(
        entries={d0: [mismatched_session_signal, ineligible_security_signal]},
        size_by_rule={"rm": 1, "ri": 1},
    )
    manifest = _manifest(
        securities=(pinned,), start_month=_month_str(start), end_month=_month_str(start)
    )

    output = run_simulation(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(market_data,),
    )

    reasons = {
        event.reason
        for event in output.events
        if isinstance(event, SkippedSignalEventV1)
    }
    assert SkipReasonCode.SIGNAL_SESSION_MISMATCH in reasons
    assert SkipReasonCode.INELIGIBLE_SECURITY in reasons


# ---------------------------------------------------------------------------
# 23. The engine never imports a live-portfolio/repository/agent module
# ---------------------------------------------------------------------------


def test_engine_never_imports_forbidden_live_portfolio_modules() -> None:
    source = Path(backtest_engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)

    forbidden_modules = {
        "app.agents",
        "app.repositories.backtest_repo",
        "app.repositories.trades_repo",
        "app.repositories.cash_balances_repo",
        "app.repositories.position_state_repo",
    }
    for module in imported_modules:
        assert module not in forbidden_modules
        assert not module.startswith("app.agents.")
    forbidden_names = {
        "TraderAgent",
        "BacktestRepository",
        "HistoricalPriceRepository",
        "TradesRepository",
        "CashBalancesRepository",
        "PositionStateRepository",
    }
    assert not (imported_names & forbidden_names)


# ---------------------------------------------------------------------------
# 24. Tampered/mismatched pinned evidence aborts fatal
# ---------------------------------------------------------------------------


def test_pinned_evidence_revision_mismatch_aborts_fatal() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, _ = _build_security("sec-a", "XNYS", sessions, revision=DIGEST_A)
    mismatched_pin = PinnedSecurityEvidenceV1(
        security_id="sec-a",
        price_revision=DIGEST_D,
        action_revision=DIGEST_D,
        fx_revision=None,
    )
    manifest = _manifest(
        securities=(mismatched_pin,),
        start_month=_month_str(start),
        end_month=_month_str(start),
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=_ScriptedStrategy(),
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
        )

    assert exc_info.value.code == "missing_pinned_evidence"


# ---------------------------------------------------------------------------
# 25. A missing required open on the scheduled fill session is fatal
# ---------------------------------------------------------------------------


def test_missing_required_open_on_fill_session_aborts_fatal() -> None:
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    full_sessions = _sessions("XNYS", start, end_exclusive)
    d0 = full_sessions[0]
    gap_day = full_sessions[1]
    # Evidence has no row for ``gap_day`` even though it is a valid XNYS
    # trading session -- distinct from a legitimate holiday/weekend gap,
    # this models tampered/incomplete provider-native evidence.
    sessions_with_gap = full_sessions[:1] + full_sessions[2:]
    market_data, pinned = _build_security(
        "sec-a", "XNYS", sessions_with_gap, revision=DIGEST_A
    )
    strategy = _ScriptedStrategy(
        entries={
            d0: [
                Signal(
                    security_id="sec-a", side=SignalSide.BUY, session=d0, rule_id="rb"
                )
            ]
        },
        size_by_rule={"rb": 1},
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(start),
        end_month=_month_str(start),
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=strategy,
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
        )

    assert exc_info.value.code == "missing_required_open"
    assert exc_info.value.session == gap_day
    assert exc_info.value.month == _month_str(gap_day)


def test_fx_evidence_revision_mismatch_against_pinned_fx_revision_aborts_fatal() -> (
    None
):
    start, end_exclusive = date(2024, 3, 1), date(2024, 4, 1)
    sessions = _sessions("XNYS", start, end_exclusive)
    market_data, price_pinned = _build_security(
        "sec-a", "XNYS", sessions, revision=DIGEST_A, currency="GBP", quote_unit="GBP"
    )
    pinned = price_pinned.model_copy(update={"fx_revision": DIGEST_D})
    fx_evidence = _fx_evidence(
        ((sessions[0], 1.25),), start=sessions[0] - timedelta(days=1), end=sessions[-1]
    )
    manifest = _manifest(
        securities=(pinned,),
        start_month=_month_str(sessions[0]),
        end_month=_month_str(sessions[0]),
        base_currency="USD",
    )

    with pytest.raises(SimulationError) as exc_info:
        run_simulation(
            manifest=manifest,
            strategy=_ScriptedStrategy(),
            market_view_factory=_market_view_factory(),
            security_market_data=(market_data,),
            fx_evidence=fx_evidence,
        )

    assert exc_info.value.code == "missing_pinned_evidence"
