from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.schemas.strategy_experiment import (
    ExperimentMetric,
    ExperimentStatus,
    ExpectedDirection,
    StrategyExperimentApprovalV1,
    StrategyExperimentDraftV1,
    StrategyExperimentModelAttemptV1,
)
from app.services.backtest.run_input_manifest import (
    PinnedSecurityEvidenceV1,
    RunInputManifestV1,
    RunInputManifestV2,
    build_run_input_manifest_v2,
    build_run_input_manifest_v3,
)
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.strategy_job import (
    JobFailureCode,
    RegimeBenchmarkPinV1,
    RunUniverseSelectionV1,
)
from app.services.backtest.strategy_experiment_service import StrategyExperimentService
from tests.backtest.test_run_input_manifest import _manifest
from tests.backtest.test_strategy_job_repository import _seed_profile

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
PROFILE_HASH = "a" * 64


def _repo(path: Path) -> BacktestRepository:
    repository = BacktestRepository(
        db.make_connect(lambda: path),
        instant_clock=lambda: NOW,
    )
    repository.ensure_schema()
    _seed_profile(path)
    return repository


def _manifests() -> tuple[RunInputManifestV1, ...]:
    security = PinnedSecurityEvidenceV1(
        security_id="sec-000",
        price_revision="a" * 64,
        action_revision="a" * 64,
    )
    base = _manifest(
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest="7" * 64,
        parameters={"symbols": ["sec-000"], "lookback": 20},
        profile_hash=PROFILE_HASH,
        securities=(security,),
    )
    selection = RunUniverseSelectionV1(
        profile_hash=PROFILE_HASH,
        activation_seq=1,
        universe_parameter="symbols",
        canonical_security_ids=("sec-000",),
        run_universe_digest=run_universe_digest(
            ["sec-000"], parameter="symbols", profile_hash=PROFILE_HASH
        ),
    )
    v2 = build_run_input_manifest_v2(
        base, selection=selection, source_preparation_job_id="preparation-1"
    )
    v3_base = _manifest(
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest="7" * 64,
        parameters={
            "symbols": ["sec-000"],
            "lookback": 20,
            "regime_filter_enabled": True,
            "regime_filter_ma_length": 50,
            "regime_filter_benchmark_security_id": "SPY",
        },
        profile_hash=PROFILE_HASH,
        securities=(security,),
    )
    v3 = build_run_input_manifest_v3(
        v3_base,
        selection=selection,
        source_preparation_job_id="preparation-1",
        regime_benchmark=RegimeBenchmarkPinV1(
            security_id="SPY",
            identity_registry_revision="1" * 64,
            alias_revision="2" * 64,
            price_revision="3" * 64,
            action_revision="3" * 64,
            evidence_digest="4" * 64,
            request_start=date(1999, 1, 1),
            request_end=date(2026, 10, 1),
            session_policy="canonical_exchange_sessions_v2",
            calendar_mic="XNYS",
            calendar_session_table_digest="5" * 64,
            price_plane_policy_version="HistoricalMarketPlanesV1",
        ),
    )
    return base, v2, v3


def _seed_baseline(
    repository: BacktestRepository, path: Path, manifest: RunInputManifestV1
) -> None:
    manifest_version = manifest.schema_version
    parent_id = None
    source_preparation_job_id = getattr(manifest, "source_preparation_job_id", None)
    selection = getattr(manifest, "universe_selection", None)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, parent_job_id, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES ('baseline-1', 'backtest', 'complete', ?, 1, 3, ?, ?)""",
            (parent_id, NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO run_input_manifests (
                   digest, execution_contract_digest, canonical_manifest_json,
                   created_at, manifest_version
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                manifest.digest(),
                manifest.execution_contract_digest(),
                manifest.canonical_json(),
                NOW.isoformat(),
                manifest_version,
            ),
        )
        connection.execute(
            """INSERT INTO strategy_runs (
                   id, strategy_id, strategy_api_version, strategy_source_digest,
                   parameters_json, profile_hash, start_month, end_month,
                   ordered_month_digest, base_currency, starting_capital,
                   run_input_manifest_digest, execution_contract_digest,
                   manifest_version, run_universe_digest, source_preparation_job_id,
                   selection_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "baseline-1",
                manifest.strategy_id,
                manifest.strategy_api_version,
                manifest.strategy_source_digest,
                json.dumps(
                    dict(manifest.parameters), sort_keys=True, separators=(",", ":")
                ),
                manifest.profile_hash,
                manifest.start_month,
                manifest.end_month,
                manifest.ordered_month_digest,
                manifest.base_currency,
                str(manifest.starting_capital),
                manifest.digest(),
                manifest.execution_contract_digest(),
                manifest.schema_version,
                None if selection is None else selection.run_universe_digest,
                source_preparation_job_id,
                None if selection is None else selection.model_dump_json(),
                NOW.isoformat(),
            ),
        )
    repository.backtest_result = lambda _run_id: SimpleNamespace(  # type: ignore[method-assign]
        run_input_manifest_digest=manifest.digest(),
        strategy_id=manifest.strategy_id,
        strategy_api_version=manifest.strategy_api_version,
        strategy_source_digest=manifest.strategy_source_digest,
    )


