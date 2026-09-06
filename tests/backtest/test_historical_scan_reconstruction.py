from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import subprocess

import pytest
import yfinance as yf

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.detectors import DETECTOR_REGISTRY, DetectorSpec
from app.services.backtest.historical_scan_reconstruction import (
    CALENDAR_DATASET_VERSION,
    DetectorComputeTask,
    HistoricalScanReconstructor,
    ReconstructionError,
    ReconstructionRequestV1,
    _compute_detector_fragments,
    canonical_calendar_digest,
)
from app.services.backtest.market_planes import (
    HistoricalMarketPlanes,
    MarketDataPolicyError,
)
from app.services.backtest.reconstruction_roster import CapturedRosterV1
from app.services.backtest.source_manifest import (
    DetectorInputIdentityV1,
    ReconstructionInputManifestV1,
    detector_source_manifests,
    record_composition_source_manifest,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROSTER_CAPTURED_AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _sessions(count: int) -> tuple[date, ...]:
    calendar = TradingCalendar()._calendar("XNAS")
    return tuple(
        timestamp.date()
        for timestamp in calendar.sessions_window(date(2025, 7, 1), count)
    )


def _hex(value: float) -> str:
    return float(value).hex()


def _evidence(
    *,
    count: int = 252,
    future_close_delta: float = 0,
    null_volume_index: int | None = None,
    zero_volume: bool = False,
) -> StoredHistoricalEvidence:
    sessions = _sessions(count)
    rows: list[dict[str, object]] = []
    for index, session in enumerate(sessions):
        close = 80 + index * 0.12 + math.sin(index / 7) * 2
        if index == count - 1:
            close += future_close_delta
        rows.append(
            {
                "session": session.isoformat(),
                "open": _hex(close - 0.5),
                "high": _hex(close + 1),
                "low": _hex(close - 1),
                "close": _hex(close),
                "adj_close": _hex(close + 99),
                "volume": (
                    None
                    if index == null_volume_index
                    else _hex(0 if zero_volume else 1000.125 + index % 11)
                ),
                "dividends": _hex(0),
                "stock_splits": _hex(0),
            }
        )
    start = sessions[0]
    end = sessions[-1] + timedelta(days=1)
    request_contract = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interval": "1d",
        "prepost": False,
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "timeout": 15,
        "raise_errors": True,
    }
    evidence_manifest = {
        "canonicalizer_version": "YFinanceHexManifestV1",
        "request_contract_version": "YFinanceDailyProviderNativeV1",
        "request": request_contract,
        "requested_symbol": "TEST",
        "observed_symbol": "TEST",
        "currency": "USD",
        "quote_unit": "USD",
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "rows": rows,
        "provider": "yfinance",
        "provider_version": "1.4.1",
        "security_id": "sec-001",
        "alias_revision": DIGEST_B,
        "actions": [],
    }
    evidence_manifest_json = canonical_json(evidence_manifest)
    return StoredHistoricalEvidence(
        data_revision=manifest_digest(evidence_manifest),
        security_id="sec-001",
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version="YFinanceDailyProviderNativeV1",
        requested_symbol="TEST",
        observed_symbol="TEST",
        alias_revision=DIGEST_B,
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start=start.isoformat(),
        end=end.isoformat(),
        request_contract=request_contract,
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json=evidence_manifest_json,
        rows=tuple(rows),
        actions=(),
    )


def _roster() -> CapturedRosterV1:
    body = {
        "schema_version": "ReconstructionRosterManifestV1",
        "policy_version": "ReconstructionRosterPolicyV1",
        "captured_at": ROSTER_CAPTURED_AT,
        "identity_registry_revision": DIGEST_C,
        "alias_revision": DIGEST_B,
        "expected_count": 1,
        "sources": [],
        "members": [
            {
                "security_id": "sec-001",
                "mic": "XNAS",
                "calendar": "XNYS",
                "provider_symbol": "TEST",
                "currency": "USD",
                "quote_unit": "USD",
                "source_memberships": ["tradingview_us"],
                "identity_evidence": [
                    {
                        "mic": "XNAS",
                        "currency": "USD",
                        "quote_unit": "USD",
                        "evidence_source": "fixture",
                        "evidence_digest": DIGEST_C,
                    }
                ],
                "evidence_digest": DIGEST_C,
            }
        ],
        "provenance": {
            "roster_captured_at": ROSTER_CAPTURED_AT,
            "universe_basis": "captured_configured_roster",
            "point_in_time_universe": False,
            "survivorship_bias": "known",
            "warning": "fixture",
        },
    }
    rendered = canonical_json(body)
    return CapturedRosterV1.from_json(manifest_digest(body), rendered)


