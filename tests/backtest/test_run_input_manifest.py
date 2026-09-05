"""Story 2.3 coverage: ``RunInputManifestV1`` determinism, the two-digest
AD-19/AD-20 scope split, and ``build_run_input_manifest``'s
``evidence_missing`` replay guarantee."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository, RosterCaptureCommit
from app.repositories.fx_quote_repo import FxQuote, FxQuoteRepository
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalPriceRepository,
)
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceFxSeriesFetcher,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.run_input_manifest import (
    ENGINE_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    DetectorSourceDigestV1,
    PinnedSecurityEvidenceV1,
    RunInputManifestError,
    RunInputManifestV1,
    build_run_input_manifest,
    build_run_input_manifest_v2,
    read_run_input_manifest,
)
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.strategy_job import RunUniverseSelectionV1
from app.services.backtest.skill_discovery import discover_strategies
from app.services.backtest.snapshot_profile import (
    MonthlySnapshotCommitV1,
    ProfileDetectorV1,
    SnapshotMemberV1,
    SnapshotProfileV1,
)
from app.services.backtest.source_manifest import detector_source_manifests
from app.services.backtest.trading_calendar import TradingCalendar

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "historical_scan_record_v1.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_ROOT = (
    PROJECT_ROOT / "tests" / "fixtures" / "backtest-strategies" / "discovery"
)


# ---------------------------------------------------------------------------
# Model-level fixtures -- direct construction (no repositories involved)
# ---------------------------------------------------------------------------


def _detector_digests(
    *,
    technical_indicators_v1: str = DIGEST_A,
    weinstein_stage_v1: str = DIGEST_B,
    vcp_v1: str = DIGEST_C,
) -> tuple[DetectorSourceDigestV1, ...]:
    return (
        DetectorSourceDigestV1(
            detector_id="technical_indicators_v1", source_digest=technical_indicators_v1
        ),
        DetectorSourceDigestV1(
            detector_id="weinstein_stage_v1", source_digest=weinstein_stage_v1
        ),
        DetectorSourceDigestV1(detector_id="vcp_v1", source_digest=vcp_v1),
    )


def _securities(
    *, price_revision: str = DIGEST_A, count: int = 1
) -> tuple[PinnedSecurityEvidenceV1, ...]:
    return tuple(
        PinnedSecurityEvidenceV1(
            security_id=f"sec-{index:03d}",
            price_revision=price_revision,
            action_revision=price_revision,
            fx_revision=None,
        )
        for index in range(count)
    )


def _manifest(**overrides: object) -> RunInputManifestV1:
    defaults: dict[str, object] = dict(
        schema_version="run_input_manifest.v1",
        engine_version=ENGINE_VERSION,
        protocol_schema_version=PROTOCOL_SCHEMA_VERSION,
        market_view_source_digest=DIGEST_A,
        ledger_action_metrics_digest=DIGEST_B,
        numeric_rounding_policy="HistoricalMarketPlanesV1",
        runtime_lock_digest=DIGEST_C,
        calendar_session_table_digest=DIGEST_D,
        python_runtime="3.13",
        timezone_dataset_version="2026.2",
        strategy_id="strategy-1",
        strategy_api_version=1,
        strategy_source_digest=DIGEST_A,
        detector_source_digests=_detector_digests(),
        parameters={"lookback": 20, "watch": "sec-aapl"},
        alias_revision=DIGEST_B,
        securities=_securities(),
        profile_hash=DIGEST_A,
        start_month="2026-06",
        end_month="2026-07",
        ordered_month_digest=DIGEST_C,
        base_currency="GBP",
        starting_capital=Decimal("10000"),
    )
    defaults.update(overrides)
    return RunInputManifestV1(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_canonical_json_and_digest_are_order_independent() -> None:
    detectors = _detector_digests()
    securities = _securities(count=3)
    first = _manifest(detector_source_digests=detectors, securities=securities)
    reordered = _manifest(
        detector_source_digests=tuple(reversed(detectors)),
        securities=tuple(reversed(securities)),
    )

    assert first.canonical_json() == reordered.canonical_json()
    assert first.digest() == reordered.digest()


def test_digest_is_stable_across_repeated_calls() -> None:
    manifest = _manifest()
    assert manifest.digest() == manifest.digest()
    assert manifest.canonical_json() == manifest.canonical_json()


def test_v2_manifest_dispatch_and_runtime_selection_equality() -> None:
    s = RunUniverseSelectionV1(
        profile_hash=DIGEST_A,
        activation_seq=1,
        universe_parameter="symbols",
        canonical_security_ids=("sec-000",),
        run_universe_digest=run_universe_digest(
            ["sec-000"], parameter="symbols", profile_hash=DIGEST_A
        ),
    )
    base = _manifest(parameters={"symbols": ["sec-000"]})
    v2 = build_run_input_manifest_v2(
        base, selection=s, source_preparation_job_id="prep"
    )
    assert (
        read_run_input_manifest(v2.canonical_json()).canonical_json()
        == v2.canonical_json()
    )
    with pytest.raises(ValueError, match="runtime universe"):
        build_run_input_manifest_v2(
            _manifest(parameters={"symbols": ["other"]}),
            selection=s,
            source_preparation_job_id="prep",
        )


def test_manifest_float_parameters_round_trip_as_numbers() -> None:
    manifest = _manifest(parameters={"threshold": 1.5})

    restored = read_run_input_manifest(manifest.canonical_json())

    assert restored.parameters == {"threshold": 1.5}
    assert isinstance(restored.parameters["threshold"], float)
    assert restored.digest() == manifest.digest()


def test_digest_changes_when_a_pinned_evidence_revision_changes() -> None:
    first = _manifest()
    changed = first.model_copy(
        update={"securities": _securities(price_revision=DIGEST_B)}
    )

    assert first.digest() != changed.digest()
    # Same Strategy/params/capital/semantics, only pinned evidence differs
    # -- execution_contract_digest must still agree.
    assert first.execution_contract_digest() == changed.execution_contract_digest()


def test_starting_capital_is_rendered_as_its_exact_decimal_string() -> None:
    manifest = _manifest(starting_capital=Decimal("12345.678"))
    assert manifest.canonical_payload()["starting_capital"] == "12345.678"


# ---------------------------------------------------------------------------
# Story 2.4 regression: the ledger/action/metrics digest now reflects real
# ``backtest_engine.py`` source, not the pre-Story-2.4 placeholder.
# ---------------------------------------------------------------------------


def test_engine_and_protocol_schema_versions_bumped_for_real_engine_semantics() -> None:
    assert ENGINE_VERSION == "backtest_engine.v4"
    assert PROTOCOL_SCHEMA_VERSION == "strategy_protocol.v3"


def test_ledger_action_metrics_digest_now_hashes_real_backtest_engine_source() -> None:
    """Regression: prior to Story 2.4, ``_ledger_action_metrics_digest``
    returned a deterministic placeholder identity unrelated to any source
    file. It now delegates to ``build_source_manifest`` over the real
    ``backtest_engine.py`` -- mirroring ``_market_view_source_manifest``'s
    pattern exactly -- so it changes now that real engine semantics exist,
    and it genuinely reflects that one allowlisted file's content."""
    import app.services.backtest.run_input_manifest as run_input_manifest_module

    real_digest = run_input_manifest_module._ledger_action_metrics_digest(PROJECT_ROOT)
    assert len(real_digest) == 64
    int(real_digest, 16)  # raises ValueError if not valid hex

    placeholder_digest = manifest_digest(
        {
            "schema_version": "ledger_action_metrics_policy.v1",
            "policy_version": "ledger_action_metrics.v1",
        }
    )
    assert real_digest != placeholder_digest

    artifact = run_input_manifest_module._ledger_action_metrics_source_manifest(
        PROJECT_ROOT
    )
    assert artifact.digest == real_digest
    assert artifact.manifest["producer_id"] == "backtest_engine"
    assert artifact.manifest["api_version"] == "1"
    assert len(artifact.manifest["files"]) == 1
    (only_file,) = artifact.manifest["files"]
    assert only_file["path"] == "app/services/backtest/backtest_engine.py"
    assert len(only_file["sha256"]) == 64
    int(only_file["sha256"], 16)


