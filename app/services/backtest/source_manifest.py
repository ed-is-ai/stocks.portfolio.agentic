"""Canonical runtime source and reconstruction-input replay identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from copy import deepcopy
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.backtest.canonical_manifest import (
    canonical_json as shared_canonical_json,
    manifest_digest,
)
from app.services.backtest.detectors import (
    DETECTOR_REGISTRY,
)
from app.services.backtest.historical_data_qualification import (
    CANONICALIZER_VERSION,
    REQUEST_CONTRACT_VERSION,
)
from app.services.backtest.historical_scan_record import (
    DETECTOR_IDS,
    FrozenDict,
    frozen_dict,
)


SOURCE_MANIFEST_VERSION = "source_manifest.v1"
RECONSTRUCTION_INPUT_MANIFEST_VERSION = "reconstruction_input_manifest.v1"
_EXCLUDED_PARTS = frozenset(
    {"tests", "test", "__pycache__", "logs", "_bmad-output", ".pytest_cache"}
)
_COMMON_ALLOWLIST = (
    "app/services/backtest/source_manifest.py",
    "app/services/backtest/historical_scan_record.py",
    "app/services/backtest/historical_scan_reconstruction.py",
    "app/services/backtest/observed_bau_record_builder.py",
    "app/services/backtest/detectors.py",
)
_DETECTOR_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "technical_indicators_v1": _COMMON_ALLOWLIST
    + ("app/core/technical_indicators.py",),
    "weinstein_stage_v1": _COMMON_ALLOWLIST
    + ("app/core/stage_classification.py", "app/core/technical_indicators.py"),
    "vcp_v1": _COMMON_ALLOWLIST
    + (
        "skills/vcp-screener/scripts/calculators/execution_state.py",
        "skills/vcp-screener/scripts/calculators/pivot_proximity_calculator.py",
        "skills/vcp-screener/scripts/calculators/trend_template_calculator.py",
        "skills/vcp-screener/scripts/calculators/vcp_pattern_calculator.py",
        "skills/vcp-screener/scripts/calculators/volume_pattern_calculator.py",
    ),
}
_YFINANCE_INGESTION_ALLOWLIST = (
    "app/agents/scanner/scanner_agent.py",
    "app/services/backtest/canonical_manifest.py",
    "app/services/backtest/historical_data_qualification.py",
    "app/services/backtest/historical_price_evidence.py",
    "app/services/backtest/bau_capture_coordinator.py",
    "app/services/backtest/bau_run_envelope.py",
    "app/services/backtest/source_manifest.py",
)


@dataclass(frozen=True)
class SourceManifestArtifact:
    _manifest: dict[str, Any]
    canonical_json: str
    digest: str

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a detached view so callers cannot invalidate this identity."""
        return deepcopy(self._manifest)


def _normalized_source_digest(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"runtime source is unreadable: {path.name}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _validated_relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or path.is_absolute()
        or ".." in path.parts
        or any(part in _EXCLUDED_PARTS for part in path.parts)
        or path.suffix in {".pyc", ".pyo"}
    ):
        raise ValueError(f"invalid runtime source path: {raw}")
    return path


def build_source_manifest(
    *,
    project_root: Path,
    producer_id: str,
    api_version: str,
    allowlist: tuple[str, ...],
    defaults: Mapping[str, object],
    python_runtime: str,
    dependency_versions: dict[str, str],
) -> SourceManifestArtifact:
    """Hash only one explicit runtime allowlist; imports are never traversed."""
    if not producer_id or not api_version or not allowlist:
        raise ValueError("source manifest identity and allowlist are required")
    if len(set(allowlist)) != len(allowlist):
        raise ValueError("runtime source allowlist contains duplicates")
    root = project_root.resolve()
    files: list[dict[str, str]] = []
    for raw in sorted(allowlist):
        relative = _validated_relative_path(raw)
        path = root.joinpath(*relative.parts)
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("runtime source escapes project root") from exc
        if not path.is_file():
            raise ValueError(f"allowlisted runtime source is missing: {raw}")
        files.append(
            {"path": relative.as_posix(), "sha256": _normalized_source_digest(path)}
        )
    manifest: dict[str, Any] = {
        "schema_version": SOURCE_MANIFEST_VERSION,
        "producer_kind": "detector_or_strategy",
        "producer_id": producer_id,
        "api_version": api_version,
        "python_runtime": python_runtime,
        "dependency_versions": dict(sorted(dependency_versions.items())),
        "defaults": dict(defaults),
        "files": files,
    }
    canonical = shared_canonical_json(manifest)
    return SourceManifestArtifact(manifest, canonical, manifest_digest(manifest))