def _revised_evidence(
    evidence: StoredHistoricalEvidence,
    rows: tuple[dict[str, object], ...],
) -> StoredHistoricalEvidence:
    payload = json.loads(evidence.canonical_manifest_json)
    payload["rows"] = list(rows)
    rendered = canonical_json(payload)
    return replace(
        evidence,
        rows=rows,
        canonical_manifest_json=rendered,
        data_revision=manifest_digest(payload),
    )


def _request(
    evidence: StoredHistoricalEvidence, **changes: object
) -> ReconstructionRequestV1:
    overrides = dict(changes)
    target = overrides.pop(
        "as_of_session_date",
        date.fromisoformat(
            str(evidence.rows[min(251, len(evidence.rows) - 1)]["session"])
        ),
    )
    assert isinstance(target, date)
    snapshot_month = overrides.pop("snapshot_month", target.strftime("%Y-%m"))
    roster = _roster()
    source_manifests = detector_source_manifests(PROJECT_ROOT)
    input_manifest = ReconstructionInputManifestV1(
        schema_version="reconstruction_input_manifest.v1",
        security_id="sec-001",
        snapshot_month=str(snapshot_month),
        as_of_session_date=target,
        provider_data_revision=evidence.data_revision,
        evidence_start=date.fromisoformat(evidence.start),
        evidence_end=date.fromisoformat(evidence.end),
        provider_request_contract_version=evidence.request_contract_version,
        provider_evidence_manifest_digest=evidence.data_revision,
        market_plane_policy_version="HistoricalMarketPlanesV1",
        alias_revision=DIGEST_B,
        roster_digest=roster.roster_digest,
        calendar_dataset_version=CALENDAR_DATASET_VERSION,
        calendar_dataset_digest=canonical_calendar_digest(),
        yfinance_ingestion_version=yfinance_ingestion_source_manifest(
            PROJECT_ROOT
        ).digest,
        record_schema_version="historical_scan_record.v1",
        reconstructability_policy_version="reconstructability.v1",
        record_composition_version=record_composition_source_manifest(
            PROJECT_ROOT
        ).digest,
        detectors=tuple(
            DetectorInputIdentityV1(
                detector_id=detector.detector_id,
                detector_api_version=detector.detector_api_version,
                detector_version=source_manifests[detector.detector_id].digest,
                configuration=dict(detector.configuration),
            )
            for detector in DETECTOR_REGISTRY
        ),
    )
    values: dict[str, object] = {
        "security_id": "sec-001",
        "observed_symbol": "TEST",
        "mic": "XNAS",
        "snapshot_month": snapshot_month,
        "as_of_session_date": target,
        "identity_candidates": ("sec-001",),
        "roster": roster,
        "evidence": evidence,
        "input_manifest": input_manifest,
    }
    values.update(overrides)
    return ReconstructionRequestV1(**values)  # type: ignore[arg-type]


def test_reconstruct_many_preserves_order_and_matches_single_request_records() -> None:
    request = _request(_evidence())
    reconstructor = HistoricalScanReconstructor()
    expected = reconstructor.reconstruct(request)

    assert reconstructor.reconstruct_many((request, request)) == (expected, expected)


def test_detector_worker_returns_a_serializable_failure_outcome() -> None:
    request = _request(_evidence())
    task = DetectorComputeTask(
        security_id=request.security_id,
        as_of_session_date=request.as_of_session_date,
        rows=(),
        keys=HistoricalScanReconstructor._detector_keys(request),
        detector_versions=dict(request.input_manifest.detector_versions),
    )

    outcome = _compute_detector_fragments(task)

    assert outcome.fragments == ()
    assert outcome.error_code == "required_data_missing"
    assert outcome.error_detector == "technical_indicators_v1"


