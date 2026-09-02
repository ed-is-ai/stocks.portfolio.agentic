"""Frozen recommendation models — the screen/email parity contract (#441).

``RecommendationResultV1`` is the single typed contract both the
recommendations screen (#441) and the per-portfolio daily email (#442)
consume: the screen renders it and the email must never recalculate the
action rules. ``NoAssignment``/``EvaluationUnavailable`` are the typed
non-result states ``PortfolioRecommendationService.recommend()`` returns
so a route can always render an actionable partial, never a 500.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.strategy_assignment import ScanFreshness

#: The closed action vocabulary, in the screen's fixed group order.
RecommendationAction = Literal["sell", "hold", "buy"]

#: The closed per-path evidence vocabulary: the backtest package's
#: ``EvidenceCompatibility`` values as plain strings, so this schema stays
#: independent of that package while still rejecting an unknown state.
EvidenceState = Literal["compatible", "degraded", "incompatible"]


class _RecommendationModel(BaseModel):
    """Frozen, strict, extra-forbidding base for recommendation models.

    Mirrors ``strategy_protocol._StrategyModel``'s immutability convention
    so a rendered screen or sent email can never observe a mutated result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RecommendationReasonV1(_RecommendationModel):
    """One already-formatted Strategy reason for one recommendation (#472).

    The plain-text projection of a Strategy's own
    ``SignalReasonV1``: a stable ``code``, its ``summary`` sentence, and
    its facts rendered by the shared ``strategy_explanation`` formatter.
    Screen, HTML email, and plain-text email all render these same rows,
    so no host ever re-derives wording from a ``strategy_id``.
    """

    code: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    facts: tuple[str, ...] = ()


class RecommendationV1(_RecommendationModel):
    """One actionable recommendation row for one security."""

    action: RecommendationAction
    ticker: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_warnings: tuple[str, ...] = ()
    #: The assigned Strategy's own structured explanation (#472), empty
    #: for host-generated hold/fail-safe rows.
    explanation: tuple[RecommendationReasonV1, ...] = ()


class EvaluationCoverageV1(_RecommendationModel):
    """Typed evidence diagnostics for one evaluation (#471).

    Makes an empty group honest: ``Buy 0`` on a complete evaluation means
    "the Strategy saw everything it needs and found nothing", while
    ``Buy 0`` with ``entry_state`` other than ``compatible`` means the
    Strategy could not be asked. States use the closed
    :data:`EvidenceState` vocabulary, so an unknown state is rejected at
    construction rather than silently reading as "supported".
    """

    entry_state: EvidenceState = "compatible"
    exit_state: EvidenceState = "compatible"
    entry_missing_evidence: tuple[str, ...] = ()
    exit_missing_evidence: tuple[str, ...] = ()
    #: How many securities the preflight *considered* across both paths —
    #: entry candidates plus holdings — not how many were successfully
    #: evaluated: a degraded security is counted here and skipped there.
    evaluated_securities: int = 0
    degraded_securities: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True only when both paths were fully evidenced."""
        return self.entry_state == "compatible" and self.exit_state == "compatible"

    @property
    def entry_supported(self) -> bool:
        """True when the entry path could be evaluated at all."""
        return self.entry_state != "incompatible"

    @property
    def exit_supported(self) -> bool:
        """True when the exit path could be evaluated at all."""
        return self.exit_state != "incompatible"


#: The default fully-evidenced coverage — a result constructed without
#: explicit diagnostics reads as a complete evaluation, exactly as before.
COMPLETE_COVERAGE = EvaluationCoverageV1()


class RecommendationResultV1(_RecommendationModel):
    """One complete, deterministic evaluation of a portfolio's Strategy.

    ``parameters`` is the stored assignment snapshot merged with the
    host-bound universe parameter (``bind_universe``), so the exact inputs
    the Strategy saw are auditable from the result alone.
    """

    portfolio_id: int
    analysis_run_id: str = Field(min_length=1)
    generated_at: datetime
    market_session: date
    freshness: ScanFreshness
    strategy_id: str = Field(min_length=1)
    strategy_source_digest: str = Field(min_length=1)
    # ``Any`` rather than the protocol's recursive ``JsonValue`` alias —
    # pydantic cannot build a schema for that implicit recursion here, and
    # the snapshot is already validated by ``validate_strategy_parameters``.
    parameters: Mapping[str, Any]
    recommendations: tuple[RecommendationV1, ...]
    #: Tickers that could not be resolved against the scan/alias evidence
    #: (ambiguous aliases, duplicate canonical ids, stale-evidence
    #: securities) — surfaced, never silently dropped.
    unresolved: tuple[str, ...] = ()
    #: Evidence diagnostics — distinguishes "no signals" from "could not
    #: ask the Strategy because the evidence was incomplete" (#471).
    coverage: EvaluationCoverageV1 = COMPLETE_COVERAGE
    evaluated_at: datetime

    @field_validator("generated_at", "evaluated_at")
    @classmethod
    def _require_aware_timestamps(cls, value: datetime) -> datetime:
        """Reject naive timestamps so freshness math never silently misfires."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value


class NoAssignment(_RecommendationModel):
    """Typed 'no Strategy assigned' outcome — the generic experience."""


#: Singleton-style shared instance — the state carries no data.
NO_ASSIGNMENT = NoAssignment()


class EvaluationUnavailable(_RecommendationModel):
    """Typed 'could not evaluate' outcome, with an actionable reason.

    ``freshness`` rides along when it is already known (e.g. a missing or
    metadata-less artifact) so the screen can still show the non-blocking
    freshness warning alongside the failure alert.
    """

    reason: str = Field(min_length=1)
    freshness: ScanFreshness | None = None
