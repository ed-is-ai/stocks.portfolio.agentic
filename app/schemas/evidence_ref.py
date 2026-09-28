"""Shared pointer to one piece of evidence an explanation rests on (GH-13).

Deliberately tiny and frozen: the Research Copilot (#13) and the follow-up
explanation surfaces (#14–#18) cite evidence by these refs, so a citation
names what kind of fact it is, when it was true and where it came from —
never the fact's content, which stays with the local evidence builder.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class EvidenceRefV1(BaseModel):
    """A citable evidence item: ``E1``-style id plus its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    id: str
    as_of: date | None = None
    source: str