def test_reconstructs_complete_record_and_fragments_without_network_or_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external execution is forbidden")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(yf, "download", forbidden)
    monkeypatch.setattr(yf, "Ticker", forbidden)
    evidence = _evidence()

    result = HistoricalScanReconstructor().reconstruct(_request(evidence))

    assert result.record.security_id == "sec-001"
    assert result.record.as_of_session_date == _sessions(252)[-1]
    assert result.record.enrichment.model_dump() == {
        field: None for field in result.record.enrichment.model_fields
    }
    assert tuple(fragment.detector for fragment in result.fragments) == (
        "technical_indicators_v1",
        "weinstein_stage_v1",
        "vcp_v1",
    )
    assert all(
        fragment.date == result.record.as_of_session_date
        for fragment in result.fragments
    )
    assert result.record.vcp.valid_vcp in {True, False}


def test_reuses_market_planes_for_the_same_evidence_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence()
    request = _request(evidence)
    reconstructor = HistoricalScanReconstructor()
    calls = 0
    original_from_evidence = HistoricalMarketPlanes.from_evidence

    def count_plane_builds(cls, candidate):
        nonlocal calls
        calls += 1
        return original_from_evidence(candidate)

    monkeypatch.setattr(
        HistoricalMarketPlanes, "from_evidence", classmethod(count_plane_builds)
    )

    first = reconstructor.reconstruct(request)
    reconstructor.reconstruct(request)
    reconstructor.adopted_record(
        request,
        technicals=first.record.technicals,
        stage=first.record.stage,
        vcp=first.record.vcp,
    )

    assert calls == 1


def test_cached_planes_reject_evidence_with_a_tampered_provider() -> None:
    evidence = _evidence()
    reconstructor = HistoricalScanReconstructor()
    reconstructor.reconstruct(_request(evidence))

    with pytest.raises(ReconstructionError) as caught:
        reconstructor.reconstruct(_request(replace(evidence, provider="other")))

    assert caught.value.code == "integrity_error"


def test_future_rows_cannot_change_earlier_detector_outputs() -> None:
    baseline = _evidence(count=252)
    with_future = _evidence(count=253, future_close_delta=500)
    request = _request(baseline)

    first = HistoricalScanReconstructor().reconstruct(request).record
    second = (
        HistoricalScanReconstructor()
        .reconstruct(
            _request(
                with_future,
                as_of_session_date=request.as_of_session_date,
                snapshot_month=request.snapshot_month,
            )
        )
        .record
    )

    assert second.technicals == first.technicals
    assert second.stage == first.stage
    assert second.vcp == first.vcp


@pytest.mark.parametrize(
    ("reconstruction_request", "code"),
    [
        (_request(_evidence(count=251)), "required_data_missing"),
        (
            _request(_evidence(), identity_candidates=("sec-001", "sec-002")),
            "identity_ambiguous",
        ),
        (_request(_evidence(null_volume_index=50)), "required_data_missing"),
    ],
)
def test_failures_are_typed_and_never_return_partial_records(
    reconstruction_request: ReconstructionRequestV1, code: str
) -> None:
    with pytest.raises(ReconstructionError) as caught:
        HistoricalScanReconstructor().reconstruct(reconstruction_request)
    assert caught.value.code == code
    assert caught.value.security_id == "sec-001"


def test_market_plane_bounds_failure_is_preserved() -> None:
    evidence = _evidence()
    with pytest.raises(MarketDataPolicyError) as caught:
        HistoricalScanReconstructor().reconstruct(
            _request(
                evidence,
                as_of_session_date=date.fromisoformat(evidence.end),
                snapshot_month=date.fromisoformat(evidence.end).strftime("%Y-%m"),
            )
        )
    assert caught.value.code == "integrity_error"


def test_request_rejects_evidence_security_mismatch() -> None:
    evidence = replace(_evidence(), security_id="different")
    with pytest.raises(ReconstructionError) as caught:
        HistoricalScanReconstructor().reconstruct(_request(evidence))
    assert caught.value.code == "integrity_error"


