"""Position thesis schemas (GH-14): why a holding is held, and the checks on it.

A thesis is a versioned, user-confirmed statement per held security: text
plus 1–6 typed invalidation rules from a closed vocabulary (no free-text
rules). Evaluations are deterministic facts about one thesis version against
one published analysis run, so they carry no wall-clock time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.schemas.evidence_ref import EvidenceRefV1

MAX_RULES = 6
SmaPeriod = Literal[50, 150, 200]
ThesisTextSource = Literal["user", "ai_draft"]
ThesisStatus = Literal["confirmed", "weakened", "invalidated", "evidence_limited"]
RuleOutcome = Literal["fired", "clear", "limited"]
RULE_KINDS: tuple[str, ...] = (
    "close_below_stop",
    "close_below_sma",
    "stage_2_lost",
    "score_below",
)
SMA_PERIODS: tuple[int, ...] = (50, 150, 200)

STATUS_LABELS: dict[str, str] = {
    "confirmed": "Confirmed",
    "weakened": "Weakened",
    "invalidated": "Invalidated",
    "evidence_limited": "Evidence limited",
}

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CloseBelowStopRule(BaseModel):
    """Fires when the scan close is below the analysis stop loss."""

    model_config = _FROZEN
    kind: Literal["close_below_stop"] = "close_below_stop"


class CloseBelowSmaRule(BaseModel):
    """Fires when the close is below an SMA, optionally on high volume."""

    model_config = _FROZEN
    kind: Literal["close_below_sma"] = "close_below_sma"
    period: SmaPeriod
    min_rel_volume: float | None = Field(
        default=None, ge=1.0, le=10.0, allow_inf_nan=False
    )


class Stage2LostRule(BaseModel):
    """Fires when the analysis stage is no longer Stage 2."""

    model_config = _FROZEN
    kind: Literal["stage_2_lost"] = "stage_2_lost"


class ScoreBelowRule(BaseModel):
    """Fires when the analysis score drops below ``min_score``."""

    model_config = _FROZEN
    kind: Literal["score_below"] = "score_below"
    #: The score is 1–10, so a threshold of 1 could never fire.
    min_score: int = Field(ge=2, le=10)


ThesisRuleV1 = Annotated[
    CloseBelowStopRule | CloseBelowSmaRule | Stage2LostRule | ScoreBelowRule,
    Field(discriminator="kind"),
]
RULE_ADAPTER: TypeAdapter[ThesisRuleV1] = TypeAdapter(ThesisRuleV1)


def describe_rule(rule: ThesisRuleV1) -> str:
    """Return a plain-English description of one rule."""
    if isinstance(rule, CloseBelowStopRule):
        return "Close below the stop loss"
    if isinstance(rule, CloseBelowSmaRule):
        volume = (
            f" on relative volume of at least {rule.min_rel_volume:g}x"
            if rule.min_rel_volume is not None
            else ""
        )
        return f"Close below the {rule.period}-day SMA{volume}"
    if isinstance(rule, Stage2LostRule):
        return "Stage 2 lost"
    return f"Score below {rule.min_score}/10"


class ThesisDraftV1(BaseModel):
    """Thesis wording plus its typed rules — the shape an AI draft proposes."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    rationale: str = Field(min_length=1, max_length=2000)
    expected_setup: str = Field(min_length=1, max_length=1000)
    rules: tuple[ThesisRuleV1, ...] = Field(min_length=1, max_length=MAX_RULES)

    @field_validator("rules")
    @classmethod
    def _distinct_rules(
        cls, rules: tuple[ThesisRuleV1, ...]
    ) -> tuple[ThesisRuleV1, ...]:
        """Drop repeated rules (first kept), so none is checked or cited twice."""
        return tuple(dict.fromkeys(rules))


class ThesisContentV1(ThesisDraftV1):
    """What the user saves: a draft plus an optional review date."""

    review_date: date | None = None


class PositionThesisV1(ThesisContentV1):
    """One immutable thesis version row (only ``active`` ever flips)."""

    id: int
    portfolio_id: int
    security_id: str
    version: int
    text_source: ThesisTextSource
    active: bool
    confirmed_at: str | None = None
    created_at: str


class RuleResultV1(BaseModel):
    """One rule's outcome, citing the rule and the scan fields it read."""

    model_config = _FROZEN

    index: int
    rule: ThesisRuleV1
    outcome: RuleOutcome
    evidence: tuple[EvidenceRefV1, ...]
    observed: dict[str, float | int | str | None]

    @property
    def citation(self) -> str:
        """Name the rule, its evidence fields, session and run."""
        fields = ", ".join(ref.id for ref in self.evidence)
        first = self.evidence[0] if self.evidence else None
        session = first.as_of.isoformat() if first and first.as_of else "unknown"
        source = first.source if first else "no analysis run"
        return (
            f"rule {self.index} {self.rule.kind} ({fields}) · "
            f"session {session} · {source}"
        )


class ThesisEvaluationV1(BaseModel):
    """Deterministic facts for one thesis version against one analysis run."""

    model_config = _FROZEN

    thesis_id: int
    thesis_version: int
    security_id: str
    analysis_run_id: str
    session: date | None
    status: ThesisStatus
    results: tuple[RuleResultV1, ...]
    limitations: tuple[str, ...] = ()

    @property
    def first_fired(self) -> RuleResultV1 | None:
        """Return the first rule that fired, if any."""
        return next((r for r in self.results if r.outcome == "fired"), None)


@dataclass(frozen=True)
class ThesisSummary:
    """One holding's thesis state for the agent cell and the email."""

    active: PositionThesisV1 | None = None
    pending: PositionThesisV1 | None = None
    latest: ThesisEvaluationV1 | None = None
    review_due: bool = False
    #: ``latest`` was checked against the currently published analysis run.
    current: bool = False
