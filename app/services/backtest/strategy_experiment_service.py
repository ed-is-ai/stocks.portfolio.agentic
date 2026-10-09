"""Lifecycle boundary for fixed-input Strategy parameter experiments."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
from typing import Callable, Literal, cast

from app.agents.strategy_experiment import StrategyExperimentAgent
from app.core import config
from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
)
from app.schemas.strategy_experiment import (
    ExperimentMetric,
    ExperimentStatus,
    ExperimentVerdict,
    ExpectedDirection,
    StrategyExperimentApprovalV1,
    StrategyExperimentComparisonV1,
    StrategyExperimentConclusionV1,
    StrategyExperimentDetailV1,
    StrategyExperimentDraftOutcomeV1,
    StrategyExperimentDraftV1,
    StrategyExperimentProposalV1,
    StrategyExperimentV1,
)
from app.services.backtest.backtest_engine import (
    ExitFillEventV1,
    TerminalSettlementEventV1,
)
from app.services.backtest.canonical_manifest import jsonable, manifest_digest
from app.services.backtest.run_input_manifest import (
    RunInputManifestV1,
    read_run_input_manifest,
)
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    discover_strategies,
)
from app.services.backtest.strategy_job import (
    StrategyJobNotFound,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.strategy_protocol import (
    JsonScalar,
    JsonValue,
    validate_strategy_parameters,
)

logger = logging.getLogger(__name__)

_BASE_LIMITATION = (
    "Historical comparison only; it does not establish statistical significance "
    "or predict future performance."
)


class StrategyExperimentService:
    """Validate drafts, require approval, enqueue once, and conclude on terminal."""

    def __init__(
        self,
        repository: BacktestRepository,
        agent: StrategyExperimentAgent | None = None,
        *,
        skills_root=None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._agent = agent or StrategyExperimentAgent()
        self._skills_root = skills_root or config.SKILLS_DIR
        self._clock = clock

    def draft(
        self, *, baseline_run_id: str, hypothesis: str
    ) -> StrategyExperimentDraftOutcomeV1:
        hypothesis = hypothesis.strip()
        if not baseline_run_id.strip() or not hypothesis or len(hypothesis) > 2000:
            return self._no_draft(
                "rejected",
                "Enter a baseline run and a hypothesis of at most 2,000 characters.",
                baseline_run_id=baseline_run_id or None,
                hypothesis=hypothesis[:2000],
            )
        try:
            result, manifest, descriptor = self._verified_baseline(baseline_run_id)
        except Exception as exc:
            logger.info("strategy experiment baseline rejected: %s", exc)
            return self._no_draft(
                "rejected",
                "The selected baseline is missing, incomplete, corrupt, or no longer source-compatible.",
                baseline_run_id=baseline_run_id,
                hypothesis=hypothesis,
            )

        current_values = self._declared_values(manifest, descriptor)
        proposal = self._agent.propose(
            hypothesis,
            strategy_id=descriptor.strategy_id,
            parameters={
                parameter.name: parameter.model_dump(mode="json")
                for parameter in descriptor.parameters
            },
            current_values=current_values,
        )
        if proposal is None:
            return self._no_draft(
                "unavailable",
                "The local proposal model is unavailable or returned no valid structured proposal.",
                baseline_run_id=baseline_run_id,
                hypothesis=hypothesis,
            )
        try:
            proposal = StrategyExperimentProposalV1.model_validate(proposal)
            candidate_parameters = self._validated_candidate_parameters(
                manifest, descriptor, proposal
            )
        except Exception as exc:
            logger.info("strategy experiment proposal rejected: %s", exc)
            return self._no_draft(
                "rejected",
                "The proposal must change one declared Strategy parameter to a valid, different value and select a canonical metric.",
                baseline_run_id=baseline_run_id,
                hypothesis=hypothesis,
            )
        baseline_value = cast(JsonScalar, manifest.parameters[proposal.parameter_name])
        proposed_value = cast(JsonScalar, candidate_parameters[proposal.parameter_name])
        now = self._clock()
        draft = StrategyExperimentDraftV1(
            baseline_run_id=baseline_run_id,
            hypothesis=hypothesis,
            strategy_id=descriptor.strategy_id,
            strategy_api_version=descriptor.api_version,
            strategy_source_digest=manifest.strategy_source_digest,
            parameter_name=proposal.parameter_name,
            baseline_value=baseline_value,
            proposed_value=proposed_value,
            effect_summary=proposal.effect_summary,
            metric=proposal.metric,
            expected_direction=proposal.expected_direction,
            baseline_manifest_digest=manifest.digest(),
            baseline_manifest_json=manifest.canonical_json(),
            model_id=self._agent.model_id,
            created_at=now,
        )
        draft_digest = manifest_digest(
            {
                "schema": "strategy-experiment-draft-digest.v1",
                "draft": draft.model_dump(mode="json"),
            }
        )
        try:
            experiment = self._repository.create_strategy_experiment_draft(
                draft, draft_digest
            )
        except Exception:
            logger.warning("strategy experiment draft persistence failed", exc_info=True)
            return self._no_draft(
                "rejected",
                "The verified draft could not be stored, so no experiment was created.",
                baseline_run_id=baseline_run_id,
                hypothesis=hypothesis,
            )
        return StrategyExperimentDraftOutcomeV1(status="created", experiment=experiment)

    def list(self) -> tuple[StrategyExperimentV1, ...]:
        return self._repository.list_strategy_experiments()

    def attempt_audit(self):
        return self._repository.strategy_experiment_attempt_audit()

    def detail(self, experiment_id: str) -> StrategyExperimentDetailV1:
        experiment = self._repository.strategy_experiment(experiment_id)
        manifest = read_run_input_manifest(experiment.draft.baseline_manifest_json)
        candidate_status = None
        if experiment.candidate_run_id is not None:
            candidate_status = self._repository.strategy_job(
                experiment.candidate_run_id
            ).status.value
        payload = jsonable(manifest.canonical_payload())
        return StrategyExperimentDetailV1(
            experiment=experiment,
            locked_manifest_json=json.dumps(
                payload, sort_keys=True, indent=2, ensure_ascii=False
            ),
            candidate_status=candidate_status,
            audit_events=self._repository.strategy_experiment_audit(experiment_id),
        )

    def discard(self, experiment_id: str, draft_digest: str) -> StrategyExperimentV1:
        return self._repository.discard_strategy_experiment(experiment_id, draft_digest)

    def approve(
        self,
        experiment_id: str,
        draft_digest: str,
        *,
        actor: Literal["local_user", "api_token"] = "local_user",
    ) -> tuple[StrategyExperimentV1, object]:
        experiment = self._repository.strategy_experiment(experiment_id)
        if experiment.draft_digest != draft_digest:
            raise ValueError("The reviewed draft changed. Reload it before approval.")
        if experiment.candidate_run_id is not None:
            return experiment, self._repository.strategy_job(experiment.candidate_run_id)
        _, manifest, descriptor = self._verified_baseline(
            experiment.draft.baseline_run_id
        )
        if (
            manifest.canonical_json() != experiment.draft.baseline_manifest_json
            or manifest.digest() != experiment.draft.baseline_manifest_digest
            or descriptor.strategy_id != experiment.draft.strategy_id
            or descriptor.api_version != experiment.draft.strategy_api_version
            or not descriptor.accepts_source_digest(
                experiment.draft.strategy_source_digest
            )
        ):
            raise ValueError("The baseline or Strategy identity changed after review.")
        proposal = StrategyExperimentProposalV1(
            parameter_name=experiment.draft.parameter_name,
            proposed_value=experiment.draft.proposed_value,
            effect_summary=experiment.draft.effect_summary,
            metric=experiment.draft.metric,
            expected_direction=experiment.draft.expected_direction,
        )
        parameters = self._validated_candidate_parameters(manifest, descriptor, proposal)
        candidate_manifest = self._candidate_manifest(manifest, parameters)
        approval = StrategyExperimentApprovalV1(
            approved_at=self._clock(), draft_digest=draft_digest, actor=actor
        )
        return self._repository.approve_strategy_experiment_candidate(
            experiment_id,
            draft_digest,
            candidate_manifest.canonical_json(),
            approval,
        )

    def reconcile_candidate(self, candidate_run_id: str) -> StrategyExperimentV1 | None:
        experiment = self._repository.strategy_experiment_for_candidate(candidate_run_id)
        if experiment is None or experiment.status is not ExperimentStatus.APPROVED:
            return experiment
        candidate_job = self._repository.strategy_job(candidate_run_id)
        if candidate_job.status not in {
            StrategyJobStatus.COMPLETE,
            StrategyJobStatus.FAILED,
            StrategyJobStatus.CANCELLED,
        }:
            return experiment

        limitations = [_BASE_LIMITATION]
        baseline_result = self._verified_result(experiment.draft.baseline_run_id)
        candidate_result = (
            self._verified_result(candidate_run_id)
            if candidate_job.status is StrategyJobStatus.COMPLETE
            else None
        )
        if baseline_result is None:
            limitations.append("The baseline result is missing or failed integrity verification.")
        if candidate_job.status is not StrategyJobStatus.COMPLETE:
            limitations.append(
                f"The candidate job ended as {candidate_job.status.value}; no win/loss conclusion is available."
            )
        elif candidate_result is None:
            limitations.append("The candidate result is missing or failed integrity verification.")

        eligibility_reason: str | None = None
        baseline_manifest_digest: str | None = experiment.draft.baseline_manifest_digest
        candidate_manifest_digest: str | None = None
        execution_contract_digest = read_run_input_manifest(
            experiment.draft.baseline_manifest_json
        ).execution_contract_digest()
        verdict = ExperimentVerdict.INCONCLUSIVE
        baseline_value = self._result_metric(baseline_result, experiment.draft.metric)
        candidate_value = self._result_metric(candidate_result, experiment.draft.metric)
        baseline_trades = self._closed_trade_count(baseline_result)
        candidate_trades = self._closed_trade_count(candidate_result)

        try:
            candidate_run = self._repository.strategy_run(candidate_run_id)
        except (BacktestIntegrityError, StrategyJobNotFound, ValueError):
            candidate_run = None
        if candidate_run is not None:
            candidate_manifest_digest = candidate_run.run_input_manifest_digest
        if baseline_result is not None:
            baseline_manifest_digest = baseline_result.run_input_manifest_digest
        if candidate_result is not None:
            candidate_manifest_digest = candidate_result.run_input_manifest_digest
        if baseline_result is not None and candidate_result is not None:
            try:
                self._verify_pair_manifest(experiment, baseline_result, candidate_result)
            except (
                BacktestIntegrityError,
                StrategyJobNotFound,
                ValueError,
                TypeError,
            ):
                eligibility_reason = "experiment_manifest_mismatch"
                limitations.append(
                    "The candidate does not preserve every baseline manifest field except the approved parameter."
                )
            else:
                try:
                    eligibility = self._repository.is_comparable(
                        experiment.draft.baseline_run_id,
                        candidate_run_id,
                        left_result=baseline_result,
                        right_result=candidate_result,
                    )
                    if not eligibility.eligible:
                        eligibility_reason = (
                            eligibility.reason.value if eligibility.reason else "ineligible"
                        )
                        limitations.append(
                            "The canonical comparison service found the result pair ineligible."
                        )
                except (
                    BacktestIntegrityError,
                    StrategyJobNotFound,
                    ValueError,
                    TypeError,
                ):
                    eligibility_reason = "comparison_integrity_error"
                    limitations.append(
                        "Canonical comparison eligibility could not be verified."
                    )
                if eligibility_reason is None:
                    if not baseline_trades or not candidate_trades:
                        eligibility_reason = "zero_closed_trades"
                        limitations.append(
                            "At least one run has zero closed trades, so the result is inconclusive."
                        )
                    elif baseline_value is None or candidate_value is None:
                        eligibility_reason = "metric_unavailable"
                        limitations.append(
                            "The approved metric is unavailable for one or both runs."
                        )
                    elif baseline_value == candidate_value:
                        eligibility_reason = "equal_metric"
                        limitations.append(
                            "The approved metric has the same value in both runs."
                        )
                    else:
                        improved = (
                            candidate_value > baseline_value
                            if experiment.draft.expected_direction
                            is ExpectedDirection.HIGHER
                            else candidate_value < baseline_value
                        )
                        verdict = (
                            ExperimentVerdict.SUPPORTED
                            if improved
                            else ExperimentVerdict.CONTRADICTED
                        )
        else:
            eligibility_reason = eligibility_reason or "result_unavailable"

        comparison = StrategyExperimentComparisonV1(
            baseline_run_id=experiment.draft.baseline_run_id,
            candidate_run_id=candidate_run_id,
            metric=experiment.draft.metric,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            baseline_closed_trades=baseline_trades,
            candidate_closed_trades=candidate_trades,
            eligibility_reason=eligibility_reason,
            baseline_manifest_digest=baseline_manifest_digest,
            candidate_manifest_digest=candidate_manifest_digest,
            execution_contract_digest=execution_contract_digest,
            limitations=tuple(dict.fromkeys(limitations)),
        )
        summary = {
            ExperimentVerdict.SUPPORTED: "The candidate moved the approved historical metric in the expected direction.",
            ExperimentVerdict.CONTRADICTED: "The candidate moved the approved historical metric opposite to the expected direction.",
            ExperimentVerdict.INCONCLUSIVE: "The available evidence does not support a win/loss conclusion.",
        }[verdict]
        conclusion = StrategyExperimentConclusionV1(
            verdict=verdict,
            summary=summary,
            concluded_at=self._clock(),
            comparison=comparison,
        )
        return self._repository.finalize_strategy_experiment(
            experiment.id, candidate_run_id, comparison, conclusion
        )

    def _verified_baseline(self, run_id: str):
        job = self._repository.strategy_job(run_id)
        if (
            job.job_type is not StrategyJobType.BACKTEST
            or job.status is not StrategyJobStatus.COMPLETE
            or job.deleted_at is not None
        ):
            raise ValueError("baseline is not a complete live backtest")
        result = self._repository.backtest_result(run_id)
        raw = self._repository.run_input_manifest_json(result.run_input_manifest_digest)
        if raw is None:
            raise ValueError("baseline manifest is missing")
        manifest = read_run_input_manifest(raw)
        if (
            not manifest.accepts_stored_digest(result.run_input_manifest_digest)
            or result.strategy_id != manifest.strategy_id
            or result.strategy_api_version != manifest.strategy_api_version
            or result.strategy_source_digest != manifest.strategy_source_digest
            or result.parameters != dict(manifest.parameters)
            or result.profile_hash != manifest.profile_hash
            or result.start_month != manifest.start_month
            or result.end_month != manifest.end_month
            or result.base_currency != manifest.base_currency
            or result.starting_capital != manifest.starting_capital
        ):
            raise ValueError("baseline result does not match its manifest")
        descriptor = next(
            (
                candidate
                for candidate in discover_strategies(self._skills_root).strategies
                if candidate.strategy_id == result.strategy_id
            ),
            None,
        )
        if (
            descriptor is None
            or descriptor.api_version != result.strategy_api_version
            or not descriptor.accepts_source_digest(result.strategy_source_digest)
        ):
            raise ValueError("baseline Strategy source is not discoverable")
        self._declared_values(manifest, descriptor)
        return result, manifest, descriptor

    @staticmethod
    def _declared_values(
        manifest: RunInputManifestV1, descriptor: StrategyDescriptorV1
    ) -> dict[str, object]:
        declared_names = {parameter.name for parameter in descriptor.parameters}
        submitted = {
            key: value for key, value in manifest.parameters.items() if key in declared_names
        }
        validation = validate_strategy_parameters(
            descriptor.parameters,
            cast(Mapping[str, JsonValue], submitted),
            apply_defaults=False,
        )
        if isinstance(validation, tuple):
            raise ValueError("baseline Strategy parameters are invalid")
        return dict(validation)

    @classmethod
    def _validated_candidate_parameters(
        cls,
        manifest: RunInputManifestV1,
        descriptor: StrategyDescriptorV1,
        proposal: StrategyExperimentProposalV1,
    ) -> dict[str, object]:
        baseline = cls._declared_values(manifest, descriptor)
        if proposal.parameter_name not in baseline:
            raise ValueError("proposal names an undeclared or host-owned parameter")
        if cls._json_value_equal(
            baseline[proposal.parameter_name], proposal.proposed_value
        ):
            raise ValueError("proposal does not change its parameter")
        candidate_declared = {**baseline, proposal.parameter_name: proposal.proposed_value}
        validation = validate_strategy_parameters(
            descriptor.parameters,
            cast(Mapping[str, JsonValue], candidate_declared),
            apply_defaults=False,
        )
        if isinstance(validation, tuple):
            raise ValueError("proposal value violates the Strategy parameter contract")
        full_parameters = dict(manifest.parameters)
        full_parameters[proposal.parameter_name] = validation[proposal.parameter_name]
        return full_parameters

    @classmethod
    def _candidate_manifest(
        cls, manifest: RunInputManifestV1, parameters: Mapping[str, object]
    ):
        candidate = type(manifest).model_validate(
            {**manifest.model_dump(mode="python"), "parameters": dict(parameters)}
        )
        baseline_payload = manifest.canonical_payload()
        candidate_payload = candidate.canonical_payload()
        baseline_payload.pop("parameters", None)
        candidate_payload.pop("parameters", None)
        if baseline_payload != candidate_payload:
            raise ValueError("candidate changed a pinned baseline field")
        return candidate

    def _no_draft(
        self,
        status: Literal["rejected", "unavailable"],
        reason: str,
        *,
        baseline_run_id: str | None,
        hypothesis: str,
    ) -> StrategyExperimentDraftOutcomeV1:
        try:
            self._repository.append_strategy_experiment_attempt(
                baseline_run_id=baseline_run_id,
                event_type="draft_unavailable" if status == "unavailable" else "draft_rejected",
                details={"reason": reason, "hypothesis": hypothesis[:2000]},
            )
        except Exception:
            logger.warning("strategy experiment attempt audit failed", exc_info=True)
            reason = "The attempt could not be audited, so no draft was created."
            status = "rejected"
        return StrategyExperimentDraftOutcomeV1(status=status, reason=reason)

    def _verified_result(self, run_id: str):
        try:
            return self._repository.backtest_result(run_id)
        except (BacktestIntegrityError, StrategyJobNotFound, ValueError):
            return None

    @staticmethod
    def _result_metric(result, metric: ExperimentMetric) -> float | None:
        if result is None:
            return None
        value = getattr(result.metrics, metric.value)
        return None if value is None else float(value)

    @staticmethod
    def _closed_trade_count(result) -> int | None:
        if result is None:
            return None
        return sum(
            isinstance(event, (ExitFillEventV1, TerminalSettlementEventV1))
            for event in result.events
        )

    def _verify_pair_manifest(self, experiment, baseline_result, candidate_result) -> None:
        baseline_raw = self._repository.run_input_manifest_json(
            baseline_result.run_input_manifest_digest
        )
        candidate_raw = self._repository.run_input_manifest_json(
            candidate_result.run_input_manifest_digest
        )
        if baseline_raw is None or candidate_raw is None:
            raise ValueError("manifest missing")
        baseline = read_run_input_manifest(baseline_raw)
        candidate = read_run_input_manifest(candidate_raw)
        if (
            not baseline.accepts_stored_digest(baseline_result.run_input_manifest_digest)
            or not candidate.accepts_stored_digest(candidate_result.run_input_manifest_digest)
            or baseline.canonical_json() != experiment.draft.baseline_manifest_json
            or baseline.canonical_payload().get("schema_version")
            != candidate.canonical_payload().get("schema_version")
        ):
            raise ValueError("manifest digest or schema mismatch")
        baseline_payload = baseline.canonical_payload()
        candidate_payload = candidate.canonical_payload()
        baseline_parameters = dict(baseline.parameters)
        candidate_parameters = dict(candidate.parameters)
        baseline_payload.pop("parameters", None)
        candidate_payload.pop("parameters", None)
        changed = {
            key
            for key in baseline_parameters.keys() | candidate_parameters.keys()
            if not self._json_value_equal(
                baseline_parameters.get(key), candidate_parameters.get(key)
            )
        }
        if (
            baseline_payload != candidate_payload
            or changed != {experiment.draft.parameter_name}
            or not self._json_value_equal(
                baseline_parameters.get(experiment.draft.parameter_name),
                experiment.draft.baseline_value,
            )
            or not self._json_value_equal(
                candidate_parameters.get(experiment.draft.parameter_name),
                experiment.draft.proposed_value,
            )
        ):
            raise ValueError("candidate did not preserve the baseline pins")

    @staticmethod
    def _json_value_equal(left: object, right: object) -> bool:
        return json.dumps(
            jsonable(left), sort_keys=True, separators=(",", ":"), allow_nan=False
        ) == json.dumps(
            jsonable(right), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
