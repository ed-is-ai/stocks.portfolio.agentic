from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

from app.agents.scanner.scanner_agent import ScannerAgent
from app.core.market_regime import MarketRegimeReadingV1
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.pipeline_status import PipelineState
from app.schemas.source_health import SourceName, SourceResult
from app.orchestration.orchestrator import _recover_bau_run_authority
from app.services.backtest.bau_capture_coordinator import _first_eligible_capture_date
from app.services.backtest.bau_run_envelope import (
    BauCaptureMemberV1,
    BauRawEvidenceV1,
    BauRunEnvelopeError,
    BauRunEnvelopeStore,
    BauRunEnvelopeV1,
    BauSnapshotCaptureV1,
)
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.observed_bau_record_builder import (
    ObservedBauBuildError,
    ObservedBauRecordBuilder,
)
from app.services.backtest.bau_snapshot_promotion import BauSnapshotPromotionService
from app.services.backtest.snapshot_profile import ProfileDetectorV1, SnapshotProfileV1
from app.services.backtest.source_manifest import (
    DetectorInputIdentityV1,
    ReconstructionInputManifestV1,
    detector_source_manifests,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar
from app.schemas.analysis_artifact import build_analysis_payload


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _payload() -> SimpleNamespace:
    request = {
        "start": "2025-07-01",
        "end": "2026-08-01",
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
    rows = [
        {
            "session": "2026-07-31",
            "open": float(9).hex(),
            "high": float(11).hex(),
            "low": float(8).hex(),
            "close": float(10).hex(),
            "adj_close": float(10).hex(),
            "volume": float(100).hex(),
            "dividends": float(0).hex(),
            "stock_splits": float(0).hex(),
        }
    ]
    manifest = {
        "provider": "yfinance",
        "provider_version": "1.0",
        "request_contract_version": "YFinanceDailyProviderNativeV1",
        "security_id": "sec-001",
        "alias_revision": "a" * 64,
        "request": request,
        "requested_symbol": "TEST",
        "observed_symbol": "TEST",
        "currency": "USD",
        "quote_unit": "USD",
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "rows": rows,
        "actions": [],
    }
    return SimpleNamespace(
        security_id="sec-001",
        alias_revision="a" * 64,
        provider="yfinance",
        provider_version="1.0",
        request_contract_version="YFinanceDailyProviderNativeV1",
        requested_symbol="TEST",
        observed_symbol="TEST",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start="2025-07-01",
        end="2026-08-01",
        request_contract=request,
        rows=tuple(manifest["rows"]),
        actions=(),
        response_metadata_digest="b" * 64,
        data_revision=manifest_digest(manifest),
        canonical_manifest_json=canonical_json(manifest),
        acquired_at="2026-08-03T12:00:00+00:00",
    )


def _full_payload() -> SimpleNamespace:
    payload = _payload()
    sessions = TradingCalendar().sessions_in_range(
        "XNAS", date(2025, 7, 1), date(2026, 8, 1)
    )[-252:]
    rows = []
    for offset, session in enumerate(sessions):
        close = 100.0 + offset * 0.1
        rows.append(
            {
                "session": session.isoformat(),
                "open": float(close - 1).hex(),
                "high": float(close + 1).hex(),
                "low": float(close - 2).hex(),
                "close": float(close).hex(),
                "adj_close": float(close).hex(),
                "volume": float(1000 + offset).hex(),
                "dividends": float(0).hex(),
                "stock_splits": float(0).hex(),
            }
        )
    manifest = json.loads(payload.canonical_manifest_json)
    manifest["rows"] = rows
    payload.rows = tuple(rows)
    payload.data_revision = manifest_digest(manifest)
    payload.canonical_manifest_json = canonical_json(manifest)
    return payload


def _envelope(run_id: str) -> BauRunEnvelopeV1:
    return BauRunEnvelopeV1(
        run_id=run_id,
        outcome="successful",
        analysis_payload_digest="c" * 64,
        prepared_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        completion_state="completed",
        completed_at=datetime(2026, 8, 3, 12, 1, tzinfo=timezone.utc),
    )


def _capture(
    run_id: str, payload: SimpleNamespace | None = None
) -> BauSnapshotCaptureV1:
    raw = BauRawEvidenceV1.from_historical_payload(payload or _payload())
    detector_manifests = detector_source_manifests(PROJECT_ROOT)
    detectors = tuple(
        ProfileDetectorV1(
            detector_id=item.detector_id,
            detector_api_version=item.detector_api_version,
            detector_version=detector_manifests[item.detector_id].digest,
        )
        for item in DETECTOR_REGISTRY
    )
    profile = SnapshotProfileV1(
        schema_version="snapshot_profile.v1",
        display_version="Scanner data v1",
        record_schema_version="historical_scan_record.v1",
        detectors=detectors,
        roster_policy_version="ReconstructionRosterPolicyV1",
        roster_digest="c" * 64,
        identity_registry_version="SecurityIdentityRegistryV1",
        alias_policy_version="SecurityAliasManifestV1",
        source_policy_version="FreeHistoricalSourcePolicyV1",
        calendar_policy_version="PerExchangeMonthEndV1",
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=TradingCalendar().session_table_digest(),
        yfinance_request_contract_version="YFinanceDailyProviderNativeV1",
        yfinance_ingestion_version=yfinance_ingestion_source_manifest(
            PROJECT_ROOT
        ).digest,
        market_plane_policy_version="HistoricalMarketPlanesV1",
        reconstructability_policy_version="reconstructability.v1",
        provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
        cadence="per-exchange month_end",
    )
    manifest = ReconstructionInputManifestV1(
        schema_version="reconstruction_input_manifest.v1",
        security_id="sec-001",
        snapshot_month="2026-07",
        as_of_session_date=datetime(2026, 7, 31).date(),
        provider_data_revision=raw.data_revision,
        evidence_start=raw.start,
        evidence_end=raw.end,
        provider_request_contract_version=raw.request_contract_version,
        provider_evidence_manifest_digest=raw.data_revision,
        market_plane_policy_version=PRICE_VOLUME_PLANE_VERSION,
        alias_revision=raw.alias_revision,
        roster_digest=profile.roster_digest,
        calendar_dataset_version=profile.calendar_dataset_version,
        calendar_dataset_digest=profile.calendar_dataset_digest,
        yfinance_ingestion_version=profile.yfinance_ingestion_version,
        record_schema_version="historical_scan_record.v1",
        reconstructability_policy_version="reconstructability.v1",
        detectors=tuple(
            DetectorInputIdentityV1(
                detector_id=item.detector_id,
                detector_api_version=item.detector_api_version,
                detector_version=detector_manifests[item.detector_id].digest,
                configuration=dict(item.configuration),
            )
            for item in DETECTOR_REGISTRY
        ),
    )
    member = BauCaptureMemberV1(
        security_id="sec-001",
        mic="XNAS",
        canonical_session=datetime(2026, 7, 31).date(),
        source_cutoff=datetime(2026, 7, 31).date(),
        alias_revision=raw.alias_revision,
        input_manifest=manifest,
        raw_evidence=raw,
    )
    return BauSnapshotCaptureV1(
        source_run_id=run_id,
        snapshot_month="2026-07",
        profile=profile,
        roster_digest=profile.roster_digest,
        roster_captured_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        captured_at=datetime(2026, 8, 3, 12, 1, tzinfo=timezone.utc),
        members=(member,),
    )


def _repository(tmp_path: Path) -> BacktestRepository:
    repository = BacktestRepository(
        db.make_connect(lambda: str(tmp_path / "backtest.db"))
    )
    repository.ensure_schema()
    return repository


def _prepared(run_id: str) -> BauRunEnvelopeV1:
    capture = _capture(run_id)
    return BauRunEnvelopeV1(
        run_id=run_id,
        outcome="pending",
        analysis_payload_digest="d" * 64,
        prepared_at=datetime(2026, 8, 3, 12, 2, tzinfo=timezone.utc),
        completion_state="prepared",
        capture=capture,
        capture_digest=capture.capture_digest,
    )


def test_raw_evidence_is_copied_from_provider_payload_not_presentation_model() -> None:
    raw = BauRawEvidenceV1.from_historical_payload(_payload())

    assert raw.security_id == "sec-001"
    assert raw.rows[0]["session"] == "2026-07-31"
    assert raw.acquired_at == datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def test_raw_evidence_rejects_a_manifest_with_a_different_revision() -> None:
    payload = _payload()
    payload.data_revision = "d" * 64

    with pytest.raises(ValueError, match="revision does not match"):
        BauRawEvidenceV1.from_historical_payload(payload)


def test_envelope_store_is_atomic_and_immutable(tmp_path) -> None:
    store = BauRunEnvelopeStore(tmp_path)
    run_id = str(uuid4())
    envelope = _envelope(run_id)

    assert store.publish(envelope) == tmp_path / f"{run_id}.json"
    assert store.load(run_id) == envelope
    assert store.publish(envelope) == tmp_path / f"{run_id}.json"

    conflicting = envelope.model_copy(update={"analysis_payload_digest": "d" * 64})
    with pytest.raises(BauRunEnvelopeError, match="immutable"):
        store.publish(conflicting)


def test_envelope_rejects_capture_for_failed_run() -> None:
    with pytest.raises(ValueError, match="failed envelope"):
        BauRunEnvelopeV1(
            run_id=str(uuid4()),
            outcome="failed",
            analysis_payload_digest="c" * 64,
            prepared_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            completion_state="failed",
            completed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            capture_digest="a" * 64,
        )


def test_store_replays_only_completed_capture_envelopes(tmp_path) -> None:
    store = BauRunEnvelopeStore(tmp_path)
    # A completed run with no capture is a normal BAU run, not a replay source.
    store.publish(_envelope(str(uuid4())))

    assert store.completed_capture_run_ids() == ()
    assert not store.has_completed_capture("a" * 64, "2026-07")


def test_scanner_uses_capture_session_frame_without_a_second_yfinance_fetch(
    monkeypatch,
) -> None:
    index = pd.date_range("2026-05-01", periods=60, freq="B")
    frame = pd.DataFrame(
        [
            {
                "open": 9.0,
                "high": 11.0,
                "low": 8.0,
                "close": 10.0,
                "volume": 100.0,
            }
            for _ in index
        ],
        index=index,
    )

    class Session:
        def frame_for(self, ticker: str):
            return frame if ticker == "TEST" else None

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("scanner performed a second provider fetch")

    monkeypatch.setattr(
        "app.agents.scanner.scanner_agent.yf.download", unexpected_fetch
    )
    scanner = ScannerAgent(name="ScannerAgent", bau_capture_session=Session())

    fetched = scanner.fetch_stock_data("TEST")
    assert fetched is not None
    assert fetched.equals(frame)


def test_eligible_scanner_run_adds_and_consumes_complete_profile_roster(
    monkeypatch,
) -> None:
    frame = pd.DataFrame(
        {
            "open": [9.0] * 60,
            "high": [11.0] * 60,
            "low": [8.0] * 60,
            "close": [10.0] * 60,
            "volume": [100.0] * 60,
        },
        index=pd.date_range("2026-05-01", periods=60, freq="B"),
    )

    class Session:
        consumed = False

        def preload(self):
            return None

        def roster_tickers(self):
            return ("ROSTER",)

        def frame_for(self, ticker: str):
            if ticker == "ROSTER":
                self.consumed = True
                return frame
            return None

        def complete_capture(self):
            assert self.consumed
            return "capture"

    empty_vcp = SourceResult.from_items(SourceName.VCP_FMP, [])
    empty_us = SourceResult.from_items(SourceName.TRADINGVIEW_US, [])
    empty_uk = SourceResult.from_items(SourceName.TRADINGVIEW_UK, [])
    monkeypatch.setattr(
        "app.agents.scanner.scanner_agent._fetch_spy_context",
        lambda: MarketRegimeReadingV1(
            spy_uptrend=True,
            return_52w_pct=1.0,
            sma_200=1.0,
            latest_close=1.0,
            session_count=252,
            is_degraded=False,
        ),
    )
    monkeypatch.setattr(
        "app.agents.scanner.scanner_agent.fetch_vcp_screener_result",
        lambda: empty_vcp,
    )
    monkeypatch.setattr(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result",
        lambda: empty_us,
    )
    monkeypatch.setattr(
        "app.agents.scanner.scanner_agent.fetch_tv_screener_result_uk",
        lambda: empty_uk,
    )

    def scan_watchlist(self, tickers, *args, **kwargs):
        assert "ROSTER" in tickers
        assert self.fetch_stock_data("ROSTER") is not None
        return []

    monkeypatch.setattr(ScannerAgent, "scan_watchlist", scan_watchlist)
    scanner = ScannerAgent(name="ScannerAgent", bau_capture_session=Session())

    assert scanner.run(["BASE"]) == []
    assert scanner.bau_capture == "capture"


def test_dashboard_analysis_cannot_complete_a_prepared_capture(tmp_path) -> None:
    store = BauRunEnvelopeStore(tmp_path / "envelopes")
    run_id = str(uuid4())
    capture = _capture(run_id)
    analysis = build_analysis_payload(
        [],
        run_id=run_id,
        generated_at=datetime(2026, 8, 3, 12, 2, tzinfo=timezone.utc),
    )
    store.prepare(
        BauRunEnvelopeV1(
            run_id=run_id,
            outcome="pending",
            analysis_payload_digest=manifest_digest(analysis),
            prepared_at=datetime(2026, 8, 3, 12, 2, tzinfo=timezone.utc),
            completion_state="prepared",
            capture=capture,
            capture_digest=capture.capture_digest,
        )
    )
    (tmp_path / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")

    assert store.load(run_id).completion_state == "prepared"


def test_sqlite_journal_claims_only_the_first_profile_month_attempt(tmp_path) -> None:
    repository = _repository(tmp_path)
    first = str(uuid4())

    assert repository.claim_bau_capture_attempt(
        run_id=first,
        profile_hash="a" * 64,
        snapshot_month="2026-07",
        attempted_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    )
    repository.fail_bau_run_authority(
        run_id=first,
        completed_at=datetime(2026, 8, 3, 12, 1, tzinfo=timezone.utc),
        reason="provider failed",
    )

    assert not repository.claim_bau_capture_attempt(
        run_id=str(uuid4()),
        profile_hash="a" * 64,
        snapshot_month="2026-07",
        attempted_at=datetime(2026, 8, 3, 13, tzinfo=timezone.utc),
    )
    with sqlite3.connect(tmp_path / "backtest.db") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone()[0] == 0
        )


def test_terminal_pipeline_status_recovers_journaled_prepared_envelope(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    store = BauRunEnvelopeStore(tmp_path / "envelopes")
    status = PipelineStatusRepository(tmp_path / "pipeline-status.json")
    run_id = str(uuid4())
    prepared = _prepared(run_id)
    assert prepared.capture is not None
    assert prepared.capture_digest is not None
    repository.claim_bau_capture_attempt(
        run_id=run_id,
        profile_hash=prepared.capture.profile.profile_hash,
        snapshot_month="2026-07",
        attempted_at=prepared.prepared_at,
    )
    store.prepare(prepared)
    repository.prepare_bau_run_authority(
        run_id=run_id,
        analysis_payload_digest=prepared.analysis_payload_digest,
        capture_digest=prepared.capture_digest,
        prepared_envelope_digest=prepared.digest(),
    )
    status.start(run_id=run_id)
    status.finish(
        PipelineState.COMPLETE,
        expected_run_id=run_id,
        artifact_produced=True,
    )

    assert _recover_bau_run_authority(repository, store, status) == (run_id,)
    assert store.load(run_id).completion_state == "completed"
    authority = repository.bau_run_authority(run_id)
    assert authority is not None
    assert authority.state == "completed"


def test_failed_pipeline_cannot_recover_a_prepared_envelope_as_success(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    store = BauRunEnvelopeStore(tmp_path / "envelopes")
    status = PipelineStatusRepository(tmp_path / "pipeline-status.json")
    run_id = str(uuid4())
    prepared = _prepared(run_id)
    assert prepared.capture is not None
    assert prepared.capture_digest is not None
    repository.claim_bau_capture_attempt(
        run_id=run_id,
        profile_hash=prepared.capture.profile.profile_hash,
        snapshot_month="2026-07",
        attempted_at=prepared.prepared_at,
    )
    store.prepare(prepared)
    repository.prepare_bau_run_authority(
        run_id=run_id,
        analysis_payload_digest=prepared.analysis_payload_digest,
        capture_digest=prepared.capture_digest,
        prepared_envelope_digest=prepared.digest(),
    )
    status.start(run_id=run_id)
    status.finish(PipelineState.FAILED, expected_run_id=run_id)

    assert _recover_bau_run_authority(repository, store, status) == ()
    assert store.load(run_id).completion_state == "failed"
    authority = repository.bau_run_authority(run_id)
    assert authority is not None
    assert authority.state == "failed"


def test_observed_builder_rejects_partial_scanner_evidence() -> None:
    capture = _capture(str(uuid4()))

    with pytest.raises(ObservedBauBuildError, match="exact canonical history"):
        ObservedBauRecordBuilder().build(
            capture.members[0], roster_captured_at=capture.roster_captured_at
        )


def test_observed_builder_emits_truthful_survivorship_provenance() -> None:
    capture = _capture(str(uuid4()), _full_payload())

    record = (
        ObservedBauRecordBuilder()
        .build(capture.members[0], roster_captured_at=capture.roster_captured_at)
        .record
    )

    assert record.provenance.point_in_time_universe is False
    assert record.provenance.survivorship_bias == "known"
    assert record.provenance.renamed_or_delisted_may_be_absent is True
    assert record.provenance.historical_tradingview_screen_available is False


def test_observed_builder_assigns_legacy_composition_identity_at_promotion() -> None:
    capture = _capture(str(uuid4()), _full_payload())
    result = ObservedBauRecordBuilder().build(
        capture.members[0], roster_captured_at=capture.roster_captured_at
    )

    assert capture.members[0].input_manifest.record_composition_version is None
    assert (
        result.record.provenance.input_revision
        != capture.members[0].input_manifest.digest()
    )


def test_observed_builder_rejects_a_stale_captured_composition_identity() -> None:
    capture = _capture(str(uuid4()), _full_payload())
    member = capture.members[0].model_copy(
        update={
            "input_manifest": capture.members[0].input_manifest.model_copy(
                update={"record_composition_version": "f" * 64}
            )
        }
    )

    with pytest.raises(ObservedBauBuildError, match="composition identity is stale"):
        ObservedBauRecordBuilder().build(
            member, roster_captured_at=capture.roster_captured_at
        )


def test_raw_evidence_rejects_fields_not_owned_by_its_manifest() -> None:
    payload = _payload()
    payload.observed_symbol = "OTHER"

    with pytest.raises(ValueError, match="fields do not match"):
        BauRawEvidenceV1.from_historical_payload(payload)


def test_mixed_us_uk_gate_waits_for_both_next_exchange_sessions() -> None:
    calendar = TradingCalendar()
    sessions = {
        mic: calendar.last_session_of_month(mic, "2026-07") for mic in ("XNAS", "XLON")
    }

    assert (
        _first_eligible_capture_date(calendar, sessions) == datetime(2026, 8, 3).date()
    )


def test_pin_reconciliation_is_derived_from_snapshot_winner_and_repeatable(
    tmp_path,
) -> None:
    class Backtest:
        def snapshot_member_revisions(self, profile_hash, snapshot_month):
            assert (profile_hash, snapshot_month) == ("p" * 64, "2026-07")
            return (("sec-001", "r" * 64),)

    class Prices:
        def __init__(self):
            self.pins = set()

        def pin(self, consumer_type, consumer_id, revision):
            self.pins.add((consumer_type, consumer_id, revision))

    prices = Prices()
    service = BauSnapshotPromotionService(
        backtest_repository=Backtest(),  # type: ignore[arg-type]
        price_repository=prices,  # type: ignore[arg-type]
        envelope_directory=tmp_path,
    )

    service.reconcile_pins("p" * 64, "2026-07")
    service.reconcile_pins("p" * 64, "2026-07")

    assert prices.pins == {("snapshot", f"{'p' * 64}:2026-07:sec-001", "r" * 64)}


def test_fabricated_envelope_file_is_never_a_replay_source(tmp_path) -> None:
    (tmp_path / f"{uuid4()}.json").write_text("{}", encoding="utf-8")

    assert BauRunEnvelopeStore(tmp_path).completed_capture_run_ids() == ()


def test_valid_completed_envelope_without_sqlite_authority_is_rejected(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    store = BauRunEnvelopeStore(tmp_path / "envelopes")
    run_id = str(uuid4())
    prepared = _prepared(run_id)
    store.prepare(prepared)
    envelope = store.complete(
        run_id, completed_at=datetime(2026, 8, 3, 12, 3, tzinfo=timezone.utc)
    )
    assert envelope.capture is not None

    decision = repository.is_promotable_bau(
        envelope.capture.profile, envelope, envelope_store=store
    )

    assert not decision.eligible
    assert decision.reason == "BAU run is not durably authoritative"


def test_replay_failure_does_not_block_later_envelopes(tmp_path, monkeypatch) -> None:
    service = BauSnapshotPromotionService(
        backtest_repository=object(),  # type: ignore[arg-type]
        price_repository=object(),  # type: ignore[arg-type]
        envelope_directory=tmp_path,
    )
    first, second = str(uuid4()), str(uuid4())

    class Store:
        def completed_capture_run_ids(self):
            return (first, second)

    attempted: list[str] = []

    def promote(run_id: str) -> bool:
        attempted.append(run_id)
        if run_id == first:
            raise RuntimeError("stale profile")
        return True

    service._store = Store()  # type: ignore[assignment]
    monkeypatch.setattr(service, "promote_run", promote)

    with pytest.raises(Exception, match="stale profile"):
        service.replay_completed_envelopes()
    assert attempted == [first, second]
