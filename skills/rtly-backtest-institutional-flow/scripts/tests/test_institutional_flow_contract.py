"""Contract tests for the institutional-flow Strategy on synthetic scan records."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

from app.services.backtest.historical_scan_record import (
    EnrichmentV1,
    HistoricalScanRecordV1,
)
from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    SignalSide,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
)

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "backtest"
    / "fixtures"
    / "historical_scan_record_v1.json"
)
SEC = "sec-001"


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "strategy.py"
    spec = spec_from_file_location("institutional_flow_strategy", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


strategy = _load().strategy


def _base_payload() -> dict[str, object]:
    record = HistoricalScanRecordV1.from_canonical_json(
        FIXTURE.read_bytes().rstrip(b"\n")
    )
    return record.model_dump(mode="python")


SCAN_DATE: date = _base_payload()["as_of_session_date"]  # type: ignore[assignment]


def _record(
    funds_net: int | None = 5,
    quality: str = "observed_bau",
    security_id: str = SEC,
) -> HistoricalScanRecordV1:
    payload = _base_payload()
    payload["provenance_quality"] = quality
    payload["security_id"] = security_id
    # Reconstructed records must carry null enrichment (policy).
    observed = quality == "observed_bau"
    payload["enrichment"] = EnrichmentV1(
        funds_net=funds_net if observed else None
    ).model_dump(mode="python")
    return HistoricalScanRecordV1.model_validate(payload)


class _View:
    def __init__(self, record: object, days_after: int = 0) -> None:
        self.as_of_session = SCAN_DATE + timedelta(days=days_after)
        self._record = record

    def price_history(self, security_id: str, **_: object):  # noqa: ANN201
        raise AssertionError("strategy must not read prices")

    def scan_result(self, security_id: str):  # noqa: ANN201
        return self._record


def _params(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "selected_securities": [SEC],
        "min_funds_net": 3,
        "exit_funds_net": 0,
        "max_scan_age_days": 45,
    }
    return {**base, **over}


def _portfolio(held: bool) -> PortfolioView:
    positions = (
        (
            PositionSummaryV1(
                security_id=SEC, quantity=Decimal(7), average_cost=Decimal(1)
            ),
        )
        if held
        else ()
    )
    return PortfolioView(
        as_of_session=SCAN_DATE,
        base_currency="GBP",
        cash=Decimal(1000),
        positions=positions,
        volatility_observations=(),
    )


def test_conforms_to_protocol() -> None:
    assert isinstance(strategy, StrategyProtocolV1)


def test_entry_on_strong_observed_flow_sets_priority() -> None:
    signals = validate_entry_signals(
        strategy.entry_signals(_View(_record(5)), _params())
    )
    assert [(s.side, s.priority) for s in signals] == [(SignalSide.BUY, Decimal(5))]


def test_entry_threshold_is_inclusive() -> None:
    assert strategy.entry_signals(_View(_record(3)), _params())
    assert not strategy.entry_signals(_View(_record(2)), _params())


@pytest.mark.parametrize("case", ["none", "null_flow", "reconstructed", "other_sec"])
def test_fails_closed_without_usable_evidence(case: str) -> None:
    records = {
        "none": None,
        "null_flow": _record(None),
        "reconstructed": _record(9, quality="best_effort_reconstructed"),
        "other_sec": _record(9, security_id="sec-999"),
    }
    view = _View(records[case])
    assert strategy.entry_signals(view, _params()) == []
    assert strategy.exit_signals(view, _portfolio(True), _params()) == []


def test_stale_record_gives_no_entry_and_holds_position() -> None:
    stale_buy = _View(_record(9), days_after=46)
    assert strategy.entry_signals(stale_buy, _params()) == []
    stale_sell = _View(_record(-4), days_after=46)
    assert strategy.exit_signals(stale_sell, _portfolio(True), _params()) == []
    edge = _View(_record(-4), days_after=45)
    assert strategy.exit_signals(edge, _portfolio(True), _params())


def test_future_dated_record_is_refused() -> None:
    view = _View(_record(9), days_after=-1)
    assert strategy.entry_signals(view, _params()) == []


def test_exit_only_when_held_and_flow_faded() -> None:
    view = _View(_record(0))
    exits = validate_exit_signals(
        strategy.exit_signals(view, _portfolio(True), _params())
    )
    assert exits[0].side == SignalSide.SELL
    assert strategy.exit_signals(view, _portfolio(False), _params()) == []
    assert strategy.exit_signals(_View(_record(1)), _portfolio(True), _params()) == []


def test_bad_universe_or_parameters_emit_nothing() -> None:
    view = _View(_record(9))
    assert strategy.entry_signals(view, _params(selected_securities=SEC)) == []
    assert strategy.entry_signals(view, _params(min_funds_net=None)) == []


def test_position_size() -> None:
    view = _View(_record(0))
    sell = strategy.exit_signals(view, _portfolio(True), _params())[0]
    sized = strategy.position_size(sell, view, _portfolio(True), _params())
    assert validate_position_size(sized) == 7
    buy = strategy.entry_signals(_View(_record(9)), _params())[0]
    assert strategy.position_size(buy, view, _portfolio(False), _params()) == 0
