"""Strategy-assignment service — the seam routes depend on (#440).

Routes never import skill discovery or repositories directly; they call this
service. Assignment is persistence + validation only: it never launches a
backtest, scan, email, or trade.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from app.core.config import ANALYSIS_JSON, SKILLS_DIR
from app.repositories.portfolio_strategies_repo import PortfolioStrategiesRepository
from app.schemas.analysis_artifact import read_analysis_artifact_meta
from app.schemas.strategy_assignment import (
    AssignmentView,
    ScanFreshness,
    StrategyAssignment,
)
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyDiscoveryWarningV1,
    discover_strategies,
)
from app.services.backtest.strategy_protocol import (
    JsonScalar,
    StrategyProtocolError,
    validate_strategy_parameters,
)

#: The published analysis artifact is stale once older than exactly this
#: much — the boundary itself (age == 24h) still counts as fresh.
SCAN_FRESHNESS_MAX_AGE = timedelta(hours=24)


class UnknownStrategyError(ValueError):
    """The requested ``strategy_id`` is not in the current discovery result."""


class IncompatibleStrategyError(ValueError):
    """A discovered Strategy's own default parameters failed validation."""


class StrategyAssignmentService:
    """Assign/clear/inspect one Strategy per portfolio, fail-soft throughout.

    ``skills_root`` and ``analysis_path`` default to the configured
    ``SKILLS_DIR``/``ANALYSIS_JSON`` and are overridable for tests.
    """

    def __init__(
        self,
        repo: PortfolioStrategiesRepository,
        skills_root: Path | None = None,
        analysis_path: Path | None = None,
    ) -> None:
        self._repo = repo
        self._skills_root = skills_root if skills_root is not None else SKILLS_DIR
        self._analysis_path = (
            analysis_path if analysis_path is not None else ANALYSIS_JSON
        )

    # --- discovery -------------------------------------------------------

    def _discovery(
        self,
    ) -> tuple[
        tuple[StrategyDescriptorV1, ...],
        tuple[StrategyDiscoveryWarningV1, ...],
    ]:
        """Run fail-soft discovery, returning (choices, warnings)."""
        result = discover_strategies(self._skills_root)
        return result.strategies, result.warnings

    def list_choices(self) -> tuple[StrategyDescriptorV1, ...]:
        """Return every currently discoverable Strategy descriptor."""
        return self._discovery()[0]

    def list_warnings(self) -> tuple[StrategyDiscoveryWarningV1, ...]:
        """Return discovery warnings — visible, never raised."""
        return self._discovery()[1]

    def _descriptor(self, strategy_id: str) -> StrategyDescriptorV1:
        """Resolve one descriptor by id, raising a domain error if unknown."""
        for descriptor in self.list_choices():
            if descriptor.strategy_id == strategy_id:
                return descriptor
        raise UnknownStrategyError(f"Unknown Strategy: {strategy_id!r}")

    # --- assignment lifecycle --------------------------------------------

    def assign(self, portfolio_id: int, strategy_id: str) -> StrategyAssignment:
        """Validate and store the portfolio's Strategy assignment.

        Stores the canonical snapshot of the descriptor's validated default
        parameters. Raises :class:`UnknownStrategyError` for an id absent
        from discovery (the stored assignment is untouched) and
        :class:`IncompatibleStrategyError` if a discovered Strategy's own
        defaults fail the shared validator.
        """
        descriptor = self._descriptor(strategy_id)
        try:
            validated = validate_strategy_parameters(
                descriptor.parameters,
                dict(descriptor.default_parameters),
                apply_defaults=False,
            )
        except StrategyProtocolError as exc:
            # A malformed skill schema is a discovery/validator defect;
            # surface it as the same domain error, never a 500 (#440).
            raise IncompatibleStrategyError(
                f"Strategy {strategy_id!r} has an invalid parameter schema: {exc}"
            ) from exc
        if isinstance(validated, tuple):
            raise IncompatibleStrategyError(
                f"Strategy {strategy_id!r} has invalid default parameters: "
                f"{validated[0].message}"
            )
        assignment = self._repo.upsert(
            portfolio_id,
            descriptor.strategy_id,
            # Defaults are scalars by schema construction; the validator's
            # wider JsonValue return is narrowed here.
            cast(Mapping[str, JsonScalar], dict(validated)),
        )
        return assignment

    def clear(self, portfolio_id: int) -> None:
        """Remove the portfolio's assignment; idempotent when none exists."""
        self._repo.clear(portfolio_id)

    def get_assignment(self, portfolio_id: int) -> AssignmentView | None:
        """Return the portfolio's assignment joined with discovery, or None."""
        assignment = self._repo.get(portfolio_id)
        return self.enrich(assignment) if assignment else None

    # --- views -----------------------------------------------------------

    def enrich(self, assignment: StrategyAssignment) -> AssignmentView:
        """Join a stored assignment against current discovery.

        A ``strategy_id`` missing from discovery is retained as
        ``available=False`` with ``display_name=None`` — never dropped.
        """
        for descriptor in self.list_choices():
            if descriptor.strategy_id == assignment.strategy_id:
                return AssignmentView(
                    assignment=assignment,
                    available=True,
                    display_name=descriptor.display_name,
                )
        return AssignmentView(assignment=assignment, available=False, display_name=None)

    def assignment_view(self, portfolio_id: int) -> AssignmentView | None:
        """Convenience combining ``get`` + ``enrich`` for one portfolio."""
        return self.get_assignment(portfolio_id)

    # --- freshness ---------------------------------------------------------

    def freshness(self) -> ScanFreshness:
        """Classify the published analysis artifact's age, never raising.

        Absent file → ``missing``; present but unparseable/legacy →
        ``unknown``; otherwise ``stale`` iff the artifact is strictly older
        than 24 hours (the exact 24h boundary is fresh).
        """
        if not self._analysis_path.exists():
            return "missing"
        meta = read_analysis_artifact_meta(self._analysis_path)
        if meta is None:
            return "unknown"
        age = datetime.now(UTC) - meta.generated_at
        if age < timedelta(0):
            # Clock skew: an artifact stamped in the future is not yet
            # trustworthy — treat it like an unparseable artifact.
            return "unknown"
        return "stale" if age > SCAN_FRESHNESS_MAX_AGE else "fresh"