def test_build_run_input_manifest_pins_the_real_ledger_action_metrics_digest(
    tmp_path,
) -> None:
    """End-to-end regression: a manifest built through the real builder
    now pins the real digest, and ``execution_contract_digest`` genuinely
    changes relative to the pre-Story-2.4 placeholder identity -- so two
    Runs compared before/after this story are correctly judged
    incomparable."""
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")

    manifest = build_run_input_manifest(
        project_root=PROJECT_ROOT,
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
        strategy=_strategy(),
        submitted_parameters={},
        profile_hash=PROFILE_HASH,
        start_month="2026-07",
        end_month="2026-07",
        base_currency="USD",
        starting_capital=Decimal("10000"),
        securities=(
            PinnedSecurityEvidenceV1(
                security_id="sec-001",
                price_revision=revision,
                action_revision=revision,
                fx_revision=None,
            ),
        ),
    )

    import app.services.backtest.run_input_manifest as run_input_manifest_module

    assert manifest.engine_version == "backtest_engine.v4"
    assert manifest.protocol_schema_version == "strategy_protocol.v3"
    assert manifest.ledger_action_metrics_digest == (
        run_input_manifest_module._ledger_action_metrics_digest(PROJECT_ROOT)
    )

    placeholder_digest = manifest_digest(
        {
            "schema_version": "ledger_action_metrics_policy.v1",
            "policy_version": "ledger_action_metrics.v1",
        }
    )
    assert manifest.ledger_action_metrics_digest != placeholder_digest

    pre_story_2_4 = manifest.model_copy(
        update={
            "engine_version": "backtest_engine.v1",
            "protocol_schema_version": "strategy_protocol.v1",
            "ledger_action_metrics_digest": placeholder_digest,
        }
    )
    assert (
        manifest.execution_contract_digest()
        != pre_story_2_4.execution_contract_digest()
    )


