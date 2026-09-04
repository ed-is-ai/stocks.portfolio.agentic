"""Contract tests for ``PortfolioRecommendationService`` (#441).

Two in-test Strategy shapes prove the adapter is not tailored to one
implementation: ``HistoryOnlyStrategy`` reads only ``price_history`` and
never touches ``scan_result``; ``ScanPlusHistoryStrategy`` reads both.
Discovery is monkeypatched (offline); the artifact lives in tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock

import pytest

from app.repositories import db
from app.repositories.portfolio_strategies_repo import (
    PortfolioStrategiesRepository,
)
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.portfolio_recommendation import (
    EvaluationUnavailable,
    NoAssignment,
    RecommendationResultV1,
)
from app.schemas.trade import Position
from app.services import portfolio_recommendation_service as svc_module
from app.services import strategy_assignment_service as assignment_module
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)
from app.services.portfolio_recommendation_service import (
    PortfolioRecommendationService,
)
from app.services.strategy_assignment_service import StrategyAssignmentService
from tests.test_strategy_assignment_service import _discovery_result

SESSION = date(2026, 8, 28)
PREVIOUS = date(2026, 8, 27)
_SESSIONS = (PREVIOUS, SESSION)


def _ohlcv_only_requirements(
    parameters: StrategyParameters,
) -> StrategyEvidenceRequirementsV1:
    """The declaration every in-test Strategy shares: OHLCV, no minimum."""
    del parameters
    history = EvidenceRequirementV1(kind=EvidenceKind.PRICE_HISTORY, columns=("close",))
    return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))


class HistoryOnlyStrategy:
    """Entry/exit from price history alone; never calls ``scan_result``."""

    def __init__(self, *, entry_everywhere: bool = False) -> None:
        self._entry_everywhere = entry_everywhere

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare OHLCV-only needs (evidence contract v1)."""
        return _ohlcv_only_requirements(parameters)

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        securities = cast(list[str], parameters["selected_securities"])
        signals = []
        for security_id in securities:
            history = view.price_history(security_id)
            if history.empty:
                continue
            rising = history["close"].iloc[-1] > history["close"].iloc[0]
            if rising or self._entry_everywhere:
                signals.append(
                    Signal(
                        security_id=security_id,
                        side=SignalSide.BUY,
                        session=view.as_of_session,
                        rule_id="entry_history_rise",
                    )
                )
        return signals

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        del parameters
        signals = []
        for position in portfolio.positions:
            history = view.price_history(position.security_id)
            if history.empty:
                continue
            if history["close"].iloc[-1] < history["close"].iloc[0]:
                signals.append(
                    Signal(
                        security_id=position.security_id,
                        side=SignalSide.SELL,
                        session=view.as_of_session,
                        rule_id="exit_history_fall",
                    )
                )
        return signals

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        del signal, view, portfolio, parameters
        return 1


class ScanPlusHistoryStrategy:
    """Reads ``scan_result`` and ``price_history`` — the other shape."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare OHLCV-only needs (evidence contract v1)."""
        return _ohlcv_only_requirements(parameters)

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        securities = cast(list[str], parameters["selected_securities"])
        signals = []
        for security_id in securities:
            if view.scan_result(security_id) is None:
                continue
            history = view.price_history(security_id)
            if history.empty:
                continue
            if history["close"].iloc[-1] > history["close"].iloc[0]:
                signals.append(
                    Signal(
                        security_id=security_id,
                        side=SignalSide.BUY,
                        session=view.as_of_session,
                        rule_id="entry_scan_rise",
                    )
                )
        return signals

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        del parameters
        signals = []
        for position in portfolio.positions:
            if view.scan_result(position.security_id) is None:
                continue
            history = view.price_history(position.security_id)
            if history.empty:
                continue
            if history["close"].iloc[-1] < history["close"].iloc[0]:
                signals.append(
                    Signal(
                        security_id=position.security_id,
                        side=SignalSide.SELL,
                        session=view.as_of_session,
                        rule_id="exit_scan_fall",
                    )
                )
        return signals

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        del signal, view, portfolio, parameters
        return 1


