"""Evidence-aware recommendation mapping (#471).

Proves the three outcomes the generic evidence contract produces —
supported, structurally unsupported, and supported-but-under-evidenced —
for both the entry and exit paths, and that neither missing kind can
manufacture a Sell or a Buy. Reuses the artifact/assignment fixtures from
``tests.test_portfolio_recommendation_service`` (AAA falling, BBB rising).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.repositories import db

from app.schemas.portfolio_recommendation import (
    EvaluationUnavailable,
    RecommendationResultV1,
)
from app.services.backtest.run_universe import (
    RunUniverseError,
    RunUniverseErrorCode,
)
from app.services.backtest.skill_discovery import StrategyDescriptorV1
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
from app.services import portfolio_recommendation_service as svc_module
from app.services import strategy_assignment_service as assignment_module
from tests.test_strategy_assignment_service import _discovery_result
from tests.test_portfolio_recommendation_service import (
    _assignment,
    _position,
    _record,
    _service,
    _write_artifact,
)


@pytest.fixture(name="db_path")
def _evidence_db_path(tmp_path: Path) -> Path:
    """One portfolio (id 7) in a throwaway trades database."""
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.execute(
        "INSERT INTO portfolios (id, name, created_at) VALUES (7, 'SIPP', 'now')"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(name="env")
def _evidence_env(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
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


_HISTORY = EvidenceRequirementV1(kind=EvidenceKind.PRICE_HISTORY)
_STAGE = EvidenceRequirementV1(kind=EvidenceKind.SCAN_STAGE)
_VCP = EvidenceRequirementV1(kind=EvidenceKind.SCAN_VCP)
_DEEP_HISTORY = EvidenceRequirementV1(
    kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=500
)


class EagerStrategy:
    """Always sells every holding and buys every selected security.

    Deliberately maximal: any Sell or Buy that survives is proof the host
    invoked a path it should have skipped, or mapped a security it should
    have withheld.
    """

    def __init__(self, requirements: StrategyEvidenceRequirementsV1) -> None:
        self._requirements = requirements

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Return the declaration this instance was constructed with."""
        del parameters
        return self._requirements

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        """Emit a BUY for every selected security."""
        securities: Any = parameters["selected_securities"]
        return [
            Signal(
                security_id=security_id,
                side=SignalSide.BUY,
                session=view.as_of_session,
                rule_id="entry_always",
            )
            for security_id in securities
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        """Emit a SELL for every held position."""
        del parameters
        return [
            Signal(
                security_id=position.security_id,
                side=SignalSide.SELL,
                session=view.as_of_session,
                rule_id="exit_always",
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
        """One share — sizing is irrelevant to these assertions."""
        del signal, view, portfolio, parameters
        return 1


class UndeclaredStrategy(EagerStrategy):
    """The same shape with no evidence declaration at all.

    ``None`` rather than a method: ``getattr`` finds the attribute but it
    is not callable, which is exactly how a legacy runtime looks.
    """

    evidence_requirements = None  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__(StrategyEvidenceRequirementsV1())


def _requirements(
    entry: tuple[EvidenceRequirementV1, ...],
    exit_: tuple[EvidenceRequirementV1, ...],
) -> StrategyEvidenceRequirementsV1:
    return StrategyEvidenceRequirementsV1(entry=entry, exit=exit_)


def _recommend(env: Any, strategy: Any, tickers: list[str]) -> Any:
    env.assignment.assign(7, "alpha")
    positions = [_position(ticker) for ticker in tickers]
    return _service(env, strategy, positions).recommend(7)


# --- supported -------------------------------------------------------------


def test_supported_evidence_evaluates_both_paths(env: Any) -> None:
    strategy = EagerStrategy(_requirements((_HISTORY,), (_HISTORY,)))
    result = _recommend(env, strategy, ["AAA"])
    assert isinstance(result, RecommendationResultV1)
    assert [(rec.action, rec.security_id) for rec in result.recommendations] == [
        ("sell", "AAA"),
        ("buy", "BBB"),
    ]
    assert result.coverage.complete
    assert result.coverage.entry_state == "compatible"
    assert result.coverage.exit_state == "compatible"
    assert result.coverage.evaluated_securities == 2
    assert result.coverage.degraded_securities == ()


# --- structurally unsupported ---------------------------------------------


def test_missing_stage_evidence_cannot_create_a_sell(env: Any) -> None:
    """The #471 regression: a scan-dependent exit rule against an OHLCV
    view must Hold with a typed warning, never Sell."""
    strategy = EagerStrategy(_requirements((_HISTORY, _STAGE), (_HISTORY, _STAGE)))
    result = _recommend(env, strategy, ["AAA"])
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == ["hold"]
    held = result.recommendations[0]
    assert held.security_id == "AAA"
    assert held.rule_id == "exit_evidence_unsupported"
    assert "exit_evidence_unsupported" in held.evidence_warnings
    assert "scan_stage" in held.evidence_warnings
    assert result.coverage.exit_state == "incompatible"
    assert result.coverage.exit_missing_evidence == ("scan_stage",)


def test_unsupported_entry_path_yields_no_buys_with_diagnostics(env: Any) -> None:
    """``Buy 0`` here is distinguishable from a complete evaluation."""
    strategy = EagerStrategy(_requirements((_HISTORY, _STAGE, _VCP), (_HISTORY,)))
    result = _recommend(env, strategy, [])
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == []
    assert result.coverage.entry_state == "incompatible"
    assert result.coverage.exit_state == "compatible"
    assert set(result.coverage.entry_missing_evidence) == {"scan_stage", "scan_vcp"}
    assert not result.coverage.complete


def test_unsupported_entry_does_not_block_a_supported_exit(env: Any) -> None:
    """The two paths are independent: exits still evaluate normally."""
    strategy = EagerStrategy(_requirements((_STAGE,), (_HISTORY,)))
    result = _recommend(env, strategy, ["AAA"])
    assert isinstance(result, RecommendationResultV1)
    assert [(rec.action, rec.rule_id) for rec in result.recommendations] == [
        ("sell", "exit_always")
    ]
    assert result.coverage.exit_state == "compatible"


# --- supported but under-evidenced ----------------------------------------


def test_thin_history_holds_the_position_and_withholds_the_buy(env: Any) -> None:
    strategy = EagerStrategy(_requirements((_DEEP_HISTORY,), (_DEEP_HISTORY,)))
    result = _recommend(env, strategy, ["AAA"])
    assert isinstance(result, RecommendationResultV1)
    assert [rec.action for rec in result.recommendations] == ["hold"]
    held = result.recommendations[0]
    assert held.rule_id == "evidence_incomplete"
    assert "evidence_incomplete" in held.evidence_warnings
    assert result.coverage.entry_state == "degraded"
    assert result.coverage.exit_state == "degraded"
    assert result.coverage.degraded_securities == ("AAA", "BBB")
    assert not result.coverage.complete


def test_thin_history_on_entry_only_still_sells(env: Any) -> None:
    """A degraded entry path never suppresses a properly evidenced exit."""
    strategy = EagerStrategy(_requirements((_DEEP_HISTORY,), (_HISTORY,)))
    result = _recommend(env, strategy, ["AAA"])
    assert isinstance(result, RecommendationResultV1)
    assert [(rec.action, rec.security_id) for rec in result.recommendations] == [
        ("sell", "AAA")
    ]
    assert result.coverage.entry_state == "degraded"
    assert result.coverage.exit_state == "compatible"


# --- undeclared ------------------------------------------------------------


def test_undeclared_requirements_are_never_treated_as_safe(env: Any) -> None:
    outcome = _recommend(env, UndeclaredStrategy(), ["AAA"])
    assert isinstance(outcome, EvaluationUnavailable)
    assert "does not declare its evidence requirements" in outcome.reason
    assert "evidence contract v1" in outcome.reason
    assert outcome.freshness == "fresh"


def test_declaration_that_raises_is_a_typed_outcome(env: Any) -> None:
    class ExplodingStrategy(EagerStrategy):
        def evidence_requirements(
            self, parameters: StrategyParameters
        ) -> StrategyEvidenceRequirementsV1:
            raise RuntimeError("boom")

    strategy = ExplodingStrategy(_requirements((_HISTORY,), (_HISTORY,)))
    outcome = _recommend(env, strategy, ["AAA"])
    assert isinstance(outcome, EvaluationUnavailable)
    assert "Strategy evidence declaration failed" in outcome.reason


# --- support labels --------------------------------------------------------


def test_strategy_support_is_fail_soft(env: Any) -> None:
    """Support labelling never raises, even when a runtime will not load."""

    def broken_loader(path: Any) -> Any:
        raise RuntimeError("no module")

    service = _service(env, None, [], loader=broken_loader)
    support = service.strategy_support()
    assert set(support) == {"alpha", "beta"}
    assert set(support.values()) == {"unknown"}


def test_strategy_support_labels_a_history_only_strategy(env: Any) -> None:
    strategy = EagerStrategy(_requirements((_HISTORY,), (_HISTORY,)))
    support = _service(env, strategy, []).strategy_support()
    assert support == {"alpha": "supported", "beta": "supported"}


def test_strategy_support_labels_a_scan_dependent_strategy(env: Any) -> None:
    strategy = EagerStrategy(_requirements((_HISTORY, _STAGE), (_HISTORY, _STAGE)))
    support = _service(env, strategy, []).strategy_support()
    assert support == {"alpha": "backtest_only", "beta": "backtest_only"}


@pytest.mark.parametrize("tickers", [[], ["AAA"], ["ZZZ"]])
def test_every_evidence_state_returns_a_typed_outcome(
    env: Any, tickers: list[str]
) -> None:
    strategy = EagerStrategy(_requirements((_HISTORY, _STAGE), (_HISTORY, _STAGE)))
    result = _recommend(env, strategy, tickers)
    assert isinstance(result, RecommendationResultV1)
    assert all(rec.action != "sell" for rec in result.recommendations)
    assert all(rec.action != "buy" for rec in result.recommendations)


def test_exit_path_ignores_universe_candidates(env: Any) -> None:
    """A thin-history candidate must not degrade the holdings-only path."""
    strategy = EagerStrategy(_requirements((_HISTORY,), (_DEEP_HISTORY,)))
    result = _recommend(env, strategy, [])
    assert isinstance(result, RecommendationResultV1)
    # No holdings, so the exit path has nothing to be degraded by.
    assert result.coverage.exit_state == "compatible"
    assert result.coverage.degraded_securities == ()
    assert [rec.action for rec in result.recommendations] == ["buy", "buy"]


def test_entry_path_ignores_holdings_it_never_considers(env: Any) -> None:
    """A holding the entry path never considers must not degrade entry.

    ``ZZZ`` is held but absent from the scan, so it has no coverage at
    all: the exit path (holdings only) is degraded by it, while the entry
    path (candidates only) stays compatible and still produces Buys.
    """
    strategy = EagerStrategy(_requirements((_HISTORY,), (_HISTORY,)))
    result = _recommend(env, strategy, ["ZZZ"])
    assert isinstance(result, RecommendationResultV1)
    assert result.coverage.entry_state == "compatible"
    assert result.coverage.exit_state == "degraded"
    assert result.coverage.degraded_securities == ("ZZZ",)
    # The fail-safe scan rule still wins for the unevidenced holding.
    assert [(rec.action, rec.security_id) for rec in result.recommendations] == [
        ("hold", "ZZZ"),
        ("buy", "AAA"),
        ("buy", "BBB"),
    ]
    assert result.recommendations[0].rule_id == "scan_evidence_missing"


def test_universe_binding_failure_is_a_typed_outcome(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bind_universe`` can reject a selection — never a 500 (#471)."""

    def exploding_bind_universe(self: Any, selected: Any) -> Any:
        raise RunUniverseError(
            RunUniverseErrorCode.EMPTY_UNIVERSE, "no securities selected"
        )

    monkeypatch.setattr(StrategyDescriptorV1, "bind_universe", exploding_bind_universe)
    strategy = EagerStrategy(_requirements((_HISTORY,), (_HISTORY,)))
    outcome = _recommend(env, strategy, ["AAA"])
    assert isinstance(outcome, EvaluationUnavailable)
    assert outcome.reason.startswith("Strategy universe could not be bound:")
    assert outcome.freshness == "fresh"