# ---------------------------------------------------------------------------
# execution_contract_digest -- the AD-19/AD-20 scope split
# ---------------------------------------------------------------------------


def test_execution_contract_digest_ignores_strategy_parameters_and_capital() -> None:
    baseline = _manifest()
    different_strategy_and_capital = _manifest(
        strategy_id="strategy-2",
        strategy_source_digest=DIGEST_D,
        parameters={"lookback": 99},
        starting_capital=Decimal("50000"),
        securities=_securities(price_revision=DIGEST_D),
    )

    assert (
        baseline.execution_contract_digest()
        == different_strategy_and_capital.execution_contract_digest()
    )
    assert baseline.digest() != different_strategy_and_capital.digest()


def test_execution_contract_digest_ignores_regime_filter_params() -> None:
    baseline = _manifest()
    with_regime_filter = _manifest(
        parameters={
            "lookback": 20,
            "watch": "sec-aapl",
            "regime_filter_enabled": True,
            "regime_filter_benchmark_security_id": "sec-spy",
            "regime_filter_ma_length": 200,
        },
    )

    assert (
        baseline.execution_contract_digest()
        == with_regime_filter.execution_contract_digest()
    )
    assert baseline.digest() != with_regime_filter.digest()


def test_execution_contract_digest_changes_with_engine_semantics() -> None:
    baseline = _manifest()
    different_engine = baseline.model_copy(
        update={"market_view_source_digest": DIGEST_D}
    )

    assert (
        baseline.execution_contract_digest()
        != different_engine.execution_contract_digest()
    )


# ---------------------------------------------------------------------------
# Construction invariants
# ---------------------------------------------------------------------------


def test_manifest_is_frozen_and_extra_forbidden() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError):
        manifest.starting_capital = Decimal("1")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _manifest(unexpected_field="x")


@pytest.mark.parametrize("capital", [Decimal("0"), Decimal("-1")])
def test_non_positive_starting_capital_is_rejected(capital: Decimal) -> None:
    with pytest.raises(ValidationError):
        _manifest(starting_capital=capital)


def test_start_month_after_end_month_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(start_month="2026-08", end_month="2026-06")


