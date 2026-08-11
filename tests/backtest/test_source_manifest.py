from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.source_manifest import (
    DetectorInputIdentityV1,
    ReconstructionInputManifestV1,
    build_source_manifest,
    detector_source_manifests,
    yfinance_ingestion_source_manifest,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def test_source_manifest_normalizes_newlines_and_ignores_non_allowlisted_files(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "app" / "runtime.py"
    runtime.parent.mkdir()
    runtime.write_bytes(b"line1\r\nline2\r\n")
    ignored = tmp_path / "tests" / "test_runtime.py"
    ignored.parent.mkdir()
    ignored.write_text("first")

    first = build_source_manifest(
        project_root=tmp_path,
        producer_id="example_v1",
        api_version="1",
        allowlist=("app/runtime.py",),
        defaults={"window": 252},
        python_runtime="3.14",
        dependency_versions={"pydantic": "2.13.4"},
    )
    runtime.write_bytes(b"line1\nline2\n")
    ignored.write_text("changed")
    second = build_source_manifest(
        project_root=tmp_path,
        producer_id="example_v1",
        api_version="1",
        allowlist=("app/runtime.py",),
        defaults={"window": 252},
        python_runtime="3.14",
        dependency_versions={"pydantic": "2.13.4"},
    )

    assert second.digest == first.digest
    assert first.manifest["files"] == [
        {"path": "app/runtime.py", "sha256": first.manifest["files"][0]["sha256"]}
    ]


@pytest.mark.parametrize(
    "allowlist",
    [
        ("tests/test_runtime.py",),
        ("app/runtime.py", "app/runtime.py"),
        ("../outside.py",),
        ("app/missing.py",),
    ],
)
def test_source_manifest_rejects_invalid_or_nonruntime_allowlists(
    tmp_path: Path, allowlist: tuple[str, ...]
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "runtime.py").write_text("runtime")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_runtime.py").write_text("test")
    with pytest.raises(ValueError):
        build_source_manifest(
            project_root=tmp_path,
            producer_id="example_v1",
            api_version="1",
            allowlist=allowlist,
            defaults={},
            python_runtime="3.14",
            dependency_versions={},
        )


def _input_manifest(
    detectors: tuple[DetectorInputIdentityV1, ...],
) -> ReconstructionInputManifestV1:
    return ReconstructionInputManifestV1(
        schema_version="reconstruction_input_manifest.v1",
        security_id="sec-001",
        snapshot_month="2026-06",
        as_of_session_date=date(2026, 6, 30),
        provider_data_revision=DIGEST_A,
        evidence_start=date(2025, 6, 1),
        evidence_end=date(2026, 7, 1),
        provider_request_contract_version="YFinanceDailyProviderNativeV1",
        provider_evidence_manifest_digest=DIGEST_B,
        market_plane_policy_version="HistoricalMarketPlanesV1",
        alias_revision=DIGEST_C,
        roster_digest=DIGEST_A,
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=DIGEST_B,
        yfinance_ingestion_version="ingestion-v1",
        record_schema_version="historical_scan_record.v1",
        reconstructability_policy_version="reconstructability.v1",
        detectors=detectors,
    )


def test_reconstruction_input_digest_sorts_detectors_and_changes_materially() -> None:
    detectors = (
        DetectorInputIdentityV1(
            detector_id="technical_indicators_v1",
            detector_api_version="1",
            detector_version=DIGEST_A,
            configuration={"window": 252},
        ),
        DetectorInputIdentityV1(
            detector_id="weinstein_stage_v1",
            detector_api_version="1",
            detector_version=DIGEST_B,
            configuration={"lookback": 252},
        ),
        DetectorInputIdentityV1(
            detector_id="vcp_v1",
            detector_api_version="1",
            detector_version=DIGEST_C,
            configuration={"b": 2, "a": 1},
        ),
    )
    first = _input_manifest(detectors)
    reordered = _input_manifest(tuple(reversed(detectors)))

    assert first.canonical_json() == reordered.canonical_json()
    assert first.digest() == reordered.digest()
    assert (
        first.digest()
        != first.model_copy(update={"calendar_dataset_digest": DIGEST_C}).digest()
    )


def test_real_detector_manifests_use_exact_closed_allowlists() -> None:
    project_root = Path(__file__).resolve().parents[2]
    manifests = detector_source_manifests(project_root)
    assert tuple(manifests) == (
        "technical_indicators_v1",
        "weinstein_stage_v1",
        "vcp_v1",
    )
    assert all(len(item.digest) == 64 for item in manifests.values())
    assert all(
        manifests[detector.detector_id].manifest["defaults"] == detector.configuration
        for detector in DETECTOR_REGISTRY
    )
    assert all(
        not entry["path"].startswith("tests/")
        for item in manifests.values()
        for entry in item.manifest["files"]
    )


def test_manifest_views_and_detector_configurations_are_immutable() -> None:
    project_root = Path(__file__).resolve().parents[2]
    manifests = detector_source_manifests(project_root)
    first = manifests["technical_indicators_v1"]
    detached = first.manifest
    detached["defaults"]["required_history_sessions"] = 1

    assert first.manifest["defaults"]["required_history_sessions"] == 252
    with pytest.raises(TypeError, match="immutable"):
        DETECTOR_REGISTRY[0].configuration["required_history_sessions"] = 1  # type: ignore[index]


def test_yfinance_ingestion_identity_is_a_canonical_source_manifest() -> None:
    artifact = yfinance_ingestion_source_manifest(Path(__file__).resolve().parents[2])
    assert len(artifact.digest) == 64
    assert artifact.manifest["producer_id"] == "yfinance_historical_ingestion_v1"