def _draft(manifest: RunInputManifestV1) -> StrategyExperimentDraftV1:
    return StrategyExperimentDraftV1(
        baseline_run_id="baseline-1",
        hypothesis="Increase lookback by one.",
        strategy_id=manifest.strategy_id,
        strategy_api_version=manifest.strategy_api_version,
        strategy_source_digest=manifest.strategy_source_digest,
        parameter_name="lookback",
        baseline_value=20,
        proposed_value=21,
        effect_summary="Test a slightly longer signal window.",
        metric=ExperimentMetric.TOTAL_RETURN,
        expected_direction=ExpectedDirection.HIGHER,
        baseline_manifest_digest=manifest.digest(),
        baseline_manifest_json=manifest.canonical_json(),
        model_provider="foundry_local",
        model_id="local-test-model",
        model_attempts=(
            StrategyExperimentModelAttemptV1(
                model_provider="anthropic",
                model_id="claude-sonnet-5",
                outcome="no_valid_proposal",
            ),
            StrategyExperimentModelAttemptV1(
                model_provider="foundry_local",
                model_id="local-test-model",
                outcome="selected",
            ),
        ),
        created_at=NOW,
    )


def test_pending_strategy_experiments_returns_only_bounded_live_drafts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repository = _repo(path)
    manifest = _manifests()[0]
    _seed_baseline(repository, path, manifest)
    first_draft = _draft(manifest)
    later_draft = first_draft.model_copy(
        update={"created_at": NOW + timedelta(seconds=1)}
    )
    first = repository.create_strategy_experiment_draft(first_draft, "d" * 64)
    later = repository.create_strategy_experiment_draft(later_draft, "e" * 64)
    repository.discard_strategy_experiment(later.id, later.draft_digest)

    pending = repository.pending_strategy_experiments(limit=1)

    assert len(pending) == 1
    assert pending[0].id == first.id
    assert pending[0].status.value == "draft"
    landing_details = StrategyExperimentService(repository).pending_details(limit=1)
    assert len(landing_details) == 1
    assert landing_details[0].experiment.id == first.id
    assert '"strategy_id": "momentum_v1"' in landing_details[0].locked_manifest_json


def test_pending_details_omit_experiment_that_settles_after_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        pending_strategy_experiments=lambda *, limit: (SimpleNamespace(id="draft-1"),)
    )
    service = StrategyExperimentService(repository, agent=SimpleNamespace())
    settled = SimpleNamespace(
        experiment=SimpleNamespace(status=ExperimentStatus.APPROVED)
    )
    monkeypatch.setattr(service, "detail", lambda _experiment_id: settled)

    assert service.pending_details(limit=1) == ()