class FailingStrategy:
    """Raises from ``exit_signals`` — the runtime-failure shape."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare OHLCV-only needs (evidence contract v1)."""
        return _ohlcv_only_requirements(parameters)

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        return []

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        raise RuntimeError("boom")

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        return 1


class _ScanResultForbiddenView:
    """Wrapper whose ``scan_result`` fails the test if ever called."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        if name == "scan_result":
            raise AssertionError("HistoryOnlyStrategy must not call scan_result")
        return getattr(self._inner, name)


def _history(closes: list[float]) -> list[dict[str, float | int | str]]:
    """Newest-first daily bars from oldest→newest closes."""
    return [
        {
            "date": session.isoformat(),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
        }
        for close, session in zip(reversed(closes), reversed(_SESSIONS))
    ]


def _record(ticker: str, closes: list[float]) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "as_of": SESSION.isoformat(),
        "price": closes[-1],
        "volume": 1000,
        "rel_volume": 1.0,
        "high_52w": 13.0,
        "low_52w": 9.0,
        "pct_from_52w_high": -1.0,
        "pct_change_week": 0.5,
        "ohlcv_history": _history(closes),
    }


def _write_artifact(
    path: Path, records: list[dict[str, Any]], *, generated_at: datetime
) -> None:
    path.write_text(
        json.dumps(
            build_analysis_payload(records, run_id="run-1", generated_at=generated_at)
        ),
        encoding="utf-8",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.execute(
        "INSERT INTO portfolios (id, name, created_at) VALUES (7, 'SIPP', 'now')"
    )
    conn.commit()
    conn.close()
    return path


def _assignment(db_path: Path, tmp_path: Path) -> StrategyAssignmentService:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    return StrategyAssignmentService(
        repo,
        skills_root=tmp_path / "skills",
        analysis_path=tmp_path / "analysis.json",
    )


@pytest.fixture
def env(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Assignment service + tmp artifact: AAA falling, BBB rising."""
    monkeypatch.setattr(
        assignment_module, "discover_strategies", lambda root: _discovery_result()
    )
    monkeypatch.setattr(svc_module, "ANALYSIS_JSON", tmp_path / "analysis.json")
    _write_artifact(
        tmp_path / "analysis.json",
        [_record("AAA", [12.0, 10.0]), _record("BBB", [10.0, 12.0])],
        generated_at=datetime.now(UTC),
    )
    return SimpleNamespace(tmp_path=tmp_path, assignment=_assignment(db_path, tmp_path))


def _service(
    env: Any,
    strategy: Any,
    positions: list[Position],
    *,
    loader: Callable[[Path], Any] | None = None,
) -> PortfolioRecommendationService:
    trader = MagicMock()
    trader.get_portfolio.return_value = positions
    trader.get_cash_balance.return_value = 1000.0
    return PortfolioRecommendationService(
        assignment_service=env.assignment,
        trader=trader,
        skills_root=env.tmp_path / "skills",
        loader=loader if loader is not None else (lambda path: strategy),
    )


def _position(ticker: str) -> Position:
    return Position(ticker=ticker, shares=100.0, avg_cost=10.0, total_cost=1000.0)