def test_duplicate_security_ids_are_rejected() -> None:
    duplicate = (
        PinnedSecurityEvidenceV1(
            security_id="sec-001",
            price_revision=DIGEST_A,
            action_revision=DIGEST_A,
        ),
        PinnedSecurityEvidenceV1(
            security_id="sec-001",
            price_revision=DIGEST_B,
            action_revision=DIGEST_B,
        ),
    )
    with pytest.raises(ValidationError):
        _manifest(securities=duplicate)


def test_empty_securities_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(securities=())


def test_incomplete_detector_set_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(
            detector_source_digests=(
                DetectorSourceDigestV1(
                    detector_id="technical_indicators_v1", source_digest=DIGEST_A
                ),
            )
        )


# ---------------------------------------------------------------------------
# build_run_input_manifest -- repository-backed builder
# ---------------------------------------------------------------------------


class _FakeTicker:
    def __init__(self, frame: pd.DataFrame, symbol: str) -> None:
        self._frame = frame
        self._symbol = symbol

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": self._symbol,
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
        }


def _commit_price_evidence(
    repo: HistoricalPriceRepository, *, security_id: str, symbol: str = "AAPL"
) -> str:
    sessions = (date(2026, 6, 1), date(2026, 6, 2))
    frame = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Adj Close": [101.0, 102.0],
            "Volume": [1_000.0, 1_100.0],
            "Dividends": [0.0, 0.0],
            "Stock Splits": [0.0, 0.0],
        },
        index=pd.DatetimeIndex(
            [session.isoformat() for session in sessions], tz="America/New_York"
        ),
    )
    request = HistoricalEvidenceRequest(
        security_id=security_id,
        alias_revision=DIGEST_B,
        symbol=symbol,
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        expected_sessions=sessions,
        allowed_observed_symbols=(symbol,),
    )
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: _FakeTicker(frame, symbol), clock=lambda: NOW
    ).fetch(request)
    repo.commit(payload)
    return payload.data_revision


def _price_repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    repo.ensure_schema()
    return repo


def _fx_quote_repo(tmp_path: Path) -> FxQuoteRepository:
    connect = db.make_connect(lambda: tmp_path / "trades.db")
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return FxQuoteRepository(connect)


def _authoritative_detectors() -> tuple[ProfileDetectorV1, ...]:
    manifests = detector_source_manifests(PROJECT_ROOT)
    return tuple(
        ProfileDetectorV1(
            detector_id=detector.detector_id,
            detector_api_version=detector.detector_api_version,
            detector_version=manifests[detector.detector_id].digest,
        )
        for detector in DETECTOR_REGISTRY
    )


def _profile() -> SnapshotProfileV1:
    return SnapshotProfileV1(
        schema_version="snapshot_profile.v1",
        display_version="Scanner data v1",
        record_schema_version="historical_scan_record.v1",
        detectors=_authoritative_detectors(),
        roster_policy_version="ReconstructionRosterPolicyV1",
        roster_digest=DIGEST_A,
        identity_registry_version="SecurityIdentityRegistryV1",
        alias_policy_version="SecurityAliasManifestV1",
        source_policy_version="FreeHistoricalSourcePolicyV1",
        calendar_policy_version="PerExchangeMonthEndV1",
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=TradingCalendar().session_table_digest(),
        yfinance_request_contract_version="yfinance-daily-v1",
        yfinance_ingestion_version="ingestion-v1",
        market_plane_policy_version="HistoricalMarketPlanesV1",
        reconstructability_policy_version="reconstructability.v1",
        provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
        cadence="per-exchange month_end",
    )


#: Deterministic (content-derived, no randomness) -- computed once so every
#: test can pass the exact ``profile_hash`` the committed profile actually
#: has, rather than an unrelated placeholder digest.
PROFILE_HASH = _profile().profile_hash


