"""Research Copilot answer schemas (GH-13).

``CopilotDraft`` is Claude's raw structured output: citations are bare ids
it claims to rest on. ``CopilotAnswerV1`` is what the panel renders, after
unknown ids are dropped and each kept id is resolved to an
``EvidenceRefV1`` locally — the model never supplies provenance itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.evidence_ref import EvidenceRefV1


class CopilotDraft(BaseModel):
    """Claude's unvalidated answer, parsed from the structured output."""

    answer: str
    citations: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class CopilotAnswerV1(BaseModel):
    """A cited, de-anonymised answer about one security."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str
    citations: list[EvidenceRefV1] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    analysis_run_id: str
    model_id: str
