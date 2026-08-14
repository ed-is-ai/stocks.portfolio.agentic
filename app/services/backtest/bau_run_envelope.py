"""Durable, scanner-owned BAU evidence and per-run publication contracts.

This module deliberately has no dependency on scanner presentation models or
historical reconstruction requests.  An envelope is the only durable object a
later promotion may read; a capture file plus a completion marker is not an
authority boundary.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Literal, Mapping
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.historical_scan_record import CanonicalModel
from app.services.backtest.snapshot_profile import SnapshotProfileV1
from app.services.backtest.source_manifest import ReconstructionInputManifestV1
from app.services.backtest.trading_calendar import TradingCalendar


Digest = str
SnapshotMonth = str


class BauRunEnvelopeError(ValueError):
    """A BAU run artifact is malformed, incomplete, or cannot be published."""


class BauRawEvidenceV1(CanonicalModel):
    """The provider-native response obtained by the owning scanner run."""

    schema_version: Literal["bau_raw_evidence.v1"] = "bau_raw_evidence.v1"
    security_id: str = Field(min_length=1)
    alias_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["yfinance"]
    provider_version: str = Field(min_length=1)
    request_contract_version: str = Field(min_length=1)
    requested_symbol: str = Field(min_length=1)
    observed_symbol: str = Field(min_length=1)
    currency: Literal["USD", "GBP"]
    quote_unit: Literal["USD", "GBP", "GBp"]
    quote_unit_scale: str = Field(min_length=1)
    exchange_timezone: str = Field(min_length=1)
    start: date
    end: date
    request_contract: Mapping[str, object]
    rows: tuple[Mapping[str, object], ...]
    actions: tuple[Mapping[str, object], ...]
    response_metadata_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_manifest_json: str = Field(min_length=1)
    acquired_at: datetime

    @field_validator("acquired_at")
    @classmethod
    def _utc_acquired_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("raw evidence acquisition must be a UTC instant")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _provider_native_identity(self) -> "BauRawEvidenceV1":
        if self.start >= self.end or not self.rows:
            raise ValueError("raw evidence requires a non-empty request interval")
        if self.currency == "USD" and self.quote_unit != "USD":
            raise ValueError("USD evidence has an invalid quote unit")
        if self.currency == "GBP" and self.quote_unit not in {"GBP", "GBp"}:
            raise ValueError("GBP evidence has an invalid quote unit")
        try:
            manifest = json.loads(self.canonical_manifest_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("raw evidence manifest is invalid") from exc
        if manifest_digest(manifest) != self.data_revision:
            raise ValueError("raw evidence revision does not match its manifest")
        if (
            canonical_json(manifest) != self.canonical_manifest_json
            or manifest.get("provider") != self.provider
            or manifest.get("provider_version") != self.provider_version
            or manifest.get("request_contract_version") != self.request_contract_version
            or manifest.get("security_id") != self.security_id
            or manifest.get("alias_revision") != self.alias_revision
            or manifest.get("request") != dict(self.request_contract)
            or manifest.get("requested_symbol") != self.requested_symbol
            or manifest.get("observed_symbol") != self.observed_symbol
            or manifest.get("currency") != self.currency
            or manifest.get("quote_unit") != self.quote_unit
            or manifest.get("quote_unit_scale") != self.quote_unit_scale
            or manifest.get("exchange_timezone") != self.exchange_timezone
            or manifest.get("rows") != list(self.rows)
            or manifest.get("actions") != list(self.actions)
        ):
            raise ValueError("raw evidence fields do not match its manifest")
        return self

    @classmethod
    def from_historical_payload(cls, payload: object) -> "BauRawEvidenceV1":
        """Copy a just-fetched provider payload before any scanner conversion."""
        fields = (
            "security_id",
            "alias_revision",
            "provider",
            "provider_version",
            "request_contract_version",
            "requested_symbol",
            "observed_symbol",
            "currency",
            "quote_unit",
            "quote_unit_scale",
            "exchange_timezone",
            "start",
            "end",
            "request_contract",
            "rows",
            "actions",
            "response_metadata_digest",
            "data_revision",
            "canonical_manifest_json",
            "acquired_at",
        )
        try:
            values = {field: getattr(payload, field) for field in fields}
        except AttributeError as exc:
            raise BauRunEnvelopeError(
                "provider payload lacks BAU evidence fields"
            ) from exc
        values["start"] = date.fromisoformat(str(values["start"]))
        values["end"] = date.fromisoformat(str(values["end"]))
        values["acquired_at"] = datetime.fromisoformat(str(values["acquired_at"]))
        return cls.model_validate(values)


class BauCaptureMemberV1(CanonicalModel):
    """Resolved roster/session authority plus exactly one raw response."""

    schema_version: Literal["bau_capture_member.v1"] = "bau_capture_member.v1"
    security_id: str = Field(min_length=1)
    mic: Literal["XNAS", "XNYS", "XLON"]
    canonical_session: date
    source_cutoff: date
    alias_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest: ReconstructionInputManifestV1
    raw_evidence: BauRawEvidenceV1

    @model_validator(mode="after")
    def _member_facts_match_evidence(self) -> "BauCaptureMemberV1":
        if self.security_id != self.raw_evidence.security_id:
            raise ValueError("raw evidence security does not match capture member")
        if self.alias_revision != self.raw_evidence.alias_revision:
            raise ValueError("raw evidence alias does not match capture member")
        if self.source_cutoff != self.canonical_session:
            raise ValueError("capture source cutoff must equal canonical session")
        manifest = self.input_manifest
        if (
            manifest.security_id != self.security_id
            or manifest.as_of_session_date != self.canonical_session
            or manifest.provider_data_revision != self.raw_evidence.data_revision
            or manifest.provider_evidence_manifest_digest
            != self.raw_evidence.data_revision
            or manifest.alias_revision != self.alias_revision
        ):
            raise ValueError("capture input manifest does not bind raw evidence")
        return self


class BauSnapshotCaptureV1(CanonicalModel):
    """A complete roster-sized observation, suitable for observed-only build."""

    schema_version: Literal["bau_snapshot_capture.v1"] = "bau_snapshot_capture.v1"
    source_run_id: str = Field(min_length=1)
    snapshot_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    profile: SnapshotProfileV1
    roster_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    roster_captured_at: datetime
    captured_at: datetime
    members: tuple[BauCaptureMemberV1, ...]

    @field_validator("captured_at", "roster_captured_at")
    @classmethod
    def _utc_capture_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("BAU capture time must be a UTC instant")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _complete_canonical_month(self) -> "BauSnapshotCaptureV1":
        if self.roster_digest != self.profile.roster_digest:
            raise ValueError("capture roster does not match profile")
        if self.roster_captured_at > self.captured_at:
            raise ValueError("roster cannot be captured after BAU observation")
        ordered = tuple(sorted(self.members, key=lambda item: item.security_id))
        if (
            not ordered
            or ordered != self.members
            or len({x.security_id for x in ordered}) != len(ordered)
        ):
            raise ValueError("capture members must be complete, unique, and ordered")
        calendar = TradingCalendar()
        for member in ordered:
            if member.canonical_session.strftime("%Y-%m") != self.snapshot_month:
                raise ValueError("capture session is outside snapshot month")
            if member.canonical_session != calendar.last_session_of_month(
                member.mic, self.snapshot_month
            ):
                raise ValueError("capture session is not canonical month end")
            if member.raw_evidence.acquired_at > self.captured_at:
                raise ValueError("evidence cannot be acquired after capture completion")
        return self

    @property
    def capture_digest(self) -> Digest:
        return self.digest()


class BauRunEnvelopeV1(CanonicalModel):
    """One immutable, versioned authority artifact for a scanner run."""

    schema_version: Literal["bau_run_envelope.v1"] = "bau_run_envelope.v1"
    run_id: str = Field(min_length=1)
    outcome: Literal["pending", "successful", "failed"]
    analysis_payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_at: datetime
    completed_at: datetime | None = None
    completion_state: Literal["prepared", "completed", "failed"]
    capture: BauSnapshotCaptureV1 | None = None
    capture_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("prepared_at", "completed_at")
    @classmethod
    def _utc_completion(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("envelope completion must be a UTC instant")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _single_complete_authority(self) -> "BauRunEnvelopeV1":
        if self.completion_state == "prepared":
            if (
                self.outcome != "pending"
                or self.completed_at is not None
                or self.capture is None
                or self.capture_digest is None
            ):
                raise ValueError("prepared envelope has an invalid state")
        elif self.completion_state == "completed":
            if self.outcome != "successful" or self.completed_at is None:
                raise ValueError("completed envelope has an invalid outcome")
        elif (
            self.outcome != "failed"
            or self.completed_at is None
            or self.capture is not None
            or self.capture_digest is not None
        ):
            raise ValueError("failed envelope has an invalid state")
        if (self.capture is None) != (self.capture_digest is None):
            raise ValueError("completed capture must include its digest")
        if self.capture is not None:
            if self.capture.source_run_id != self.run_id:
                raise ValueError("capture is owned by another run")
            if self.capture.capture_digest != self.capture_digest:
                raise ValueError("capture digest does not match capture")
            boundary = self.completed_at or self.prepared_at
            if self.capture.captured_at > boundary:
                raise ValueError("capture completed after run envelope")
        return self


class BauRunEnvelopeStore:
    """Atomically publish and reload the one authoritative run envelope."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, run_id: str) -> Path:
        try:
            UUID(run_id)
        except ValueError as exc:
            raise BauRunEnvelopeError("run ID is not a UUID") from exc
        return self._directory / f"{run_id}.json"

    def publish(self, envelope: BauRunEnvelopeV1) -> Path:
        path = self.path_for(envelope.run_id)
        self._directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = self.load(envelope.run_id)
            if existing != envelope:
                raise BauRunEnvelopeError("run envelope is immutable")
            return path
        fd, temporary_name = tempfile.mkstemp(
            dir=self._directory, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(envelope.canonical_json_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.load(envelope.run_id)
                if existing != envelope:
                    raise BauRunEnvelopeError("run envelope is immutable")
            _fsync_directory(self._directory)
        except OSError as exc:
            raise BauRunEnvelopeError("could not publish BAU run envelope") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def prepare(self, envelope: BauRunEnvelopeV1) -> Path:
        if envelope.completion_state != "prepared":
            raise BauRunEnvelopeError("only a prepared envelope may be staged")
        return self.publish(envelope)

    def complete(self, run_id: str, *, completed_at: datetime) -> BauRunEnvelopeV1:
        """Atomically transition the one prepared envelope to immutable success."""
        prepared = self.load(run_id)
        if prepared.completion_state == "completed":
            return prepared
        if prepared.completion_state != "prepared":
            raise BauRunEnvelopeError("run envelope cannot be completed")
        completed = prepared.model_copy(
            update={
                "outcome": "successful",
                "completion_state": "completed",
                "completed_at": completed_at,
            }
        )
        completed = BauRunEnvelopeV1.model_validate(completed.model_dump())
        path = self.path_for(run_id)
        fd, temporary_name = tempfile.mkstemp(
            dir=self._directory, prefix=f".{path.name}.complete.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(completed.canonical_json_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            if self.load(run_id) != prepared:
                raise BauRunEnvelopeError("prepared envelope changed concurrently")
            os.replace(temporary, path)
            _fsync_directory(self._directory)
        except OSError as exc:
            raise BauRunEnvelopeError("could not complete BAU run envelope") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return completed

    def fail(self, run_id: str, *, completed_at: datetime) -> BauRunEnvelopeV1:
        """Close a prepared envelope without retaining promotable capture."""
        prepared = self.load(run_id)
        if prepared.completion_state != "prepared":
            return prepared
        failed = BauRunEnvelopeV1.model_validate(
            prepared.model_copy(
                update={
                    "outcome": "failed",
                    "completion_state": "failed",
                    "completed_at": completed_at,
                    "capture": None,
                    "capture_digest": None,
                }
            ).model_dump()
        )
        path = self.path_for(run_id)
        fd, temporary_name = tempfile.mkstemp(
            dir=self._directory, prefix=f".{path.name}.failed.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(failed.canonical_json_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            if self.load(run_id) != prepared:
                raise BauRunEnvelopeError("prepared envelope changed concurrently")
            os.replace(temporary, path)
            _fsync_directory(self._directory)
        except OSError as exc:
            raise BauRunEnvelopeError("could not fail BAU run envelope") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return failed

    def load(self, run_id: str) -> BauRunEnvelopeV1:
        path = self.path_for(run_id)
        try:
            return BauRunEnvelopeV1.from_canonical_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise BauRunEnvelopeError(
                "BAU run envelope is unavailable or invalid"
            ) from exc

    def has_completed_capture(self, profile_hash: str, snapshot_month: str) -> bool:
        """Whether an earlier immutable envelope already owns this month."""
        if not self._directory.exists():
            return False
        for path in self._directory.glob("*.json"):
            try:
                envelope = BauRunEnvelopeV1.from_canonical_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            capture = envelope.capture
            if (
                envelope.outcome == "successful"
                and envelope.completion_state == "completed"
                and capture is not None
                and capture.profile.profile_hash == profile_hash
                and capture.snapshot_month == snapshot_month
            ):
                return True
        return False

    def completed_capture_run_ids(self) -> tuple[str, ...]:
        """List valid completed capture envelopes for durable replay."""
        if not self._directory.exists():
            return ()
        run_ids: list[str] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                envelope = BauRunEnvelopeV1.from_canonical_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if (
                envelope.outcome == "successful"
                and envelope.completion_state == "completed"
                and envelope.capture is not None
            ):
                run_ids.append(envelope.run_id)
        return tuple(run_ids)


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes where the platform supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "BauCaptureMemberV1",
    "BauRawEvidenceV1",
    "BauRunEnvelopeError",
    "BauRunEnvelopeStore",
    "BauRunEnvelopeV1",
    "BauSnapshotCaptureV1",
]
