"""Target-weight sizing and rebalancing (#118): one test per I/O matrix row,
determinism, stored-result read-back and worker position reconstruction."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.backtest_engine import (
    EntryFillEventV1,
    EquityCurvePointV1,
    ExitFillEventV1,
    InMemorySessionBatchSink,
    SimulationError,
    SimulationOutputV1,
    SkipReasonCode,
    SkippedSignalEventV1,
    SplitAppliedEventV1,
    TradeLogEvent,
    run_simulation,
)
from app.services.backtest.strategy_protocol import (
    Signal,
    SignalSide,
    TargetWeightSizingV1,
)
from app.services.backtest.worker import _ClaimState, _StagingSink
from tests.backtest.test_backtest_engine import (
    _ScriptedStrategy,
    _build_security,
    _manifest,
    _market_view_factory,
    _month_str,
    _sessions,
)
from tests.backtest.test_backtest_repo_results import (
    CLAIM_TOKEN,
    RUN_ID,
    _entry,
    _exit,
    _repo,
    _seed_backtest_run,
    _write_staging,
)

START = date(2024, 3, 1)
SESSIONS = _sessions("XNYS", START, date(2024, 4, 1))
D0, D2, D3 = SESSIONS[0], SESSIONS[2], SESSIONS[3]


class _TargetStrategy(_ScriptedStrategy):
    """A scripted Strategy that opts in to target-weight sizing."""

    target_weight_sizing: Literal[True] = True


def _target(security_id: str, session: date, weight: str) -> Signal:
    return Signal(
        security_id=security_id,
        side=SignalSide.BUY,
        session=session,
        rule_id=f"target-{security_id}",
        target_weight=Decimal(weight),
    )


def _from(day: date, open_: float, close: float) -> dict[date, tuple[float, float]]:
    """Price override from ``day`` onwards."""
    return {session: (open_, close) for session in SESSIONS if session >= day}


def _run(
    targets: dict[date, list[Signal]],
    prices: dict[str, tuple[float, dict[date, tuple[float, float]]]],
    *,
    strategy_type: type[_ScriptedStrategy] = _TargetStrategy,
    parameters: dict[str, object] | None = None,
) -> tuple[SimulationOutputV1, InMemorySessionBatchSink]:
    built = [
        _build_security(
            security_id,
            "XNYS",
            SESSIONS,
            revision=hashlib.sha256(security_id.encode()).hexdigest(),
            open_price=price,
            close_price=price,
            price_overrides=overrides,
        )
        for security_id, (price, overrides) in sorted(prices.items())
    ]
    sink = InMemorySessionBatchSink()
    output = run_simulation(
        manifest=_manifest(
            securities=tuple(pinned for _, pinned in built),
            start_month=_month_str(START),
            end_month=_month_str(START),
            parameters=parameters,
        ),
        strategy=strategy_type(entries=targets),
        market_view_factory=_market_view_factory(),
        security_market_data=tuple(market for market, _ in built),
        sink=sink,
    )
    return output, sink


def _of(output: SimulationOutputV1, kind: type) -> list:
    return [event for event in output.events if isinstance(event, kind)]


def _held(output: SimulationOutputV1) -> dict[str, Decimal]:
    return {mark.security_id: mark.shares for mark in output.final_open_positions}


def _trim_and_top_up() -> tuple[SimulationOutputV1, InMemorySessionBatchSink]:
    return _run(
        {
            D0: [_target("sec-a", D0, "0.5"), _target("sec-b", D0, "0.5")],
            D2: [_target("sec-a", D2, "0.5"), _target("sec-b", D2, "0.5")],
        },
        {
            "sec-a": (100.0, _from(D2, 150.0, 150.0)),
            "sec-b": (100.0, _from(D2, 120.0, 120.0)),
        },
    )


def test_target_strategy_satisfies_the_capability_protocol() -> None:
    assert isinstance(_TargetStrategy(), TargetWeightSizingV1)
    assert not isinstance(_ScriptedStrategy(), TargetWeightSizingV1)


def test_empty_book_lands_each_weight_within_one_share_and_keeps_cash() -> None:
    weights = {"sec-a": "0.30", "sec-b": "0.20", "sec-c": "0.50"}
    prices = {"sec-a": 100.0, "sec-b": 50.0, "sec-c": 210.0}
    output, sink = _run(
        {D0: [_target(sid, D0, weight) for sid, weight in weights.items()]},
        {sid: (price, {}) for sid, price in prices.items()},
    )

    fills = _of(output, EntryFillEventV1)
    assert [fill.shares for fill in fills] == [300, 400, 238]
    for fill in fills:
        value = Decimal(fill.shares) * Decimal(str(prices[fill.security_id]))
        target = Decimal(weights[fill.security_id]) * Decimal(100000)
        assert target - value < Decimal(str(prices[fill.security_id]))
        assert fill.target_weight == Decimal(weights[fill.security_id])
        assert fill.current_weight == Decimal(0)
    assert output.final_cash_base == Decimal("20.00000000")
    assert [audit.disposition.value for audit in sink.candidate_audits] == [
        "filled"
    ] * 3


def test_trim_sells_part_before_the_top_up_buy() -> None:
    output, sink = _trim_and_top_up()

    (trim,) = _of(output, ExitFillEventV1)
    top_up = _of(output, EntryFillEventV1)[-1]
    assert (trim.security_id, trim.shares, trim.fill_session) == ("sec-a", 50, D3)
    assert trim.realized_pnl_base == Decimal("2500.00000000")
    assert trim.current_weight == Decimal("0.55555556")
    assert (top_up.security_id, top_up.shares, top_up.fill_session) == (
        "sec-b",
        62,
        D3,
    )
    assert trim.sequence < top_up.sequence
    assert _held(output) == {"sec-a": Decimal(450), "sec-b": Decimal(562)}
    marks = {mark.security_id: mark for mark in output.final_open_positions}
    assert marks["sec-a"].cost_basis_base == Decimal("45000.00000000")
    expected_basis = (Decimal(50000) + Decimal(7440)) / Decimal(562)
    assert marks["sec-b"].cost_basis_base == (
        expected_basis.quantize(Decimal("0.00000001")) * 562
    ).quantize(Decimal("0.00000001"))
    assert output.final_cash_base == Decimal("60.00000000")
    assert len(sink.candidate_audits) == 3


def test_target_zero_closes_the_position_fully() -> None:
    output, _ = _run(
        {D0: [_target("sec-a", D0, "1")], D2: [_target("sec-a", D2, "0")]},
        {"sec-a": (100.0, {})},
    )

    (close,) = _of(output, ExitFillEventV1)
    assert close.shares == 1000
    assert close.target_weight == Decimal(0)
    assert output.final_open_positions == ()


@pytest.mark.parametrize(
    "weights",
    [("0.60", "0.45"), ("-0.1", "0.2"), ("1.2", "0")],
    ids=["sum-over-one", "negative", "above-one"],
)
def test_invalid_weights_fail_closed_without_orders(weights: tuple[str, str]) -> None:
    output, sink = _run(
        {D0: [_target("sec-a", D0, weights[0]), _target("sec-b", D0, weights[1])]},
        {"sec-a": (100.0, {}), "sec-b": (100.0, {})},
    )

    skips = _of(output, SkippedSignalEventV1)
    assert [skip.reason for skip in skips] == [
        SkipReasonCode.TARGET_WEIGHTS_INVALID
    ] * 2
    assert not _of(output, EntryFillEventV1)
    assert output.final_cash_base == Decimal("100000.00000000")
    assert [audit.reason_code for audit in sink.candidate_audits] == [
        "target_weights_invalid"
    ] * 2


def test_drift_inside_rebalance_tolerance_places_no_order() -> None:
    output, _ = _run(
        {D0: [_target("sec-a", D0, "0.5")], D2: [_target("sec-a", D2, "0.5")]},
        {"sec-a": (100.0, _from(D2, 110.0, 110.0))},
        parameters={"rebalance_tolerance": 0.05},
    )

    assert len(_of(output, EntryFillEventV1)) == 1
    assert not _of(output, ExitFillEventV1)
    assert not _of(output, SkippedSignalEventV1)


def test_target_zero_closes_even_inside_rebalance_tolerance() -> None:
    output, _ = _run(
        {D0: [_target("sec-a", D0, "0.04")], D2: [_target("sec-a", D2, "0")]},
        # The price halves, so the held weight (~0.02) is inside the band.
        {"sec-a": (100.0, _from(D2, 50.0, 50.0))},
        parameters={"rebalance_tolerance": 0.03},
    )

    (close,) = _of(output, ExitFillEventV1)
    assert close.shares == 40
    assert output.final_open_positions == ()


def test_unchanged_targets_place_no_whole_share_noise_orders() -> None:
    output, sink = _run(
        {
            day: [_target("sec-a", day, "0.333"), _target("sec-b", day, "0.333")]
            for day in (D0, D2, D3)
        },
        {"sec-a": (100.0, {}), "sec-b": (70.0, {})},
    )

    assert len(_of(output, EntryFillEventV1)) == 2
    assert not _of(output, SkippedSignalEventV1)
    assert len(sink.candidate_audits) == 2


def test_duplicate_target_is_a_conflict_not_a_session_veto() -> None:
    output, _ = _run(
        {
            D0: [
                _target("sec-a", D0, "0.5"),
                _target("sec-a", D0, "0.5").model_copy(update={"rule_id": "dup"}),
                _target("sec-b", D0, "0.5"),
            ]
        },
        {"sec-a": (100.0, {}), "sec-b": (100.0, {})},
    )

    (skip,) = _of(output, SkippedSignalEventV1)
    assert skip.reason is SkipReasonCode.POSITION_CONFLICT
    assert _held(output) == {"sec-a": Decimal(500), "sec-b": Decimal(500)}


def test_trim_after_fractional_split_sells_whole_shares() -> None:
    market, pinned = _build_security(
        "sec-a",
        "XNYS",
        SESSIONS,
        revision=hashlib.sha256(b"sec-a").hexdigest(),
        open_price=99.9,
        close_price=99.9,
        price_overrides=_from(D2, 66.6, 66.6),
        split_by_session={D2: 1.5},
    )
    output = run_simulation(
        manifest=_manifest(
            securities=(pinned,),
            start_month=_month_str(START),
            end_month=_month_str(START),
        ),
        strategy=_TargetStrategy(
            entries={D0: [_target("sec-a", D0, "1")], D3: [_target("sec-a", D3, "0.5")]}
        ),
        market_view_factory=_market_view_factory(),
        security_market_data=(market,),
    )

    (split,) = _of(output, SplitAppliedEventV1)
    (trim,) = _of(output, ExitFillEventV1)
    assert split.shares_after % 1 == Decimal("0.5")
    assert trim.shares == trim.shares.to_integral_value()
    assert _held(output)["sec-a"] == split.shares_after - trim.shares


def test_strategy_without_capability_skips_target_weight_signals() -> None:
    output, _ = _run(
        {D0: [_target("sec-a", D0, "0.5")]},
        {"sec-a": (100.0, {})},
        strategy_type=_ScriptedStrategy,
    )

    (skip,) = _of(output, SkippedSignalEventV1)
    assert skip.reason is SkipReasonCode.TARGET_WEIGHT_NOT_ENABLED
    assert not _of(output, EntryFillEventV1)


def test_top_up_short_of_cash_buys_what_cash_affords() -> None:
    output, _ = _run(
        {
            D0: [_target("sec-a", D0, "0.4"), _target("sec-b", D0, "0.4")],
            D2: [_target("sec-a", D2, "0.5"), _target("sec-b", D2, "0.5")],
        },
        {"sec-a": (100.0, _from(D3, 80.0, 80.0)), "sec-b": (100.0, {})},
    )

    top_ups = [
        fill for fill in _of(output, EntryFillEventV1) if fill.fill_session == D3
    ]
    assert [(fill.security_id, fill.shares) for fill in top_ups] == [
        ("sec-a", 175),
        ("sec-b", 60),
    ]
    assert output.final_cash_base == Decimal("0E-8")


def test_new_open_counts_against_max_concurrent_positions() -> None:
    output, _ = _run(
        {D0: [_target("sec-a", D0, "0.4"), _target("sec-b", D0, "0.4")]},
        {"sec-a": (100.0, {}), "sec-b": (100.0, {})},
        parameters={"max_concurrent_positions": 1},
    )

    (skip,) = _of(output, SkippedSignalEventV1)
    assert (skip.security_id, skip.reason) == (
        "sec-b",
        SkipReasonCode.MAX_CONCURRENT_POSITIONS,
    )


def test_target_weight_replay_is_deterministic() -> None:
    first, first_sink = _trim_and_top_up()
    second, second_sink = _trim_and_top_up()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first_sink.candidate_audits == second_sink.candidate_audits


def test_unset_target_fields_are_omitted_from_dumps() -> None:
    signal = Signal(security_id="a", side=SignalSide.BUY, session=D0, rule_id="r")
    assert "target_weight" not in signal.model_dump(mode="json")
    assert "target_weight" not in _entry(1, D0).model_dump(mode="json")
    assert "current_weight" not in _exit(2, D0).model_dump(mode="json")


def test_stored_target_weight_result_reads_back_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    day, later = date(2026, 1, 2), date(2026, 1, 5)
    weights = {"target_weight": Decimal("0.5"), "current_weight": Decimal("0.6")}
    events = (
        _entry(1, day),
        _exit(2, later, "25").model_copy(
            update={
                "shares": Decimal(5),
                "proceeds_base": Decimal("525"),
                "cost_basis_base": Decimal("500"),
                **weights,
            }
        ),
    )
    _write_staging(
        repo,
        payload={
            "state_schema_version": "backtest_portfolio_state.v1",
            "portfolio_state": {"cash": "9525", "positions": []},
            "events": events,
            "equity_curve": (
                EquityCurvePointV1(
                    session=later,
                    cash_base=Decimal("9525"),
                    positions_value_base=Decimal("525"),
                    total_equity_base=Decimal("10050"),
                    sequence=3,
                ),
            ),
            "final_cash_base": Decimal("9525"),
        },
    )
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    result = repo.backtest_result(RUN_ID)

    assert result.events == events
    assert result.events[0].model_dump(mode="json").keys() == (
        _entry(1, day).model_dump(mode="json").keys()
    )


def test_worker_reconstructs_partial_positions_as_a_running_sum() -> None:
    published: list[dict[str, object]] = []
    repository = SimpleNamespace(
        append_backtest_staging_batch=lambda *_, **kwargs: published.append(
            kwargs["portfolio_state"]
        )
    )
    sink = _StagingSink(
        repository=repository,  # type: ignore[arg-type]
        state=_ClaimState(job_id="job", claim_token="token", status_version=1),
    )
    output, _ = _trim_and_top_up()
    by_session: dict[date, list[TradeLogEvent]] = {}
    for event in output.events:
        # Fills only: final open-position marks would mask the running sum.
        if isinstance(event, (EntryFillEventV1, ExitFillEventV1)):
            by_session.setdefault(event.fill_session, []).append(event)
    for point in output.equity_curve:
        sink.publish_session(
            session=point.session,
            events=tuple(by_session.get(point.session, ())),
            equity_point=point,
        )

    assert sink.open_positions == {"sec-a": Decimal(450), "sec-b": Decimal(562)}
    assert published[-1]["positions"] == [
        {"security_id": "sec-a", "shares": "450"},
        {"security_id": "sec-b", "shares": "562"},
    ]


@pytest.mark.parametrize("tolerance", [-0.01, 1.5, "0.05", True, float("nan")])
def test_invalid_rebalance_tolerance_is_fatal(tolerance: object) -> None:
    with pytest.raises(SimulationError, match="rebalance_tolerance"):
        _run(
            {D0: [_target("sec-a", D0, "0.5")]},
            {"sec-a": (100.0, {})},
            parameters={"rebalance_tolerance": tolerance},
        )


def test_target_buy_audits_satisfy_the_staging_link_checks() -> None:
    batches: list[tuple[date, tuple, tuple]] = []

    class _BatchSink(InMemorySessionBatchSink):
        def publish_session(self, *, session, events, equity_point, **kwargs):
            batches.append((session, events, kwargs.get("candidate_audits", ())))
            super().publish_session(
                session=session, events=events, equity_point=equity_point, **kwargs
            )

    targets = {
        D0: [_target("sec-a", D0, "0.5"), _target("sec-b", D0, "0.5")],
        D2: [_target("sec-a", D2, "0.3"), _target("sec-b", D2, "0.7")],
        SESSIONS[5]: [_target("sec-b", SESSIONS[5], "0.75")],
    }
    built = [
        _build_security(sid, "XNYS", SESSIONS, revision=sid[-1] * 64)
        for sid in ("sec-a", "sec-b")
    ]
    run_simulation(
        manifest=_manifest(
            securities=tuple(pinned for _, pinned in built),
            start_month=_month_str(START),
            end_month=_month_str(START),
        ),
        strategy=_TargetStrategy(entries=targets),
        market_view_factory=_market_view_factory(),
        security_market_data=tuple(market for market, _ in built),
        sink=_BatchSink(),
    )

    for session, events, audits in batches:
        BacktestRepository._validate_candidate_audit_event_links(
            session, audits, events
        )
        buy_events = {
            event.sequence
            for event in events
            if event.kind == "entry_fill"
            or (event.kind == "skipped_signal" and event.side is SignalSide.BUY)
        }
        assert buy_events == {audit.event_sequence for audit in audits}
    assert sum(len(audits) for _, _, audits in batches) == 4
