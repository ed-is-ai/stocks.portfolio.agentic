"""Generic Strategy evidence-capability contract (#471).

One provider-neutral authority answering a single question: *can this
Strategy's rules actually be evaluated against the evidence this market
view carries?* A Strategy declares, per path (``entry``/``exit``), the
evidence kinds and minimum bounded history its own guards require; a
market view declares which kinds it can structurally supply plus each
security's real coverage; :func:`preflight_evidence` compares the two.

The comparison is deliberately generic — it never branches on a
``strategy_id`` and never special-cases a provider. An undeclared
requirement set is *not* treated as "safe": the caller resolves it to a
typed unavailable state, so a Strategy whose evidence needs are unknown
can never silently emit a Sell (or silently emit nothing) from evidence
the view never had.

Runtime-facing by design: this module is on ``skill_discovery``'s approved
import allowlist so a Strategy's own ``scripts/strategy.py`` may declare
its requirements without reaching outside the sanctioned import graph. It
therefore imports nothing but the standard library, pydantic, and
``strategy_protocol``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import (
    Iterable,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import Field, model_validator

from app.services.backtest.strategy_protocol import (
    StrategyParameters,
    _StrategyModel,
)

#: The evidence contract generation this module implements. A declaration
#: naming any other version is rejected rather than reinterpreted.
EVIDENCE_CONTRACT_VERSION: int = 1


class EvidenceKind(StrEnum):
    """The closed vocabulary of evidence a Strategy may require.

    Each member names one *structural* capability of a market view, not a
    threshold: ``PRICE_HISTORY`` is bounded OHLCV, while the ``SCAN_*``
    members are the monthly-scan detector fragments a full historical
    ``MarketView`` pins but the current-scan adapter deliberately does not
    fabricate.
    """

    PRICE_HISTORY = "price_history"
    SCAN_STAGE = "scan_stage"
    SCAN_VCP = "scan_vcp"
    SCAN_TECHNICALS = "scan_technicals"


class EvidenceRequirementV1(_StrategyModel):
    """One evidence kind a Strategy path needs, with its thresholds.

    ``minimum_sessions`` is only meaningful for :attr:`EvidenceKind.
    PRICE_HISTORY` (the number of bounded sessions the rule's own guard
    demands); ``columns`` names the price columns the rule reads.
    """

    kind: EvidenceKind
    minimum_sessions: int = Field(default=0, ge=0)
    columns: tuple[str, ...] = ()


class StrategyEvidenceRequirementsV1(_StrategyModel):
    """A Strategy's complete per-path evidence declaration.

    Entry and exit are declared separately because they routinely differ:
    a Strategy may need a full 204-session trend window to *enter* and
    only 150 sessions to manage an existing position.
    """

    contract_version: int = EVIDENCE_CONTRACT_VERSION
    entry: tuple[EvidenceRequirementV1, ...] = ()
    exit: tuple[EvidenceRequirementV1, ...] = ()

    @model_validator(mode="after")
    def _validate_declaration(self) -> StrategyEvidenceRequirementsV1:
        """Reject an unsupported generation or a duplicated kind per path."""
        if self.contract_version != EVIDENCE_CONTRACT_VERSION:
            raise ValueError(
                f"unsupported evidence contract version {self.contract_version}; "
                f"this host implements v{EVIDENCE_CONTRACT_VERSION}"
            )
        for path, requirements in (("entry", self.entry), ("exit", self.exit)):
            kinds = [requirement.kind for requirement in requirements]
            if len(kinds) != len(set(kinds)):
                raise ValueError(f"{path} declares the same evidence kind twice")
        return self


class SecurityEvidenceCoverageV1(_StrategyModel):
    """What one market view actually holds for one security today."""

    security_id: str = Field(min_length=1)
    kinds: frozenset[EvidenceKind] = frozenset()
    sessions: int = Field(default=0, ge=0)
    columns: tuple[str, ...] = ()


class EvidenceCompatibility(StrEnum):
    """How well one path's requirements are met by a view's evidence."""

    COMPATIBLE = "compatible"
    DEGRADED = "degraded"
    INCOMPATIBLE = "incompatible"


class EvidencePreflightV1(_StrategyModel):
    """The typed outcome of comparing one declaration against one view.

    ``*_missing`` names structural capabilities the view cannot supply at
    all (an incompatible path); ``degraded_*`` maps a security id to the
    kinds that *are* structurally supported but are not evidenced well
    enough for that security (too few sessions, absent columns, or an
    absent scan fragment).
    """

    entry: EvidenceCompatibility
    exit: EvidenceCompatibility
    entry_missing: tuple[EvidenceKind, ...] = ()
    exit_missing: tuple[EvidenceKind, ...] = ()
    degraded_entry: Mapping[str, tuple[EvidenceKind, ...]] = {}
    degraded_exit: Mapping[str, tuple[EvidenceKind, ...]] = {}

    @property
    def entry_supported(self) -> bool:
        """True when the entry path may be invoked at all."""
        return self.entry is not EvidenceCompatibility.INCOMPATIBLE

    @property
    def exit_supported(self) -> bool:
        """True when the exit path may be invoked at all."""
        return self.exit is not EvidenceCompatibility.INCOMPATIBLE

    @property
    def complete(self) -> bool:
        """True only when both paths are fully compatible."""
        return (
            self.entry is EvidenceCompatibility.COMPATIBLE
            and self.exit is EvidenceCompatibility.COMPATIBLE
        )

    @property
    def degraded_securities(self) -> tuple[str, ...]:
        """Sorted ids degraded on either path — the diagnostics vocabulary."""
        return tuple(sorted(set(self.degraded_entry) | set(self.degraded_exit)))

    def entry_degraded_for(self, security_id: str) -> bool:
        """True when ``security_id`` lacks evidence the entry path needs."""
        return security_id in self.degraded_entry

    def exit_degraded_for(self, security_id: str) -> bool:
        """True when ``security_id`` lacks evidence the exit path needs."""
        return security_id in self.degraded_exit


@runtime_checkable
class EvidenceDeclaringStrategyV1(Protocol):
    """Optional Strategy capability: declare evidence needs per path.

    Mirrors ``InitialEntrySelectionProviderV1``'s optional-capability
    shape. The declaration is a *runtime* method rather than SKILL.md
    frontmatter because minimum history is parameter-derived.
    """

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1: ...


@runtime_checkable
class EvidenceCapableViewV1(Protocol):
    """Optional market-view capability: declare what it can evidence."""

    @property
    def evidence_capabilities(self) -> frozenset[EvidenceKind]:
        """The evidence kinds this view can structurally supply."""
        ...

    def evidence_coverage(self, security_id: str) -> SecurityEvidenceCoverageV1:
        """Return one security's actual coverage — never raises."""
        ...


