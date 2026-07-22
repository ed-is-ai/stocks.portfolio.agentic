"""Self-describing envelope for the published analysis artifact.

The dashboard's freshness display (#42) must never disagree with the data it
is describing. Earlier work tracked "did this run publish an artifact?" as a
separate flag on the persisted pipeline-status record, written *after* the
artifact file was atomically promoted with ``os.replace``. That left a crash
window between the two writes: if the process died after the rename but
before the flag was recorded, the file on disk legitimately belonged to the
new run while every consumer of "last usable run" kept reporting the
previous run's (older) timestamp — a silent mismatch between what was shown
and what was true.

The fix here removes the second write entirely. Run ownership and the
generation timestamp are embedded *inside* the artifact payload itself, so
the single ``os.replace`` that publishes the file is also the single atomic
operation that publishes its ownership metadata — there is no window where
one fact can be true without the other.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AnalysisArtifactMeta(BaseModel):
    """Ownership metadata embedded in the promoted analysis artifact."""

    run_id: str
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Reject naive timestamps so freshness math never silently misfires."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value


class AnalysisArtifact(BaseModel):
    """The full artifact payload: ownership metadata plus analysis records."""

    meta: AnalysisArtifactMeta
    records: list[dict[str, Any]] = Field(default_factory=list)


def build_analysis_payload(
    records: list[dict[str, Any]], *, run_id: str, generated_at: datetime
) -> dict[str, Any]:
    """Return the JSON-ready payload for the analysis artifact promotion.

    Embedding ``meta`` alongside ``records`` in one dict means the single
    ``os.replace`` that publishes this payload is also what publishes its
    ownership — see the module docstring for why that matters.
    """
    artifact = AnalysisArtifact(
        meta=AnalysisArtifactMeta(run_id=run_id, generated_at=generated_at),
        records=records,
    )
    return artifact.model_dump(mode="json")


def read_analysis_artifact_meta(path: Path) -> AnalysisArtifactMeta | None:
    """Return the embedded ownership metadata for *path*, or None if absent.

    Legacy artifacts written before this envelope existed (a bare JSON list)
    and unreadable/corrupt files both return None — callers must treat that
    as an unknown owner (freshness "unknown") rather than guessing at a
    timestamp.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    try:
        return AnalysisArtifactMeta.model_validate(meta)
    except Exception:
        return None


def read_analysis_records(path: Path) -> list[dict[str, Any]]:
    """Return the analysis records from *path*, supporting legacy bare lists."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    return []
