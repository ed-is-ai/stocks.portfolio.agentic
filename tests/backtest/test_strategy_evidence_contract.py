"""Every discovered first-party Strategy declares its evidence needs (#471).

Parameterized over real discovery, so a new Strategy that omits an
``evidence_requirements`` declaration — or asks for an evidence kind the
contract does not know — fails here by name rather than silently
evaluating against evidence the current scan view never carries.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pandas as pd
import pytest

from app.core.config import SKILLS_DIR
from app.services.backtest.market_view import PRICE_HISTORY_COLUMNS
from app.services.backtest.scan_view import CurrentScanMarketView
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    discover_strategies,
)
from app.services.backtest.strategy_evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceCompatibility,
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
    preflight_evidence,
    strategy_support_label,
)
from app.services.backtest.worker import _load_strategy_instance

SESSION = date(2026, 8, 28)
SECURITY = "AAA"
#: Comfortably above every first-party Strategy's own history guard.
EVIDENCED_SESSIONS = 400

#: Strategies whose rules read price evidence alone — the current OHLCV
#: scan view can drive their live recommendations.
HISTORY_ONLY_STRATEGIES = (
    "rtly-backtest-buy-and-hold",
    "rtly-backtest-darvas-box",
    "rtly-backtest-moving-average",
    "rtly-backtest-turtle-trend",
)

#: Strategies needing committed scan-detector evidence, keyed by the kinds
#: an OHLCV-only view must report as missing.
SCAN_DEPENDENT_STRATEGIES = {
    "rtly-backtest-weinstein": (EvidenceKind.SCAN_STAGE,),
    "rtly-backtest-minervini": (EvidenceKind.SCAN_STAGE, EvidenceKind.SCAN_VCP),
}


def _ohlcv_view(sessions: int = EVIDENCED_SESSIONS) -> CurrentScanMarketView:
    """Build an OHLCV-only current-scan view with ample evidenced history."""
    index = [
        SESSION - timedelta(days=sessions - 1 - offset) for offset in range(sessions)
    ]
    frame = pd.DataFrame(
        {
            name: [Decimal(100 + offset) for offset in range(sessions)]
            for name in PRICE_HISTORY_COLUMNS
        },
        index=pd.Index(index, dtype=object, name="session"),
        columns=list(PRICE_HISTORY_COLUMNS),
    )
    return CurrentScanMarketView(
        as_of_session=SESSION,
        selected_universe=(SECURITY,),
        _histories={SECURITY: frame},
    )


def _descriptors() -> tuple[StrategyDescriptorV1, ...]:
    return discover_strategies(SKILLS_DIR).strategies


DESCRIPTORS = _descriptors()

if not DESCRIPTORS:  # pragma: no cover - a checkout with no skills
    pytest.skip("no Strategies discovered in this checkout", allow_module_level=True)


def _requirements(descriptor: StrategyDescriptorV1) -> StrategyEvidenceRequirementsV1:
    """Load a real runtime and read its declaration for default parameters."""
    strategy: Any = _load_strategy_instance(SKILLS_DIR / descriptor.runtime_path)
    declare = getattr(strategy, "evidence_requirements", None)
    assert callable(declare), (
        f"Strategy {descriptor.strategy_id} does not declare "
        "evidence_requirements (evidence contract v1)"
    )
    parameters = dict(descriptor.default_parameters) | dict(
        descriptor.bind_universe((SECURITY,))
    )
    return cast(StrategyEvidenceRequirementsV1, declare(parameters))


@pytest.mark.parametrize(
    "descriptor", DESCRIPTORS, ids=[item.strategy_id for item in DESCRIPTORS]
)
def test_every_strategy_declares_a_valid_contract(
    descriptor: StrategyDescriptorV1,
) -> None:
    requirements = _requirements(descriptor)
    assert isinstance(requirements, StrategyEvidenceRequirementsV1)
    assert requirements.contract_version == EVIDENCE_CONTRACT_VERSION
    for requirement in requirements.entry + requirements.exit:
        assert isinstance(requirement.kind, EvidenceKind)
        assert requirement.minimum_sessions >= 0


@pytest.mark.parametrize("strategy_id", HISTORY_ONLY_STRATEGIES)
def test_history_only_strategies_are_compatible_with_the_ohlcv_view(
    strategy_id: str,
) -> None:
    descriptor = next(
        (item for item in DESCRIPTORS if item.strategy_id == strategy_id), None
    )
    if descriptor is None:
        pytest.skip(f"{strategy_id} not discovered in this checkout")
    preflight = preflight_evidence(
        _requirements(descriptor), _ohlcv_view(), (SECURITY,), (SECURITY,)
    )
    assert preflight.entry is EvidenceCompatibility.COMPATIBLE
    assert preflight.exit is EvidenceCompatibility.COMPATIBLE
    assert strategy_support_label(preflight) == "supported"


@pytest.mark.parametrize(
    ("strategy_id", "missing"), sorted(SCAN_DEPENDENT_STRATEGIES.items())
)
def test_scan_dependent_strategies_are_incompatible_with_the_ohlcv_view(
    strategy_id: str, missing: tuple[EvidenceKind, ...]
) -> None:
    descriptor = next(
        (item for item in DESCRIPTORS if item.strategy_id == strategy_id), None
    )
    if descriptor is None:
        pytest.skip(f"{strategy_id} not discovered in this checkout")
    preflight = preflight_evidence(
        _requirements(descriptor), _ohlcv_view(), (SECURITY,), (SECURITY,)
    )
    assert preflight.entry is EvidenceCompatibility.INCOMPATIBLE
    assert preflight.exit is EvidenceCompatibility.INCOMPATIBLE
    assert set(missing) <= set(preflight.entry_missing)
    assert set(missing) <= set(preflight.exit_missing)
    assert strategy_support_label(preflight) == "backtest_only"


def test_thin_history_degrades_rather_than_disqualifies() -> None:
    """A supported kind with too few sessions is degraded, not incompatible."""
    descriptor = next(
        (
            item
            for item in DESCRIPTORS
            if item.strategy_id == "rtly-backtest-moving-average"
        ),
        None,
    )
    if descriptor is None:
        pytest.skip("moving-average not discovered in this checkout")
    preflight = preflight_evidence(
        _requirements(descriptor), _ohlcv_view(sessions=10), (SECURITY,), (SECURITY,)
    )
    assert preflight.entry is EvidenceCompatibility.DEGRADED
    assert preflight.degraded_securities == (SECURITY,)
    assert preflight.entry_missing == ()
    assert strategy_support_label(preflight) == "degraded"


#: Buy and Hold is a passive benchmark: it holds forever and never emits an
#: ordinary exit, so an empty exit declaration is the honest answer. Every
#: other Strategy must declare both paths — an empty declaration would
#: otherwise preflight COMPATIBLE and quietly beat omitting the method.
EMPTY_EXIT_EXEMPT = ("rtly-backtest-buy-and-hold",)


@pytest.mark.parametrize(
    "descriptor", DESCRIPTORS, ids=[item.strategy_id for item in DESCRIPTORS]
)
def test_declarations_are_not_vacuous(descriptor: StrategyDescriptorV1) -> None:
    """An empty declaration must not be a cheaper route than omitting one."""
    requirements = _requirements(descriptor)
    assert requirements.entry, (
        f"{descriptor.strategy_id} declares no entry evidence requirements"
    )
    if descriptor.strategy_id in EMPTY_EXIT_EXEMPT:
        assert requirements.exit == (), (
            f"{descriptor.strategy_id} is listed as exit-exempt but declares "
            "exit requirements — remove it from EMPTY_EXIT_EXEMPT"
        )
        return
    assert requirements.exit, (
        f"{descriptor.strategy_id} declares no exit evidence requirements"
    )


def test_per_path_security_sets_are_independent() -> None:
    """A security only one path acts on never degrades the other path."""
    view = _ohlcv_view()
    thin = _ohlcv_view(sessions=2)
    requirements = StrategyEvidenceRequirementsV1(
        entry=(
            EvidenceRequirementV1(
                kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=100
            ),
        ),
        exit=(
            EvidenceRequirementV1(
                kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=100
            ),
        ),
    )
    # Entry sees the well-evidenced view; exit sees the thin one.
    entry_only = preflight_evidence(requirements, view, (SECURITY,), ())
    assert entry_only.entry is EvidenceCompatibility.COMPATIBLE
    assert entry_only.exit is EvidenceCompatibility.COMPATIBLE

    exit_only = preflight_evidence(requirements, thin, (), (SECURITY,))
    assert exit_only.entry is EvidenceCompatibility.COMPATIBLE
    assert exit_only.exit is EvidenceCompatibility.DEGRADED
    assert exit_only.degraded_securities == (SECURITY,)
