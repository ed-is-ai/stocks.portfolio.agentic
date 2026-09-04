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

from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.backtest.historical_scan_record import (
    DETECTOR_IDS,
    DetectorFragmentEnvelopeV1,
)


class _FrozenArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CurrentEvidenceSuccessV1(_FrozenArtifactModel):
    """One complete, indivisible current detector result set."""

    schema_version: Literal["current_scan_evidence.v1"]
    security_id: str = Field(min_length=1)
    as_of_session: date
    input_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    fragments: tuple[DetectorFragmentEnvelopeV1, ...]

    @model_validator(mode="after")
    def _complete_bound_suite(self) -> "CurrentEvidenceSuccessV1":
        if tuple(item.detector for item in self.fragments) != DETECTOR_IDS:
            raise ValueError("current evidence requires the ordered detector set")
        if any(
            item.security_id != self.security_id
            or item.date != self.as_of_session
            or item.input_revision != self.input_revision
            for item in self.fragments
        ):
            raise ValueError("current evidence fragments are not consistently bound")
        return self


class CurrentEvidenceGapV1(_FrozenArtifactModel):
    """Typed, per-security reason that canonical scan evidence is unavailable."""

    schema_version: Literal["current_scan_evidence_gap.v1"]
    security_id: str = Field(min_length=1)
    as_of_session: date | None = None
    reason: Literal[
        "insufficient_history",
        "incomplete_history",
        "malformed_history",
        "stale_session",
        "detector_failure",
        "identity_conflict",
    ]
    detail: str = Field(min_length=1)


CurrentEvidenceEntryV1 = Annotated[
    CurrentEvidenceSuccessV1 | CurrentEvidenceGapV1,
    Field(discriminator="schema_version"),
]


class CurrentAnalysisEvidenceV1(_FrozenArtifactModel):
    """Run-owned current evidence published with the presentation records."""

    schema_version: Literal["current_analysis_evidence.v1"]
    run_id: str = Field(min_length=1)
    as_of_session: date
    entries: tuple[CurrentEvidenceEntryV1, ...]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _digest_payload(run_id: str, as_of_session: date, entries: tuple) -> str:
        payload = {
            "schema_version": "current_analysis_evidence.v1",
            "run_id": run_id,
            "as_of_session": as_of_session.isoformat(),
            "entries": [item.model_dump(mode="json") for item in entries],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        as_of_session: date,
        entries: tuple[CurrentEvidenceEntryV1, ...],
    ) -> "CurrentAnalysisEvidenceV1":
        ordered = tuple(sorted(entries, key=lambda item: item.security_id))
        return cls(
            schema_version="current_analysis_evidence.v1",
            run_id=run_id,
            as_of_session=as_of_session,
            entries=ordered,
            content_digest=cls._digest_payload(run_id, as_of_session, ordered),
        )

    @model_validator(mode="after")
    def _canonical_run(self) -> "CurrentAnalysisEvidenceV1":
        identities = tuple(item.security_id for item in self.entries)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("current evidence identities must be unique and ordered")
        if any(
            isinstance(item, CurrentEvidenceSuccessV1)
            and item.as_of_session != self.as_of_session
            for item in self.entries
        ):
            raise ValueError(
                "current evidence success is stale for the artifact session"
            )
        if self.content_digest != self._digest_payload(
            self.run_id, self.as_of_session, self.entries
        ):
            raise ValueError("current evidence digest does not match its content")
        return self


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
    current_evidence: CurrentAnalysisEvidenceV1 | None = None

    @model_validator(mode="after")
    def _evidence_owned_by_artifact(self) -> "AnalysisArtifact":
        if self.current_evidence and self.current_evidence.run_id != self.meta.run_id:
            raise ValueError("current evidence belongs to another run")
        return self


def build_analysis_payload(
    records: list[dict[str, Any]],
    *,
    run_id: str,
    generated_at: datetime,
    current_evidence: CurrentAnalysisEvidenceV1 | None = None,
) -> dict[str, Any]:
    """Return the JSON-ready payload for the analysis artifact promotion.

    Embedding ``meta`` alongside ``records`` in one dict means the single
    ``os.replace`` that publishes this payload is also what publishes its
    ownership — see the module docstring for why that matters.
    """
    artifact = AnalysisArtifact(
        meta=AnalysisArtifactMeta(run_id=run_id, generated_at=generated_at),
        records=records,
        current_evidence=current_evidence,
    )
    return artifact.model_dump(mode="json")


def read_analysis_artifact(path: Path) -> AnalysisArtifact | None:
    """Read one coherent filesystem snapshot; legacy bare lists have no envelope."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return AnalysisArtifact.model_validate(payload)
    except Exception:
        return None


def read_analysis_artifact_meta(path: Path) -> AnalysisArtifactMeta | None:
    """Return the embedded ownership metadata for *path*, or None if absent.

    Legacy artifacts written before this envelope existed (a bare JSON list)
    and unreadable/corrupt files both return None — callers must treat that
    as an unknown owner (freshness "unknown") rather than guessing at a
    timestamp. Reads only the ``meta`` sub-object, independent of
    ``current_evidence`` or any other section — freshness must stay
    readable even when a corrupt or incompatible evidence block would make
    ``read_analysis_artifact``'s full-model validation reject the file.
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