def _record() -> HistoricalScanRecordV1:
    original = HistoricalScanRecordV1.from_canonical_json(
        FIXTURE.read_bytes().rstrip(b"\n")
    )
    payload = original.model_dump(mode="python")
    provenance = dict(payload["provenance"])
    provenance["calendar_dataset_digest"] = TradingCalendar().session_table_digest()
    provenance["detector_versions"] = {
        item.detector_id: item.detector_version for item in _authoritative_detectors()
    }
    payload["provenance"] = provenance
    provisional = HistoricalScanRecordV1.model_validate(payload, strict=False)
    # Reconcile the record's declared provider revision with the evidence
    # ``_evidence_for_record`` actually constructs for it -- mirroring
    # ``test_snapshot_coverage_repository.py``'s ``_record`` helper.
    revision = _evidence_for_record(provisional).data_revision
    provenance = dict(provisional.provenance.model_dump(mode="python"))
    provenance["provider_data_revision"] = revision
    provenance["provider_evidence_manifest_digest"] = revision
    payload = provisional.model_dump(mode="python")
    payload["provenance"] = provenance
    return HistoricalScanRecordV1.model_validate(payload)


def _evidence_for_record(record: HistoricalScanRecordV1):
    from app.repositories.historical_price_repo import StoredHistoricalEvidence
    from app.services.backtest.canonical_manifest import canonical_json, manifest_digest

    session = record.as_of_session_date
    start = f"{session.year}-01-01"
    end = date.fromordinal(session.toordinal() + 1).isoformat()
    rows = (
        {
            "session": session.isoformat(),
            "open": float(100).hex(),
            "high": float(102).hex(),
            "low": float(99).hex(),
            "close": float(101).hex(),
            "adj_close": float(101).hex(),
            "volume": float(1000).hex(),
            "dividends": float(0).hex(),
            "stock_splits": float(0).hex(),
        },
    )
    manifest = {
        "canonicalizer_version": "HistoricalEvidenceCanonicalizerV1",
        "request_contract_version": record.provenance.provider_request_contract_version,
        "request": {
            "start": start,
            "end": end,
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
        },
        "requested_symbol": record.observed_symbol,
        "observed_symbol": record.observed_symbol,
        "currency": record.currency,
        "quote_unit": record.quote_unit,
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "rows": rows,
        "provider": "yfinance",
        "provider_version": "1.4.1",
        "security_id": record.security_id,
        "alias_revision": DIGEST_B,
        "actions": (),
    }
    rendered = canonical_json(manifest)
    return StoredHistoricalEvidence(
        data_revision=manifest_digest(manifest),
        security_id=record.security_id,
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version=record.provenance.provider_request_contract_version,
        requested_symbol=record.observed_symbol,
        observed_symbol=record.observed_symbol,
        alias_revision=DIGEST_B,
        currency=record.currency,
        quote_unit=record.quote_unit,
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start=start,
        end=end,
        request_contract=manifest["request"],
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json=rendered,
        rows=rows,
        actions=(),
    )


class _Verifier:
    def __init__(self, snapshot: MonthlySnapshotCommitV1) -> None:
        self._evidence = {
            item.data_revision: item
            for item in (_evidence_for_record(record) for record in snapshot.records)
        }

    def verify(self, data_revision: str):  # noqa: ANN201
        return self._evidence[data_revision]


def _roster_commit() -> RosterCaptureCommit:
    return RosterCaptureCommit(
        lineage_id="lineage-1",
        roster_digest=DIGEST_A,
        roster_manifest_json='{"schema_version":"ReconstructionRosterManifestV1"}',
        policy_version="ReconstructionRosterPolicyV1",
        identity_registry_revision="d" * 64,
        identity_registry_json='{"identities":[]}',
        identity_evidence_digest="e" * 64,
        alias_revision=DIGEST_B,
        alias_manifest_json='{"entries":[]}',
        alias_evidence_digest="f" * 64,
        captured_at=NOW.isoformat(),
        identities=(("sec-001", "XNAS", "CAFÉ", "1" * 64),),
        aliases=(
            (
                "sec-001",
                "yfinance",
                "XNAS",
                "CAFÉ",
                None,
                None,
                "fixture",
                "4" * 64,
                "provider_evidence",
            ),
        ),
        sources=(("datahub_sp500", "2" * 64, "[]", NOW.isoformat()),),
        members=(
            (
                "sec-001",
                "XNAS",
                "CAFÉ",
                "USD",
                '["datahub_sp500"]',
                "[]",
                "3" * 64,
            ),
        ),
    )