def _installed_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError as exc:
        raise ValueError(f"runtime dependency is unavailable: {package}") from exc


@lru_cache(maxsize=None)
def detector_source_manifests(
    project_root: Path,
) -> Mapping[str, SourceManifestArtifact]:
    dependencies = {
        "pandas": _installed_version("pandas"),
        "pydantic": _installed_version("pydantic"),
    }
    runtime = f"{sys.version_info.major}.{sys.version_info.minor}"
    return cast(
        Mapping[str, SourceManifestArtifact],
        frozen_dict(
            {
                detector.detector_id: build_source_manifest(
                    project_root=project_root,
                    producer_id=detector.detector_id,
                    api_version=detector.detector_api_version,
                    allowlist=_DETECTOR_ALLOWLISTS[detector.detector_id],
                    defaults=detector.configuration,
                    python_runtime=runtime,
                    dependency_versions=dependencies,
                )
                for detector in DETECTOR_REGISTRY
            }
        ),
    )


@lru_cache(maxsize=None)
def yfinance_ingestion_source_manifest(project_root: Path) -> SourceManifestArtifact:
    """Return the locally authoritative yfinance ingestion implementation identity."""
    return build_source_manifest(
        project_root=project_root,
        producer_id="yfinance_historical_ingestion_v1",
        api_version="1",
        allowlist=_YFINANCE_INGESTION_ALLOWLIST,
        defaults={
            "canonicalizer_version": CANONICALIZER_VERSION,
            "request_contract_version": REQUEST_CONTRACT_VERSION,
        },
        python_runtime=f"{sys.version_info.major}.{sys.version_info.minor}",
        dependency_versions={
            "pandas": _installed_version("pandas"),
            "yfinance": _installed_version("yfinance"),
        },
    )


Digest = str


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DetectorInputIdentityV1(_ManifestModel):
    detector_id: Literal["technical_indicators_v1", "weinstein_stage_v1", "vcp_v1"]
    detector_api_version: str = Field(min_length=1)
    detector_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: dict[str, object]

    @field_validator("configuration")
    @classmethod
    def _immutable_configuration(cls, value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], FrozenDict(value))


class ReconstructionInputManifestV1(_ManifestModel):
    schema_version: Literal["reconstruction_input_manifest.v1"]
    security_id: str = Field(min_length=1)
    snapshot_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    as_of_session_date: date
    provider_data_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_start: date
    evidence_end: date
    provider_request_contract_version: str = Field(min_length=1)
    provider_evidence_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_plane_policy_version: str = Field(min_length=1)
    alias_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    roster_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_dataset_version: str = Field(min_length=1)
    calendar_dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    yfinance_ingestion_version: str = Field(min_length=1)
    record_schema_version: Literal["historical_scan_record.v1"]
    reconstructability_policy_version: Literal["reconstructability.v1"]
    detectors: tuple[DetectorInputIdentityV1, ...]

    @field_validator("detectors")
    @classmethod
    def _exact_detector_set(
        cls, value: tuple[DetectorInputIdentityV1, ...]
    ) -> tuple[DetectorInputIdentityV1, ...]:
        if {item.detector_id for item in value} != set(DETECTOR_IDS) or len(
            value
        ) != len(DETECTOR_IDS):
            raise ValueError("input manifest requires the exact detector set")
        return value

    def canonical_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="python", exclude={"detectors"})
        payload["detectors"] = [
            detector.model_dump(mode="python")
            for detector in sorted(self.detectors, key=lambda item: item.detector_id)
        ]
        return payload

    def canonical_json(self) -> str:
        return shared_canonical_json(self.canonical_payload())

    def digest(self) -> str:
        return manifest_digest(self.canonical_payload())

    @property
    def detector_versions(self) -> dict[str, str]:
        versions: dict[str, Any] = {
            str(detector.detector_id): detector.detector_version
            for detector in sorted(self.detectors, key=lambda item: item.detector_id)
        }
        return cast(
            dict[str, str],
            FrozenDict(versions),
        )


__all__ = [
    "DetectorInputIdentityV1",
    "RECONSTRUCTION_INPUT_MANIFEST_VERSION",
    "ReconstructionInputManifestV1",
    "SOURCE_MANIFEST_VERSION",
    "SourceManifestArtifact",
    "build_source_manifest",
    "detector_source_manifests",
    "yfinance_ingestion_source_manifest",
]