def test_request_rejects_detector_configuration_not_used_by_runtime() -> None:
    request = _request(_evidence())
    payload = request.input_manifest.model_dump(mode="python")
    detectors = payload["detectors"]
    assert isinstance(detectors, tuple)
    detectors[0]["configuration"]["required_history_sessions"] = 251
    mismatched = ReconstructionInputManifestV1.model_validate(payload)

    with pytest.raises(ReconstructionError) as caught:
        HistoricalScanReconstructor().reconstruct(
            replace(request, input_manifest=mismatched)
        )

    assert caught.value.code == "integrity_error"
    assert caught.value.detector == "technical_indicators_v1"


def test_request_rejects_detector_version_not_generated_from_runtime() -> None:
    request = _request(_evidence())
    payload = request.input_manifest.model_dump(mode="python")
    payload["detectors"][0]["detector_version"] = DIGEST_A
    mismatched = ReconstructionInputManifestV1.model_validate(payload)

    with pytest.raises(ReconstructionError) as caught:
        HistoricalScanReconstructor().reconstruct(
            replace(request, input_manifest=mismatched)
        )

    assert caught.value.code == "integrity_error"
    assert caught.value.detector == "technical_indicators_v1"


def test_request_rejects_tampered_provider_evidence_content() -> None:
    evidence = _evidence()
    tampered_rows = list(evidence.rows)
    tampered_rows[10] = {**tampered_rows[10], "close": _hex(999)}
    tampered = replace(evidence, rows=tuple(tampered_rows))

    with pytest.raises(ReconstructionError) as caught:
        HistoricalScanReconstructor().reconstruct(_request(tampered))

    assert caught.value.code == "integrity_error"


def test_request_rejects_missing_exchange_session_even_with_252_rows() -> None:
    evidence = _evidence(count=253)
    rows = tuple(dict(row) for index, row in enumerate(evidence.rows) if index != 100)
    sparse = _revised_evidence(evidence, rows)

    with pytest.raises(ReconstructionError) as caught:
        HistoricalScanReconstructor().reconstruct(_request(sparse))

    assert caught.value.code == "required_data_missing"


def test_request_rejects_untrusted_roster_or_ingestion_identity() -> None:
    request = _request(_evidence())
    roster_payload = json.loads(request.roster.canonical_manifest_json)
    roster_payload["members"][0]["mic"] = "XNYS"
    rendered = canonical_json(roster_payload)
    bad_roster = CapturedRosterV1.from_json(manifest_digest(roster_payload), rendered)
    manifest_payload = request.input_manifest.model_dump(mode="python")
    manifest_payload["roster_digest"] = bad_roster.roster_digest
    bad_manifest = ReconstructionInputManifestV1.model_validate(manifest_payload)

    with pytest.raises(ReconstructionError) as roster_error:
        HistoricalScanReconstructor().reconstruct(
            replace(request, roster=bad_roster, input_manifest=bad_manifest)
        )
    assert roster_error.value.code == "integrity_error"

    ingestion_payload = request.input_manifest.model_dump(mode="python")
    ingestion_payload["yfinance_ingestion_version"] = "fabricated"
    with pytest.raises(ReconstructionError) as ingestion_error:
        HistoricalScanReconstructor().reconstruct(
            replace(
                request,
                input_manifest=ReconstructionInputManifestV1.model_validate(
                    ingestion_payload
                ),
            )
        )
    assert ingestion_error.value.code == "integrity_error"


def test_zero_volume_is_preserved_while_missing_volume_fails() -> None:
    result = HistoricalScanReconstructor().reconstruct(
        _request(_evidence(zero_volume=True))
    )
    assert result.record.technicals.volume.is_zero()
    assert result.record.technicals.vol_ma50.is_zero()
    assert result.record.technicals.rel_volume == 1


def test_reconstruction_modules_have_no_agent_or_live_portfolio_imports() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "app/services/backtest/historical_scan_reconstruction.py",
        "app/services/backtest/detectors.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "app.agents" not in source
        assert "TraderAgent" not in source
        assert "price_cache" not in source
        assert "app.repositories.trades" not in source