def test_exit_on_held_sells(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    # AAA (held, falling) sells; BBB (unheld, rising) is a Buy.
    assert [(rec.action, rec.rule_id) for rec in result.recommendations] == [
        ("sell", "exit_history_fall"),
        ("buy", "entry_history_rise"),
    ]
    assert result.recommendations[0].reason == "Strategy rule exit_history_fall"


def test_held_without_exit_holds(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("BBB")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert [(rec.action, rec.rule_id) for rec in result.recommendations] == [
        ("hold", "no_exit_signal")
    ]
    assert result.recommendations[0].evidence_warnings == ()


def test_held_missing_from_scan_fails_safe(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("ZZZ")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    # ZZZ fails safe to Hold; BBB (unheld, rising) is a Buy.
    assert [rec.action for rec in result.recommendations] == ["hold", "buy"]
    assert result.recommendations[0].rule_id == "scan_evidence_missing"
    assert result.recommendations[0].evidence_warnings == ("scan_evidence_missing",)


def test_entry_on_unheld_buys_and_unheld_without_entry_omitted(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), []).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert [(rec.action, rec.security_id) for rec in result.recommendations] == [
        ("buy", "BBB")
    ]
    assert "AAA" not in {rec.security_id for rec in result.recommendations}


def test_sell_precedence_when_both_signals(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    strategy = HistoryOnlyStrategy(entry_everywhere=True)
    result = _service(env, strategy, [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    aaa = [rec for rec in result.recommendations if rec.security_id == "AAA"]
    assert len(aaa) == 1
    assert aaa[0].action == "sell"


def test_scan_plus_history_strategy_reads_scan_result(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    result = _service(env, ScanPlusHistoryStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert [rec.rule_id for rec in result.recommendations] == [
        "exit_scan_fall",
        "entry_scan_rise",
    ]


def test_history_only_strategy_never_reads_scan_result(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.assignment.assign(7, "alpha")
    real_build = svc_module.build_scan_market_view

    def guarded_build(
        records: Any,
        aliases: Any,
        as_of_session: Any = None,
        current_evidence: Any = None,
    ) -> Any:
        view, unresolved = real_build(records, aliases, as_of_session, current_evidence)
        return _ScanResultForbiddenView(view), unresolved

    monkeypatch.setattr(svc_module, "build_scan_market_view", guarded_build)
    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == ["sell", "buy"]


def test_determinism_same_inputs_identical_result(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    service = _service(env, HistoryOnlyStrategy(), [_position("AAA"), _position("BBB")])
    first = service.recommend(7)
    second = service.recommend(7)
    assert isinstance(first, RecommendationResultV1)
    assert isinstance(second, RecommendationResultV1)
    # evaluated_at is the wall-clock stamp; everything else is identical.
    assert first.model_dump(exclude={"evaluated_at"}) == second.model_dump(
        exclude={"evaluated_at"}
    )


def test_no_assignment(env: Any) -> None:
    outcome = _service(env, HistoryOnlyStrategy(), []).recommend(7)
    assert isinstance(outcome, NoAssignment)


def test_unavailable_assignment(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    env.assignment._repo.upsert(7, "ghost", {})
    outcome = _service(env, HistoryOnlyStrategy(), []).recommend(7)
    assert isinstance(outcome, EvaluationUnavailable)
    assert "no longer discoverable" in outcome.reason


def test_runtime_load_failure(env: Any) -> None:
    env.assignment.assign(7, "alpha")

    def broken_loader(path: Path) -> Any:
        raise RuntimeError("no module")

    outcome = _service(
        env, HistoryOnlyStrategy(), [_position("AAA")], loader=broken_loader
    ).recommend(7)
    assert isinstance(outcome, EvaluationUnavailable)
    assert outcome.reason.startswith("Strategy runtime could not be loaded:")


def test_runtime_evaluation_failure(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    outcome = _service(env, FailingStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(outcome, EvaluationUnavailable)
    assert "Strategy evaluation failed:" in outcome.reason


def test_result_carries_provenance(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert result.portfolio_id == 7
    assert result.analysis_run_id == "run-1"
    assert result.market_session == SESSION
    assert result.freshness == "fresh"
    assert result.strategy_id == "alpha"
    assert result.strategy_source_digest == "a" * 64
    assert result.parameters["lookback"] == 20
    assert result.parameters["selected_securities"] == ["AAA", "BBB"]


def test_result_names_universe_parameter_and_keeps_full_universe(env: Any) -> None:
    """gh-474: the display split is typed, the stored universe untouched."""
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert result.universe_parameter == "selected_securities"
    # The complete universe stays verbatim in the typed result.
    assert result.parameters["selected_securities"] == ["AAA", "BBB"]
    assert result.universe_symbols == ("AAA", "BBB")
    assert dict(result.tuning_parameters) == {"lookback": 20}


def test_stale_artifact_still_evaluates(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    _write_artifact(
        env.tmp_path / "analysis.json",
        [_record("AAA", [12.0, 10.0]), _record("BBB", [10.0, 12.0])],
        generated_at=datetime.now(UTC) - timedelta(hours=25),
    )
    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert result.freshness == "stale"
    assert [rec.action for rec in result.recommendations] == ["sell", "buy"]


def test_empty_artifact_is_unavailable_with_freshness(env: Any) -> None:
    env.assignment.assign(7, "alpha")
    _write_artifact(
        env.tmp_path / "analysis.json",
        [],
        generated_at=datetime.now(UTC),
    )
    outcome = _service(env, HistoryOnlyStrategy(), []).recommend(7)
    assert isinstance(outcome, EvaluationUnavailable)
    assert "No published scan artifact" in outcome.reason
    assert outcome.freshness == "fresh"


def test_fail_safe_hold_beats_exit_signal_on_missing_evidence(env: Any) -> None:
    """A runtime exit for a holding the scan cannot evidence is still a
    Hold with a warning — never a Sell from missing evidence (AC #441.6)."""

    class PortfolioOnlyExitStrategy:
        """Emits an exit for every held position without scan evidence."""

        def evidence_requirements(
            self, parameters: StrategyParameters
        ) -> StrategyEvidenceRequirementsV1:
            """Declare OHLCV-only needs (evidence contract v1)."""
            return _ohlcv_only_requirements(parameters)

        def entry_signals(
            self, view: MarketViewV1, parameters: StrategyParameters
        ) -> list[Signal]:
            return []

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
                    rule_id="exit_time_stop",
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
            return 1

    env.assignment.assign(7, "alpha")
    result = _service(env, PortfolioOnlyExitStrategy(), [_position("ZZZ")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == ["hold"]
    assert result.recommendations[0].rule_id == "scan_evidence_missing"
    assert result.recommendations[0].evidence_warnings == ("scan_evidence_missing",)


def test_held_alias_canonicalizes_to_scan_security_id(env: Any) -> None:
    """A held provider ticker resolving through aliases matches the scan."""
    env.assignment.assign(7, "alpha")
    monkeypatched_aliases = {"OLDA": "AAA"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(svc_module, "load_aliases", lambda: monkeypatched_aliases)
    try:
        result = _service(env, HistoryOnlyStrategy(), [_position("OLDA")]).recommend(7)
    finally:
        monkey.undo()
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == ["sell", "buy"]
    assert result.recommendations[0].security_id == "AAA"


def test_held_row_shows_display_symbol_with_canonical_security_id(env: Any) -> None:
    """GH-473: an aliased holding surfaces its portfolio import spelling as
    ``ticker`` while ``security_id`` stays the canonical id the exit signal
    was keyed on -- so the row is still a Sell."""
    env.assignment.assign(7, "alpha")
    held = Position(
        ticker="AAA",
        shares=100.0,
        avg_cost=10.0,
        total_cost=1000.0,
        display_ticker="HSFWA",
    )
    result = _service(env, HistoryOnlyStrategy(), [held]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    sell = result.recommendations[0]
    assert (sell.action, sell.ticker, sell.security_id) == ("sell", "HSFWA", "AAA")


def test_unaliased_held_row_display_symbol_equals_security_id(env: Any) -> None:
    """A holding with no distinct import spelling shows one symbol only --
    nothing for the screen to render as secondary canonical text."""
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("BBB")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    hold = result.recommendations[0]
    assert hold.ticker == hold.security_id == "BBB"


def test_buy_candidate_ticker_equals_security_id(env: Any) -> None:
    """An unheld Buy candidate has no portfolio spelling at all, so its
    display ticker is simply the canonical scan id."""
    env.assignment.assign(7, "alpha")
    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    buy = result.recommendations[1]
    assert (buy.action, buy.ticker, buy.security_id) == ("buy", "BBB", "BBB")


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_canonical_holdings_collapse_deterministically(
    env: Any, reverse: bool
) -> None:
    """Two lots canonicalizing to one id yield exactly one row, and the
    surviving display symbol is the lexicographically smallest one
    regardless of the order the trader returns the lots in."""
    env.assignment.assign(7, "alpha")
    lots = [
        Position(
            ticker="AAA",
            shares=100.0,
            avg_cost=10.0,
            total_cost=1000.0,
            display_ticker=spelling,
        )
        for spelling in ("ZZZ.L", "AAA.L")
    ]
    result = _service(
        env, HistoryOnlyStrategy(), list(reversed(lots)) if reverse else lots
    ).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    held_rows = [rec for rec in result.recommendations if rec.security_id == "AAA"]
    assert len(held_rows) == 1
    assert held_rows[0].ticker == "AAA.L"


def test_off_session_and_wrong_side_signals_are_ignored(env: Any) -> None:
    """Signals dated off the market session or with the wrong side never
    become recommendation rows."""

    class OffSessionStrategy:
        def evidence_requirements(
            self, parameters: StrategyParameters
        ) -> StrategyEvidenceRequirementsV1:
            """Declare OHLCV-only needs (evidence contract v1)."""
            return _ohlcv_only_requirements(parameters)

        def entry_signals(
            self, view: MarketViewV1, parameters: StrategyParameters
        ) -> list[Signal]:
            return [
                Signal(
                    security_id="BBB",
                    side=SignalSide.BUY,
                    session=PREVIOUS,  # off-session
                    rule_id="entry_past",
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
                    security_id="AAA",
                    side=SignalSide.BUY,  # wrong side for an exit list
                    session=view.as_of_session,
                    rule_id="exit_wrong_side",
                )
            ]

        def position_size(
            self,
            signal: Signal,
            view: MarketViewV1,
            portfolio: PortfolioView,
            parameters: StrategyParameters,
        ) -> int:
            return 1

    env.assignment.assign(7, "alpha")
    result = _service(env, OffSessionStrategy(), [_position("AAA")]).recommend(7)
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == ["hold"]


def test_real_discovered_runtime_evaluates_against_adapter(
    env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke-test a REAL discovered runtime (history-only Darvas Box) through
    the adapter — proves the honest projection satisfies a real Strategy
    without fabricated stage/vcp evidence (spec Block If check)."""
    from app.core.config import SKILLS_DIR
    from app.services.backtest.skill_discovery import (
        discover_strategies,
    )
    from app.services.backtest.worker import _load_strategy_instance

    # Undo the env fixture's fake discovery: this test needs the REAL scan.
    monkeypatch.setattr(assignment_module, "discover_strategies", discover_strategies)
    discovered = discover_strategies(SKILLS_DIR).strategies
    descriptor = next(
        (d for d in discovered if d.strategy_id == "rtly-backtest-darvas-box"),
        None,
    )
    if descriptor is None:
        pytest.skip("darvas-box skill not discovered in this checkout")
    # A dedicated assignment seam pointed at the REAL skills tree.
    assignment = StrategyAssignmentService(
        PortfolioStrategiesRepository(db.make_connect(lambda: tmp_path / "trades.db")),
        skills_root=SKILLS_DIR,
        analysis_path=tmp_path / "analysis.json",
    )
    assignment._repo.upsert(7, descriptor.strategy_id, {})
    service = PortfolioRecommendationService(
        assignment_service=assignment,
        trader=MagicMock(),
        skills_root=SKILLS_DIR,
        loader=_load_strategy_instance,
    )
    service._trader.get_portfolio.return_value = [_position("AAA")]
    service._trader.get_cash_balance.return_value = 1000.0
    outcome = service.recommend(7)
    # The real runtime must evaluate cleanly against the honest projection —
    # any crash here is a spec Block If, not something to paper over.
    if isinstance(outcome, EvaluationUnavailable):
        pytest.fail(f"real runtime failed against the adapter: {outcome.reason}")
    assert isinstance(outcome, RecommendationResultV1)
    assert outcome.strategy_id == "rtly-backtest-darvas-box"


# ---------------------------------------------------------------------------
# #472 -- Strategy-owned structured explanations reach the result rows
# ---------------------------------------------------------------------------

from app.services.backtest.strategy_explanation import (  # noqa: E402
    ComparisonOperator,
    EvidenceUnit,
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
)

EXIT_EXPLANATION = SignalExplanationV1(
    reasons=(
        SignalReasonV1(
            code="maximum_loss_stop",
            summary="Close hit the maximum-loss stop for this position.",
            facts=(
                ExplanationFactV1(
                    label="Close",
                    observed=Decimal("9.50"),
                    operator=ComparisonOperator.LTE,
                    threshold=Decimal("9.90"),
                    unit=EvidenceUnit.PRICE,
                ),
            ),
        ),
        SignalReasonV1(
            code="close_below_sma150",
            summary="Close fell below the 150-session moving average.",
        ),
    )
)
ENTRY_EXPLANATION = SignalExplanationV1(
    reasons=(
        SignalReasonV1(
            code="breakout_above_prior_high",
            summary="Close broke above its prior breakout-window high.",
            facts=(
                ExplanationFactV1(
                    label="Close",
                    observed=Decimal("12"),
                    operator=ComparisonOperator.GT,
                    threshold=Decimal("11"),
                    unit=EvidenceUnit.PRICE,
                ),
            ),
        ),
    )
)


class ExplainingStrategy(HistoryOnlyStrategy):
    """``HistoryOnlyStrategy`` that explains every signal it emits (#472)."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        return [
            signal.model_copy(update={"explanation": ENTRY_EXPLANATION})
            for signal in super().entry_signals(view, parameters)
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        return [
            signal.model_copy(update={"explanation": EXIT_EXPLANATION})
            for signal in super().exit_signals(view, portfolio, parameters)
        ]


def test_explained_signals_project_onto_typed_recommendation_rows(env: Any) -> None:
    env.assignment.assign(7, "alpha")

    result = _service(env, ExplainingStrategy(), [_position("AAA")]).recommend(7)

    assert isinstance(result, RecommendationResultV1)
    sell, buy = result.recommendations
    assert [(row.code, row.summary, row.facts) for row in sell.explanation] == [
        (
            "close_below_sma150",
            "Close fell below the 150-session moving average.",
            (),
        ),
        (
            "maximum_loss_stop",
            "Close hit the maximum-loss stop for this position.",
            ("Close 9.5 <= 9.9",),
        ),
    ]
    assert sell.reason == (
        "Close fell below the 150-session moving average. · "
        "Close hit the maximum-loss stop for this position."
    )
    assert sell.rule_id == "exit_history_fall"
    assert [row.code for row in buy.explanation] == ["breakout_above_prior_high"]
    assert buy.reason == "Close broke above its prior breakout-window high."
    assert buy.rule_id == "entry_history_rise"


def test_unexplained_signals_keep_the_generic_wording(env: Any) -> None:
    env.assignment.assign(7, "alpha")

    result = _service(env, HistoryOnlyStrategy(), [_position("AAA")]).recommend(7)

    assert isinstance(result, RecommendationResultV1)
    assert all(rec.explanation == () for rec in result.recommendations)
    assert result.recommendations[0].reason == "Strategy rule exit_history_fall"


def test_host_generated_hold_rows_carry_no_strategy_explanation(env: Any) -> None:
    env.assignment.assign(7, "alpha")

    result = _service(env, ExplainingStrategy(), [_position("BBB")]).recommend(7)

    assert isinstance(result, RecommendationResultV1)
    hold = result.recommendations[0]
    assert (hold.action, hold.rule_id) == ("hold", "no_exit_signal")
    assert hold.explanation == ()
    assert hold.reason == "No exit signal from the assigned Strategy — position held."