def _backtest_repo(tmp_path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: tmp_path / "backtest.db"),
        clock=lambda: date(2026, 8, 11),
    )
    repo.ensure_schema()
    repo.commit_roster_capture(_roster_commit())
    return repo


def _commit_ready_month(repo: BacktestRepository) -> None:
    profile = _profile()
    record = _record()
    commit = MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month="2026-07",
        provenance_quality="best_effort_reconstructed",
        members=(SnapshotMemberV1.valid_scan(record),),
        records=(record,),
        committed_at=NOW,
        as_of=date(2026, 8, 11),
    )
    repo.commit_snapshot_month(commit, _Verifier(commit))


def _strategy():
    result = discover_strategies(DISCOVERY_ROOT)
    (descriptor,) = (
        item for item in result.strategies if item.strategy_id == "valid-strategy"
    )
    return descriptor


def test_build_run_input_manifest_resolves_and_binds_a_valid_launch(
    tmp_path,
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")

    manifest = build_run_input_manifest(
        project_root=PROJECT_ROOT,
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
        strategy=_strategy(),
        submitted_parameters={},
        profile_hash=PROFILE_HASH,
        start_month="2026-07",
        end_month="2026-07",
        base_currency="USD",
        starting_capital=Decimal("10000"),
        securities=(
            PinnedSecurityEvidenceV1(
                security_id="sec-001",
                price_revision=revision,
                action_revision=revision,
                fx_revision=None,
            ),
        ),
    )

    assert manifest.strategy_id == "valid-strategy"
    assert manifest.parameters["watch_security_id"] == "sec-aapl"
    assert manifest.parameters["fixed_shares"] == 1
    assert manifest.alias_revision == DIGEST_B
    assert manifest.digest() == manifest.digest()


def test_build_run_input_manifest_replay_fails_evidence_missing_when_revision_vanishes(
    tmp_path,
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)

    with pytest.raises(EvidenceMissingError):
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={},
            profile_hash=PROFILE_HASH,
            start_month="2026-07",
            end_month="2026-07",
            base_currency="USD",
            starting_capital=Decimal("10000"),
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="sec-001",
                    price_revision="9" * 64,
                    action_revision="9" * 64,
                    fx_revision=None,
                ),
            ),
        )


def test_build_run_input_manifest_rejects_incomplete_coverage(tmp_path) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")

    with pytest.raises(RunInputManifestError) as exc_info:
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={},
            profile_hash=PROFILE_HASH,
            start_month="2026-06",
            end_month="2026-07",
            base_currency="USD",
            starting_capital=Decimal("10000"),
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="sec-001",
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision=None,
                ),
            ),
        )

    assert exc_info.value.code == "coverage_incomplete"


def test_build_run_input_manifest_rejects_invalid_submitted_parameters(
    tmp_path,
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")

    with pytest.raises(RunInputManifestError) as exc_info:
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={"fixed_shares": -1},
            profile_hash=PROFILE_HASH,
            start_month="2026-07",
            end_month="2026-07",
            base_currency="USD",
            starting_capital=Decimal("10000"),
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="sec-001",
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision=None,
                ),
            ),
        )

    assert exc_info.value.code == "invalid_parameters"


def _commit_fx_series_evidence(repo: HistoricalPriceRepository) -> str:
    """Commit a deterministic daily GBPUSD=X series as the ``fx:GBPUSD=X``
    pseudo-security through the production ranged fetcher (#459), and
    return its content-addressed revision."""
    sessions = tuple(date(2026, 6, 1) + timedelta(days=i) for i in range(30))
    frame = pd.DataFrame(
        {
            "Open": [1.27] * len(sessions),
            "High": [1.28] * len(sessions),
            "Low": [1.26] * len(sessions),
            "Close": [1.27] * len(sessions),
            "Adj Close": [1.27] * len(sessions),
            "Volume": [0.0] * len(sessions),
            "Dividends": [0.0] * len(sessions),
            "Stock Splits": [0.0] * len(sessions),
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp(session) for session in sessions], tz="UTC"
        ),
    )

    class _FxTicker:
        def history(self, **_kwargs: object) -> pd.DataFrame:
            return frame.copy()

        def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
            return {
                "symbol": "GBPUSD=X",
                "currency": "USD",
                "exchangeTimezoneName": "Europe/London",
            }

    payload = YFinanceFxSeriesFetcher(lambda _symbol: _FxTicker()).fetch(
        start=sessions[0], end=sessions[-1] + timedelta(days=1)
    )
    repo.commit(payload)
    return payload.data_revision