def test_reconstruction_reuses_exact_cached_fragments_without_rerunning_detectors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    request = _request(_evidence())
    reconstructor = HistoricalScanReconstructor(repo)
    first = reconstructor.reconstruct(request)

    def forbidden_run(_self: DetectorSpec, _context: object) -> object:
        raise AssertionError("cache hit must not rerun detector")

    monkeypatch.setattr(DetectorSpec, "run", forbidden_run)
    second = reconstructor.reconstruct(request)

    assert second.record.canonical_json_bytes() == first.record.canonical_json_bytes()
    assert second.fragments == first.fragments
    assert repo.detector_cache_count() == 3


def _roster_with_new_digest() -> CapturedRosterV1:
    """A distinct roster generation: same members/alias_revision, new digest.

    Mirrors the live-DB shape this fixes -- four roster generations there
    already share one ``alias_revision`` while each has its own
    ``roster_digest`` (a new ``identity_registry_revision`` is enough to
    change the digest without touching anything reconstruct() reads).
    """
    body = {
        "schema_version": "ReconstructionRosterManifestV1",
        "policy_version": "ReconstructionRosterPolicyV1",
        "captured_at": ROSTER_CAPTURED_AT,
        "identity_registry_revision": DIGEST_A,
        "alias_revision": DIGEST_B,
        "expected_count": 1,
        "sources": [],
        "members": [
            {
                "security_id": "sec-001",
                "mic": "XNAS",
                "calendar": "XNYS",
                "provider_symbol": "TEST",
                "currency": "USD",
                "quote_unit": "USD",
                "source_memberships": ["tradingview_us"],
                "identity_evidence": [
                    {
                        "mic": "XNAS",
                        "currency": "USD",
                        "quote_unit": "USD",
                        "evidence_source": "fixture",
                        "evidence_digest": DIGEST_C,
                    }
                ],
                "evidence_digest": DIGEST_C,
            }
        ],
        "provenance": {
            "roster_captured_at": ROSTER_CAPTURED_AT,
            "universe_basis": "captured_configured_roster",
            "point_in_time_universe": False,
            "survivorship_bias": "known",
            "warning": "fixture",
        },
    }
    rendered = canonical_json(body)
    return CapturedRosterV1.from_json(manifest_digest(body), rendered)


def test_reconstruction_reuses_cache_across_a_new_roster_generation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new roster generation must not force detectors to rerun.

    ``roster_digest`` gates evidence lookup and month scope-inclusion --
    decided before ``reconstruct()`` runs -- not the ``rows`` a detector
    computes over. Two requests differing only in ``roster``/
    ``roster_digest`` (same evidence, same alias_revision, same detector
    versions) must resolve to the same detector-fragment cache row rather
    than each computing and storing its own copy (#487 follow-up).
    """
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    first_request = _request(_evidence())
    first = HistoricalScanReconstructor(repo).reconstruct(first_request)
    assert repo.detector_cache_count() == 3

    next_roster = _roster_with_new_digest()
    assert next_roster.roster_digest != first_request.roster.roster_digest
    next_generation_request = replace(
        first_request,
        roster=next_roster,
        input_manifest=first_request.input_manifest.model_copy(
            update={"roster_digest": next_roster.roster_digest}
        ),
    )

    def forbidden_run(_self: DetectorSpec, _context: object) -> object:
        raise AssertionError("cache hit must not rerun detector")

    monkeypatch.setattr(DetectorSpec, "run", forbidden_run)
    reused = HistoricalScanReconstructor(repo).reconstruct(next_generation_request)

    assert repo.detector_cache_count() == 3
    assert reused.record.technicals == first.record.technicals
    assert reused.record.stage == first.record.stage
    assert reused.record.vcp == first.record.vcp


def test_concurrent_complete_reconstruction_converges_on_three_cache_rows(
    tmp_path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    request = _request(_evidence())

    def reconstruct(_index: int) -> bytes:
        independent_repo = BacktestRepository(db.make_connect(lambda: path))
        return (
            HistoricalScanReconstructor(independent_repo)
            .reconstruct(request)
            .record.canonical_json_bytes()
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = tuple(pool.map(reconstruct, range(8)))

    assert len(set(records)) == 1
    assert repo.detector_cache_count() == 3