def preflight_evidence(
    requirements: StrategyEvidenceRequirementsV1,
    view: EvidenceCapableViewV1,
    entry_securities: Iterable[str],
    exit_securities: Iterable[str],
) -> EvidencePreflightV1:
    """Compare a Strategy's declaration against a view's real evidence.

    Pure and generic: identical inputs always produce an identical
    outcome, and no Strategy identity is consulted. A required kind the
    view cannot supply at all makes that path
    :attr:`EvidenceCompatibility.INCOMPATIBLE` and is listed in the
    path's ``*_missing`` tuple. Otherwise each path is checked only
    against the securities it can actually act on — candidates for entry,
    holdings for exit — because a security one path never considers must
    never degrade the other. Any shortfall marks that security degraded
    for its path, and one degraded security makes the path
    :attr:`EvidenceCompatibility.DEGRADED`. An empty requirement tuple is
    always compatible.
    """
    coverage: dict[str, SecurityEvidenceCoverageV1] = {}

    def covered(securities: Iterable[str]) -> dict[str, SecurityEvidenceCoverageV1]:
        """Resolve one path's coverage, reading each security only once."""
        ordered = tuple(sorted(set(securities)))
        for security_id in ordered:
            if security_id not in coverage:
                coverage[security_id] = view.evidence_coverage(security_id)
        return {security_id: coverage[security_id] for security_id in ordered}

    entry_state, entry_missing, degraded_entry = _evaluate_path(
        requirements.entry, view.evidence_capabilities, covered(entry_securities)
    )
    exit_state, exit_missing, degraded_exit = _evaluate_path(
        requirements.exit, view.evidence_capabilities, covered(exit_securities)
    )
    return EvidencePreflightV1(
        entry=entry_state,
        exit=exit_state,
        entry_missing=entry_missing,
        exit_missing=exit_missing,
        degraded_entry=degraded_entry,
        degraded_exit=degraded_exit,
    )


def _evaluate_path(
    requirements: tuple[EvidenceRequirementV1, ...],
    capabilities: frozenset[EvidenceKind],
    coverage: Mapping[str, SecurityEvidenceCoverageV1],
) -> tuple[
    EvidenceCompatibility, tuple[EvidenceKind, ...], dict[str, tuple[EvidenceKind, ...]]
]:
    """Resolve one path's compatibility, missing kinds, and degraded ids."""
    if not requirements:
        return EvidenceCompatibility.COMPATIBLE, (), {}
    missing = tuple(
        requirement.kind
        for requirement in requirements
        if requirement.kind not in capabilities
    )
    if missing:
        return EvidenceCompatibility.INCOMPATIBLE, missing, {}
    degraded: dict[str, tuple[EvidenceKind, ...]] = {}
    for security_id, security_coverage in coverage.items():
        shortfalls = tuple(
            requirement.kind
            for requirement in requirements
            if _falls_short(requirement, security_coverage)
        )
        if shortfalls:
            degraded[security_id] = shortfalls
    if degraded:
        return EvidenceCompatibility.DEGRADED, (), degraded
    return EvidenceCompatibility.COMPATIBLE, (), {}


def _falls_short(
    requirement: EvidenceRequirementV1, coverage: SecurityEvidenceCoverageV1
) -> bool:
    """True when one security's coverage cannot satisfy one requirement."""
    if requirement.kind not in coverage.kinds:
        return True
    if coverage.sessions < requirement.minimum_sessions:
        return True
    return any(column not in coverage.columns for column in requirement.columns)


#: The three labels the assign-Strategy modal renders per choice.
StrategySupportLabel = Literal["supported", "degraded", "backtest_only"]


def strategy_support_label(preflight: EvidencePreflightV1) -> StrategySupportLabel:
    """Reduce a preflight to the badge the Strategy Manager shows.

    ``backtest_only`` means a path's evidence simply does not exist in the
    current view — the Strategy still backtests normally, it just cannot
    drive live portfolio recommendations.
    """
    if not (preflight.entry_supported and preflight.exit_supported):
        return "backtest_only"
    if preflight.complete:
        return "supported"
    return "degraded"


__all__ = [
    "EVIDENCE_CONTRACT_VERSION",
    "EvidenceCapableViewV1",
    "EvidenceCompatibility",
    "EvidenceDeclaringStrategyV1",
    "EvidenceKind",
    "EvidencePreflightV1",
    "EvidenceRequirementV1",
    "SecurityEvidenceCoverageV1",
    "StrategyEvidenceRequirementsV1",
    "StrategySupportLabel",
    "preflight_evidence",
    "strategy_support_label",
]