def test_build_run_input_manifest_resolves_fx_revision_via_historical_price_repo(
    tmp_path,
) -> None:
    """A currency-mismatched security's ``fx_revision`` resolves through
    ``HistoricalPriceRepository`` as the ingested ``fx:GBPUSD=X`` daily
    series (#459) -- the exact artifact the engine replays through
    ``currency.py``."""
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")
    fx_revision = _commit_fx_series_evidence(price_repo)

    manifest = build_run_input_manifest(
        project_root=PROJECT_ROOT,
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
        strategy=_strategy(),
        submitted_parameters={},
        profile_hash=PROFILE_HASH,
        start_month="2026-07",
        end_month="2026-07",
        base_currency="GBP",
        starting_capital=Decimal("10000"),
        securities=(
            PinnedSecurityEvidenceV1(
                security_id="sec-001",
                price_revision=revision,
                action_revision=revision,
                fx_revision=fx_revision,
            ),
        ),
    )

    assert manifest.securities[0].fx_revision == fx_revision


def test_build_run_input_manifest_rejects_legacy_fx_quotes_only_digest(
    tmp_path,
) -> None:
    """A pre-#459 manifest whose ``fx_revision`` is a single-day
    ``fx_quotes`` digest is rejected at seal time -- the series evidence
    in the historical price cache is now mandatory."""
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    fx_repo = _fx_quote_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")
    quote = FxQuote(
        pair="GBPUSD=X",
        as_of="2026-07-01",
        rate=Decimal("1.25"),
        digest="f" * 64,
    )
    fx_repo.insert_or_get(quote)

    with pytest.raises(EvidenceMissingError):
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={},
            profile_hash=PROFILE_HASH,
            start_month="2026-07",
            end_month="2026-07",
            base_currency="GBP",
            starting_capital=Decimal("10000"),
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="sec-001",
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision=quote.digest,
                ),
            ),
        )


def test_build_run_input_manifest_rejects_currency_mismatch_without_fx_revision(
    tmp_path,
) -> None:
    """USD evidence pinned against a GBP base with no ``fx_revision`` is a
    caller-side pinning bug, not a valid same-currency Run."""
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")

    with pytest.raises(RunInputManifestError) as exc_info:
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={},
            profile_hash=PROFILE_HASH,
            start_month="2026-07",
            end_month="2026-07",
            base_currency="GBP",
            starting_capital=Decimal("10000"),
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="sec-001",
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision=None,
                ),
            ),
        )

    assert exc_info.value.code == "evidence_mismatch"


def test_build_run_input_manifest_rejects_unresolvable_fx_revision(tmp_path) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(price_repo, security_id="sec-001")

    with pytest.raises(EvidenceMissingError):
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={},
            profile_hash=PROFILE_HASH,
            start_month="2026-07",
            end_month="2026-07",
            base_currency="GBP",
            starting_capital=Decimal("10000"),
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="sec-001",
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision="9" * 64,
                ),
            ),
        )


def test_build_run_input_manifest_rejects_empty_securities(tmp_path) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    _commit_ready_month(backtest_repo)
    price_repo = _price_repo(tmp_path)

    with pytest.raises(RunInputManifestError) as exc_info:
        build_run_input_manifest(
            project_root=PROJECT_ROOT,
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            strategy=_strategy(),
            submitted_parameters={},
            profile_hash=PROFILE_HASH,
            start_month="2026-07",
            end_month="2026-07",
            base_currency="USD",
            starting_capital=Decimal("10000"),
            securities=(),
        )

    assert exc_info.value.code == "invalid_securities"