@pytest.mark.parametrize("manifest", _manifests())
def test_approval_clones_manifest_and_concurrent_retry_returns_one_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest
) -> None:
    path = tmp_path / "backtest.db"
    repository = _repo(path)
    _seed_baseline(repository, path, manifest)
    draft = _draft(manifest)
    draft_digest = "d" * 64
    experiment = repository.create_strategy_experiment_draft(draft, draft_digest)
    assert experiment.draft.model_attempts == draft.model_attempts
    candidate_parameters = {**dict(manifest.parameters), "lookback": 21}
    candidate_manifest = type(manifest).model_validate(
        {**manifest.model_dump(mode="python"), "parameters": candidate_parameters}
    )
    approval = StrategyExperimentApprovalV1(approved_at=NOW, draft_digest=draft_digest)
    barrier = Barrier(2)

    def approve():
        barrier.wait()
        return repository.approve_strategy_experiment_candidate(
            experiment.id,
            draft_digest,
            candidate_manifest.canonical_json(),
            approval,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = tuple(pool.map(lambda _index: approve(), range(2)))

    candidate_ids = {response[1].id for response in responses}
    assert len(candidate_ids) == 1
    candidate_id = candidate_ids.pop()
    assert responses[0][0].status is ExperimentStatus.APPROVED
    assert responses[1][0].candidate_run_id == candidate_id

    with repository._connect() as connection:
        job_rows = connection.execute(
            "SELECT id, parent_job_id FROM strategy_jobs WHERE job_type='backtest' ORDER BY enqueue_seq"
        ).fetchall()
        run = connection.execute(
            """SELECT run_input_manifest_digest, parameters_json, profile_hash,
                      start_month, end_month, base_currency, starting_capital,
                      execution_contract_digest, manifest_version,
                      run_universe_digest, source_preparation_job_id, selection_json
               FROM strategy_runs WHERE id=?""",
            (candidate_id,),
        ).fetchone()
    assert [row[0] for row in job_rows] == ["baseline-1", candidate_id]
    assert job_rows[1][1] == "baseline-1"
    assert run is not None
    stored_candidate = repository.run_input_manifest_json(str(run[0]))
    assert stored_candidate == candidate_manifest.canonical_json()
    baseline_payload = manifest.canonical_payload()
    candidate_payload = candidate_manifest.canonical_payload()
    baseline_payload.pop("parameters")
    candidate_payload.pop("parameters")
    assert candidate_payload == baseline_payload
    assert json.loads(str(run[1])) == candidate_parameters
    selection = (
        manifest.universe_selection
        if isinstance(manifest, RunInputManifestV2)
        else None
    )
    source_preparation_job_id = (
        manifest.source_preparation_job_id
        if isinstance(manifest, RunInputManifestV2)
        else None
    )
    assert tuple(run[2:]) == (
        manifest.profile_hash,
        manifest.start_month,
        manifest.end_month,
        manifest.base_currency,
        str(manifest.starting_capital),
        manifest.execution_contract_digest(),
        manifest.schema_version,
        None if selection is None else selection.run_universe_digest,
        source_preparation_job_id,
        None if selection is None else selection.model_dump_json(),
    )
    audit = repository.strategy_experiment_audit(experiment.id)
    assert [event.event_type for event in audit] == [
        "draft_created",
        "candidate_approved_and_enqueued",
    ]
    assert audit[0].details["model_provider"] == "foundry_local"
    assert audit[0].details["model_attempts"] == [
        attempt.model_dump(mode="json") for attempt in draft.model_attempts
    ]


def test_terminal_approved_candidate_is_discoverable_for_reconciliation_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repository = _repo(path)
    manifest = _manifests()[0]
    _seed_baseline(repository, path, manifest)
    draft_digest = "e" * 64
    experiment = repository.create_strategy_experiment_draft(
        _draft(manifest), draft_digest
    )
    candidate_manifest = type(manifest).model_validate(
        {
            **manifest.model_dump(mode="python"),
            "parameters": {**dict(manifest.parameters), "lookback": 21},
        }
    )
    _approved, candidate = repository.approve_strategy_experiment_candidate(
        experiment.id,
        draft_digest,
        candidate_manifest.canonical_json(),
        StrategyExperimentApprovalV1(approved_at=NOW, draft_digest=draft_digest),
    )
    assert repository.pending_strategy_experiment_reconciliations() == ()

    claim = repository.claim_next_strategy_job()
    assert claim is not None and claim.job.id == candidate.id
    repository.fail_claimed_strategy_job(
        candidate.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.WORKER_INTERRUPTED,
        failed_month=None,
        detail="test terminal state",
    )

    assert repository.pending_strategy_experiment_reconciliations() == (candidate.id,)


def test_rejected_draft_attempt_audit_is_readable(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "backtest.db")
    repository.append_strategy_experiment_attempt(
        baseline_run_id="baseline-missing",
        event_type="draft_rejected",
        details={"reason": "baseline unavailable", "hypothesis": "Try a change"},
    )

    (event,) = repository.strategy_experiment_attempt_audit()
    assert event.experiment_id is None
    assert event.baseline_run_id == "baseline-missing"
    assert event.details["reason"] == "baseline unavailable"


def test_baseline_options_skip_a_damaged_recent_result(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repository = _repo(path)
    manifest = _manifests()[0]
    _seed_baseline(repository, path, manifest)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, parent_job_id, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES ('baseline-bad', 'backtest', 'complete', NULL, 2, 3, ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO strategy_runs (
                   id, strategy_id, strategy_api_version, strategy_source_digest,
                   parameters_json, profile_hash, start_month, end_month,
                   ordered_month_digest, base_currency, starting_capital,
                   run_input_manifest_digest, execution_contract_digest,
                   manifest_version, run_universe_digest, source_preparation_job_id,
                   selection_json, created_at
               ) SELECT 'baseline-bad', strategy_id, strategy_api_version,
                         strategy_source_digest, parameters_json, profile_hash,
                         start_month, end_month, ordered_month_digest, base_currency,
                         starting_capital, run_input_manifest_digest,
                         execution_contract_digest, manifest_version,
                         run_universe_digest, source_preparation_job_id,
                         selection_json, created_at
                  FROM strategy_runs WHERE id='baseline-1'"""
        )

    def result(run_id: str):
        if run_id == "baseline-bad":
            raise BacktestIntegrityError("damaged result")
        return SimpleNamespace(strategy_id=manifest.strategy_id)

    repository.backtest_result = result  # type: ignore[method-assign]

    (option,) = repository.strategy_experiment_baselines()
    assert option.id == "baseline-1"
