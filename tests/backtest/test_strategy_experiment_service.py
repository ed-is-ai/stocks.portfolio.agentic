from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import cast

import pytest

from app.agents.strategy_experiment import StrategyExperimentAgent
from app.agents.strategy_experiment.agent import StrategyExperimentProposalResult
from app.repositories.backtest_repo import BacktestRepository
from app.schemas.strategy_experiment import (
    ExperimentMetric,
    ExperimentStatus,
    ExperimentVerdict,
    ExpectedDirection,
    StrategyExperimentModelAttemptV1,
    StrategyExperimentApprovalV1,
    StrategyExperimentConclusionV1,
    StrategyExperimentDraftV1,
    StrategyExperimentProposalV1,
    StrategyExperimentV1,
)
from app.services.backtest.backtest_engine import ExitFillEventV1
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.run_input_manifest import (
    PinnedSecurityEvidenceV1,
    RunInputManifestV1,
    build_run_input_manifest_v2,
    build_run_input_manifest_v3,
)
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.skill_discovery import discover_strategies
from app.services.backtest.strategy_experiment_service import StrategyExperimentService
from app.services.backtest.strategy_job import (
    RegimeBenchmarkPinV1,
    RunUniverseSelectionV1,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.strategy_protocol import JsonScalar
from tests.backtest.test_run_input_manifest import _manifest

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
DISCOVERY_ROOT = (
    Path(__file__).parents[1]
    / "fixtures"
    / "backtest-strategies"
    / "discovery"
)


def _base_manifest(parameters: dict[str, object]) -> RunInputManifestV1:
    descriptor = next(
        item
        for item in discover_strategies(DISCOVERY_ROOT).strategies
        if item.strategy_id == "valid-strategy"
    )
    return _manifest(
        strategy_id=descriptor.strategy_id,
        strategy_api_version=descriptor.api_version,
        strategy_source_digest=descriptor.source_digest,
        parameters=parameters,
        securities=(
            PinnedSecurityEvidenceV1(
                security_id="sec-000",
                price_revision="a" * 64,
                action_revision="a" * 64,
            ),
        ),
    )


def _manifest_versions() -> tuple[RunInputManifestV1, ...]:
    profile_hash = "a" * 64
    selection = RunUniverseSelectionV1(
        profile_hash=profile_hash,
        activation_seq=2,
        universe_parameter="symbols",
        canonical_security_ids=("sec-000",),
        run_universe_digest=run_universe_digest(
            ["sec-000"], parameter="symbols", profile_hash=profile_hash
        ),
    )
    v1 = _base_manifest({"symbols": ["sec-000"], "lookback": 20})
    v2 = build_run_input_manifest_v2(
        v1, selection=selection, source_preparation_job_id="prep-1"
    )
    v3_base = _base_manifest(
        {
            "symbols": ["sec-000"],
            "lookback": 20,
            "regime_filter_enabled": True,
            "regime_filter_ma_length": 50,
            "regime_filter_benchmark_security_id": "SPY",
        }
    )
    v3 = build_run_input_manifest_v3(
        v3_base,
        selection=selection,
        source_preparation_job_id="prep-1",
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
    return v1, v2, v3


@pytest.mark.parametrize("baseline", _manifest_versions())
def test_candidate_manifest_preserves_every_pin_for_v1_v2_and_v3(baseline) -> None:
    before = dict(baseline.parameters)
    after = {**before, "lookback": 21}

    candidate = StrategyExperimentService._candidate_manifest(baseline, after)

    baseline_payload = baseline.canonical_payload()
    candidate_payload = candidate.canonical_payload()
    assert type(candidate) is type(baseline)
    assert baseline_payload.pop("parameters") == before
    assert candidate_payload.pop("parameters") == after
    assert candidate_payload == baseline_payload
    assert candidate.execution_contract_digest() == baseline.execution_contract_digest()


class _FakeAgent:
    def __init__(self, proposal: object | None) -> None:
        self.proposal = proposal
        self.calls: list[dict[str, object]] = []

    def propose(self, hypothesis: str, **context: object):
        self.calls.append({"hypothesis": hypothesis, **context})
        if self.proposal is None:
            return None
        return StrategyExperimentProposalResult(
            self.proposal,
            "foundry_local",
            "local-test-model",
            (
                StrategyExperimentModelAttemptV1(
                    model_provider="foundry_local",
                    model_id="local-test-model",
                    outcome="selected",
                ),
            ),
        )


class _FakeRepository:
    def __init__(self, manifest: RunInputManifestV1, *, baseline_status=None) -> None:
        self.manifest = manifest
        self.baseline_status = baseline_status or StrategyJobStatus.COMPLETE
        self.attempts: list[dict[str, object]] = []
        self.created_draft: StrategyExperimentV1 | None = None
        self.create_candidate_calls: list[tuple[object, ...]] = []
        self.result = SimpleNamespace(
            run_input_manifest_digest=manifest.digest(),
            strategy_id=manifest.strategy_id,
            strategy_api_version=manifest.strategy_api_version,
            strategy_source_digest=manifest.strategy_source_digest,
            parameters=dict(manifest.parameters),
            profile_hash=manifest.profile_hash,
            start_month=manifest.start_month,
            end_month=manifest.end_month,
            base_currency=manifest.base_currency,
            starting_capital=manifest.starting_capital,
        )

    def strategy_job(self, _run_id: str):
        return SimpleNamespace(
            id="baseline-1",
            job_type=StrategyJobType.BACKTEST,
            status=self.baseline_status,
            deleted_at=None,
        )

    def backtest_result(self, _run_id: str):
        if self.baseline_status is not StrategyJobStatus.COMPLETE:
            raise ValueError("baseline result is unavailable")
        return self.result

    def run_input_manifest_json(self, _digest: str):
        return self.manifest.canonical_json()

    def append_strategy_experiment_attempt(self, **event: object) -> None:
        self.attempts.append(event)

    def create_strategy_experiment_draft(self, draft, digest: str):
        self.created_draft = StrategyExperimentV1(
            id="experiment-1",
            status=ExperimentStatus.DRAFT,
            draft=draft,
            draft_digest=digest,
            created_at=draft.created_at,
            updated_at=draft.created_at,
        )
        return self.created_draft


def _proposal(
    *, parameter_name: str = "fixed_shares", proposed_value: JsonScalar = 3
) -> StrategyExperimentProposalV1:
    return StrategyExperimentProposalV1(
        parameter_name=parameter_name,
        proposed_value=proposed_value,
        effect_summary="A slightly larger fixed position may improve returns.",
        metric=ExperimentMetric.TOTAL_RETURN,
        expected_direction=ExpectedDirection.HIGHER,
    )


def _service(repository: object, agent: object) -> StrategyExperimentService:
    return StrategyExperimentService(
        cast(BacktestRepository, repository),
        cast(StrategyExperimentAgent, agent),
        skills_root=DISCOVERY_ROOT,
        clock=lambda: NOW,
    )


def test_draft_is_one_declared_parameter_and_does_not_enqueue() -> None:
    manifest = _base_manifest(
        {
            "selected_securities": ["sec-000"],
            "watch_security_id": "sec-000",
            "fixed_shares": 1,
            "max_concurrent_positions": 10,
        }
    )
    repository = _FakeRepository(manifest)
    agent = _FakeAgent(_proposal())
    service = _service(repository, agent)

    outcome = service.draft(baseline_run_id="baseline-1", hypothesis="Test more shares")

    assert outcome.status == "created"
    assert outcome.experiment is not None
    assert outcome.experiment.draft.parameter_name == "fixed_shares"
    assert outcome.experiment.draft.baseline_value == 1
    assert outcome.experiment.draft.proposed_value == 3
    assert outcome.experiment.draft.model_provider == "foundry_local"
    assert outcome.experiment.draft.model_id == "local-test-model"
    assert [
        (attempt.model_provider, attempt.model_id, attempt.outcome)
        for attempt in outcome.experiment.draft.model_attempts
    ] == [("foundry_local", "local-test-model", "selected")]
    assert repository.attempts == []
    assert repository.create_candidate_calls == []
    assert agent.calls[0]["current_values"] == {
        "watch_security_id": "sec-000",
        "fixed_shares": 1,
        "max_concurrent_positions": 10,
    }


@pytest.mark.parametrize(
    ("proposal", "baseline_status", "expected_status"),
    [
        (None, StrategyJobStatus.COMPLETE, "unavailable"),
        ({
            "parameter_name": "selected_securities",
            "proposed_value": ["sec-001"],
            "effect_summary": "Change the selected universe.",
            "metric": "total_return",
            "expected_direction": "higher",
        }, StrategyJobStatus.COMPLETE, "rejected"),
        (_proposal(proposed_value=101), StrategyJobStatus.COMPLETE, "rejected"),
        (_proposal(), StrategyJobStatus.FAILED, "rejected"),
    ],
)
def test_unusable_proposal_or_baseline_creates_no_draft_and_audits_attempt(
    proposal, baseline_status, expected_status
) -> None:
    manifest = _base_manifest(
        {
            "selected_securities": ["sec-000"],
            "watch_security_id": "sec-000",
            "fixed_shares": 1,
            "max_concurrent_positions": 10,
        }
    )
    repository = _FakeRepository(manifest, baseline_status=baseline_status)
    service = _service(repository, _FakeAgent(proposal))

    outcome = service.draft(baseline_run_id="baseline-1", hypothesis="Try a change")

    assert outcome.status == expected_status
    assert outcome.experiment is None
    assert repository.created_draft is None
    assert len(repository.attempts) == 1


def _exit_event(sequence: int) -> ExitFillEventV1:
    return ExitFillEventV1(
        security_id="sec-000",
        signal_session=date(2026, 6, 1),
        fill_session=date(2026, 6, 2),
        rule_id="exit",
        shares=1,
        fill_price_native=Decimal("10"),
        fill_currency="USD",
        fill_quote_unit="USD",
        proceeds_base=Decimal("10"),
        cost_basis_base=Decimal("9"),
        realized_pnl_base=Decimal("1"),
        sequence=sequence,
    )


def _experiment_for_reconciliation(
    manifest: RunInputManifestV1, *, candidate_run_id: str = "candidate-1"
) -> StrategyExperimentV1:
    draft = StrategyExperimentDraftV1(
        baseline_run_id="baseline-1",
        hypothesis="Try more shares",
        strategy_id=manifest.strategy_id,
        strategy_api_version=manifest.strategy_api_version,
        strategy_source_digest=manifest.strategy_source_digest,
        parameter_name="lookback",
        baseline_value=20,
        proposed_value=21,
        effect_summary="Test whether more lookback changes the return.",
        metric=ExperimentMetric.TOTAL_RETURN,
        expected_direction=ExpectedDirection.HIGHER,
        baseline_manifest_digest=manifest.digest(),
        baseline_manifest_json=manifest.canonical_json(),
        model_provider="foundry_local",
        model_id="local-test-model",
        created_at=NOW,
    )
    digest = manifest_digest({"draft": draft.model_dump(mode="json")})
    return StrategyExperimentV1(
        id="experiment-1",
        status=ExperimentStatus.APPROVED,
        draft=draft,
        draft_digest=digest,
        candidate_run_id=candidate_run_id,
        approval=StrategyExperimentApprovalV1(approved_at=NOW, draft_digest=digest),
        created_at=NOW,
        updated_at=NOW,
    )


class _ReconcileRepository:
    def __init__(
        self,
        experiment: StrategyExperimentV1,
        baseline_manifest: RunInputManifestV1,
        candidate_manifest: RunInputManifestV1,
        *,
        candidate_status: StrategyJobStatus = StrategyJobStatus.COMPLETE,
        values: tuple[float | None, float | None] = (0.1, 0.2),
        eligible: bool = True,
        closed_trades: bool = True,
    ) -> None:
        self.experiment = experiment
        self.candidate_status = candidate_status
        self.eligible = eligible
        self.finalized: list[tuple[object, ...]] = []
        self.manifests = {
            baseline_manifest.digest(): baseline_manifest.canonical_json(),
            candidate_manifest.digest(): candidate_manifest.canonical_json(),
        }
        events = (_exit_event(1),) if closed_trades else ()
        self.results = {
            "baseline-1": SimpleNamespace(
                metrics=SimpleNamespace(total_return=values[0]),
                events=events,
                run_input_manifest_digest=baseline_manifest.digest(),
                execution_contract_digest=baseline_manifest.execution_contract_digest(),
            ),
            "candidate-1": SimpleNamespace(
                metrics=SimpleNamespace(total_return=values[1]),
                events=events,
                run_input_manifest_digest=candidate_manifest.digest(),
                execution_contract_digest=candidate_manifest.execution_contract_digest(),
            ),
        }

    def strategy_experiment_for_candidate(self, _candidate_run_id: str):
        return self.experiment

    def strategy_job(self, _candidate_run_id: str):
        return SimpleNamespace(status=self.candidate_status)

    def strategy_run(self, run_id: str):
        return SimpleNamespace(
            run_input_manifest_digest=self.results[run_id].run_input_manifest_digest
        )

    def backtest_result(self, run_id: str):
        if self.candidate_status is not StrategyJobStatus.COMPLETE and run_id == "candidate-1":
            raise ValueError("candidate has no verified result")
        return self.results[run_id]

    def run_input_manifest_json(self, digest: str):
        return self.manifests.get(digest)

    def is_comparable(self, *_args: object, **_kwargs: object):
        return SimpleNamespace(eligible=self.eligible, reason=None)

    def finalize_strategy_experiment(self, *args: object):
        self.finalized.append(args)
        return SimpleNamespace(conclusion=args[3], comparison=args[2])


def test_terminal_reconciliation_records_supported_metric_and_both_samples() -> None:
    baseline = _base_manifest({"symbols": ["sec-000"], "lookback": 20})
    candidate = StrategyExperimentService._candidate_manifest(
        baseline, {"symbols": ["sec-000"], "lookback": 21}
    )
    experiment = _experiment_for_reconciliation(baseline)
    repository = _ReconcileRepository(experiment, baseline, candidate)
    service = _service(repository, _FakeAgent(None))

    result = service.reconcile_candidate("candidate-1")

    assert result is not None
    conclusion = cast(StrategyExperimentConclusionV1, repository.finalized[0][3])
    assert conclusion.verdict is ExperimentVerdict.SUPPORTED
    assert conclusion.comparison.baseline_value == 0.1
    assert conclusion.comparison.candidate_value == 0.2
    assert conclusion.comparison.baseline_closed_trades == 1
    assert conclusion.comparison.candidate_closed_trades == 1


@pytest.mark.parametrize(
    ("candidate_status", "values", "eligible", "closed_trades", "reason"),
    [
        (StrategyJobStatus.FAILED, (0.1, 0.2), True, True, "result_unavailable"),
        (StrategyJobStatus.COMPLETE, (0.1, 0.2), False, True, "ineligible"),
        (StrategyJobStatus.COMPLETE, (0.1, 0.1), True, True, "equal_metric"),
        (StrategyJobStatus.COMPLETE, (0.1, None), True, True, "metric_unavailable"),
        (StrategyJobStatus.COMPLETE, (0.1, 0.2), True, False, "zero_closed_trades"),
    ],
)
def test_failed_or_ineligible_pair_is_inconclusive(
    candidate_status, values, eligible, closed_trades, reason
) -> None:
    baseline = _base_manifest({"symbols": ["sec-000"], "lookback": 20})
    candidate = StrategyExperimentService._candidate_manifest(
        baseline, {"symbols": ["sec-000"], "lookback": 21}
    )
    experiment = _experiment_for_reconciliation(baseline)
    repository = _ReconcileRepository(
        experiment,
        baseline,
        candidate,
        candidate_status=candidate_status,
        values=values,
        eligible=eligible,
        closed_trades=closed_trades,
    )
    service = _service(repository, _FakeAgent(None))

    service.reconcile_candidate("candidate-1")

    conclusion = cast(StrategyExperimentConclusionV1, repository.finalized[0][3])
    assert conclusion.verdict is ExperimentVerdict.INCONCLUSIVE
    assert conclusion.comparison.eligibility_reason == reason
    assert "future performance" in " ".join(conclusion.comparison.limitations).lower()
    if candidate_status is StrategyJobStatus.FAILED:
        assert conclusion.comparison.baseline_manifest_digest == baseline.digest()
        assert conclusion.comparison.candidate_manifest_digest == candidate.digest()


def test_transient_comparison_storage_error_is_left_pending_for_retry() -> None:
    baseline = _base_manifest({"symbols": ["sec-000"], "lookback": 20})
    candidate = StrategyExperimentService._candidate_manifest(
        baseline, {"symbols": ["sec-000"], "lookback": 21}
    )
    experiment = _experiment_for_reconciliation(baseline)
    repository = _ReconcileRepository(experiment, baseline, candidate)
    repository.is_comparable = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")
    )
    service = _service(repository, _FakeAgent(None))

    with pytest.raises(sqlite3.OperationalError):
        service.reconcile_candidate("candidate-1")

    assert repository.finalized == []
