from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.backtest.strategy_explanation import (
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
)
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionProviderV1,
    InitialEntrySelectionV1,
    Signal,
    SignalSide,
    StrategyProtocolV1,
    PortfolioView,
    StrategyParameters,
    MarketViewV1,
)
from app.services.backtest.backtest_engine import _engine_signal_sort_key, _slot_rank
import scripts.research.gh66_experiment as gh66_experiment
from scripts.research.gh66_experiment import _fixed_candidate_fixture
from scripts.research.gh66_variants import (
    BUY_AND_HOLD,
    MINERVINI,
    ResearchInitialSelectionAdapter,
    research_variant_adapter,
    transform_signal_batch,
)

SESSION = date(2020, 1, 2)


def _signal(
    security_id: str, priority: str | None = None, *, raw_vcp_score: str | None = None
) -> Signal:
    explanation = None
    if raw_vcp_score is not None:
        explanation = SignalExplanationV1(
            reasons=[
                SignalReasonV1(
                    code="entry_ranking",
                    summary="Ranked by the proposed Skill.",
                    facts=[
                        ExplanationFactV1(
                            label="Raw VCP score",
                            observed=Decimal(raw_vcp_score),
                        )
                    ],
                )
            ]
        )
    return Signal(
        security_id=security_id,
        side=SignalSide.BUY,
        session=SESSION,
        rule_id="entry",
        priority=Decimal(priority) if priority is not None else None,
        explanation=explanation,
    )


def _ordered_ids(signals: list[Signal]) -> list[str]:
    return [
        signal.security_id
        for signal in sorted(
            signals, key=lambda item: item.priority or Decimal(0), reverse=True
        )
    ]


def test_proposed_variant_preserves_skill_output_objects() -> None:
    batch = [_signal("B", "2"), _signal("A", "1")]

    actual = transform_signal_batch(
        "rtly-backtest-darvas-box", batch, variant="proposed"
    )

    assert actual[0] is batch[0]
    assert actual[1] is batch[1]


def test_legacy_minervini_uses_raw_vcp_priority() -> None:
    batch = [_signal("A", "1", raw_vcp_score="63")]

    actual = transform_signal_batch(MINERVINI, batch, variant="legacy")

    assert actual[0].priority == Decimal("63")


def test_legacy_unranked_skills_retain_fallback_priority() -> None:
    batch = [_signal("A", "3"), _signal("B", "1")]

    for strategy_id in (
        "rtly-backtest-weinstein",
        "rtly-backtest-darvas-box",
        "rtly-backtest-turtle-trend",
        "rtly-backtest-moving-average",
    ):
        actual = transform_signal_batch(strategy_id, batch, variant="legacy")
        assert [signal.priority for signal in actual] == [None, None]


def test_random_variant_is_seeded_per_session_and_independent_of_input_order() -> None:
    batch = [_signal(name) for name in "ABCDEFGHIJK"]
    first = transform_signal_batch(
        "rtly-backtest-turtle-trend", batch, variant="random", seed=11
    )
    repeated = transform_signal_batch(
        "rtly-backtest-turtle-trend", list(reversed(batch)), variant="random", seed=11
    )
    other_seed = transform_signal_batch(
        "rtly-backtest-turtle-trend", batch, variant="random", seed=23
    )

    assert {signal.security_id: signal.priority for signal in first} == {
        signal.security_id: signal.priority for signal in repeated
    }
    assert _ordered_ids(first) != _ordered_ids(other_seed)
    priorities = [signal.priority for signal in first if signal.priority is not None]
    assert sorted(priorities) == [Decimal(rank) for rank in range(1, len(batch) + 1)]
    assert all(
        any(
            reason.code == "research_random_order"
            for reason in signal.explanation.reasons
        )
        and all(reason.code != "entry_ranking" for reason in signal.explanation.reasons)
        for signal in first
        if signal.explanation is not None
    )


def test_random_control_preserves_buy_and_hold_membership_at_equal_cap() -> None:
    selected = [_signal(f"S{index:02}", str(10 - index)) for index in range(10)]

    actual = transform_signal_batch(BUY_AND_HOLD, selected, variant="random", seed=11)

    assert {signal.security_id for signal in actual} == {
        signal.security_id for signal in selected
    }
    assert len(actual) == 10  # top_x=10 and cap=10: every selected signal still fits.


def test_random_control_changes_the_winner_under_a_smaller_cap() -> None:
    selected = [_signal(name) for name in "ABCDEFGHIJK"]
    first = transform_signal_batch(BUY_AND_HOLD, selected, variant="random", seed=11)
    second = transform_signal_batch(BUY_AND_HOLD, selected, variant="random", seed=23)

    assert _ordered_ids(first)[0] != _ordered_ids(second)[0]
    assert {signal.security_id for signal in first} == {
        signal.security_id for signal in second
    }


def test_fixed_candidate_state_explains_policy_admissions() -> None:
    candidates = [
        _signal("AAA", "3"),
        _signal("BBB", "5"),
        _signal("CCC", "4"),
    ]

    def winner(variant: str, seed: int | None = None) -> str:
        transformed = transform_signal_batch(
            "rtly-backtest-moving-average",
            candidates,
            variant=variant,  # type: ignore[arg-type]
            seed=seed,
        )
        host_order = sorted(transformed, key=_engine_signal_sort_key)
        return sorted(host_order, key=_slot_rank)[0].security_id

    assert winner("legacy") == "AAA"  # no priority: deterministic host fallback
    assert winner("proposed") == "BBB"  # Skill priority 5 is highest
    assert winner("random", 11) == winner("random", 11)
    assert winner("random", 11) != winner("random", 23)


def test_preregistered_candidate_fixture_captures_admissions_and_reasons() -> None:
    fixture = _fixed_candidate_fixture()

    assert fixture["portfolio_state"]["available_slots"] == 1
    assert fixture["same_eligible_candidates"] == [
        {"security_id": "AAA", "proposed_priority": "3"},
        {"security_id": "BBB", "proposed_priority": "5"},
        {"security_id": "CCC", "proposed_priority": "4"},
    ]
    assert fixture["variants"]["legacy"]["admitted_security_ids"] == ["AAA"]
    assert fixture["variants"]["proposed"]["admitted_security_ids"] == ["BBB"]
    assert all(
        len(detail["ordered_candidates"]) == 3
        and detail["admitted_security_ids"]
        == [detail["ordered_candidates"][0]["security_id"]]
        for detail in fixture["variants"].values()
    )


class _BuyAndHoldFixture:
    def __init__(self) -> None:
        self.initial_selection_calls = 0

    def initial_entry_selection(
        self, view: object, parameters: object
    ) -> InitialEntrySelectionV1:
        del view, parameters
        self.initial_selection_calls += 1
        signals = (_signal("A", "2"), _signal("B", "1"))
        return InitialEntrySelectionV1(
            session=SESSION,
            metric_id="strength",
            metric_version="1",
            rule_id="buy_and_hold",
            decisions=(
                EntrySelectionDecisionV1(
                    security_id="A", rank=1, state=EntrySelectionState.SELECTED
                ),
                EntrySelectionDecisionV1(
                    security_id="B", rank=2, state=EntrySelectionState.SELECTED
                ),
            ),
            signals=signals,
        )

    def entry_signals(self, view: object, parameters: object) -> list[Signal]:
        del view, parameters
        return []

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        del view, portfolio, parameters
        return []

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        del signal, view, portfolio, parameters
        return 1


def test_buy_and_hold_adapter_preserves_optional_initial_selection_capability() -> None:
    strategy = _BuyAndHoldFixture()
    cache: dict[date, InitialEntrySelectionV1] = {}
    adapted = research_variant_adapter(
        strategy,
        BUY_AND_HOLD,
        "legacy",
        initial_selection_cache=cache,
    )  # type: ignore[arg-type]
    random = research_variant_adapter(
        strategy,
        BUY_AND_HOLD,
        "random",
        11,
        initial_selection_cache=cache,
    )  # type: ignore[arg-type]

    assert isinstance(adapted, StrategyProtocolV1)
    assert isinstance(adapted, ResearchInitialSelectionAdapter)
    assert isinstance(adapted, InitialEntrySelectionProviderV1)
    view = SimpleNamespace(as_of_session=SESSION)
    selection = adapted.initial_entry_selection(view, {})  # type: ignore[arg-type]
    randomized = random.initial_entry_selection(view, {})  # type: ignore[arg-type]
    assert {signal.security_id for signal in selection.signals} == {"A", "B"}
    assert {decision.security_id for decision in selection.decisions} == {"A", "B"}
    assert {signal.security_id for signal in randomized.signals} == {"A", "B"}
    assert strategy.initial_selection_calls == 1
    assert random.initial_selection_cache_hits == 1


class _CountingStrategy:
    def __init__(self) -> None:
        self.entry_calls = 0

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        del view, parameters
        self.entry_calls += 1
        return [_signal("A", "3")]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        del view, portfolio, parameters
        return []

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        del signal, view, portfolio, parameters
        return 1


def test_raw_entry_signal_batches_are_shared_but_ranked_per_arm() -> None:
    strategy = _CountingStrategy()
    cache: dict[date, tuple[Signal, ...]] = {}
    legacy = research_variant_adapter(
        strategy,
        "rtly-backtest-moving-average",
        "legacy",
        entry_signal_cache=cache,
    )
    proposed = research_variant_adapter(
        strategy,
        "rtly-backtest-moving-average",
        "proposed",
        entry_signal_cache=cache,
    )
    view = SimpleNamespace(as_of_session=SESSION)

    assert legacy.entry_signals(view, {})[0].priority is None  # type: ignore[arg-type]
    assert proposed.entry_signals(view, {})[0].priority == Decimal("3")  # type: ignore[arg-type]
    assert strategy.entry_calls == 1
    assert legacy.entry_signal_cache_misses == 1
    assert proposed.entry_signal_cache_hits == 1


def test_minervini_legacy_requires_explicit_raw_score_evidence() -> None:
    with pytest.raises(ValueError, match="no raw VCP score evidence"):
        transform_signal_batch(MINERVINI, [_signal("A", "1")], variant="legacy")


@pytest.mark.parametrize(
    ("changed_identity", "message"),
    [
        ("research", "research source changed"),
        ("host", "host runtime identity changed"),
        ("skill", "Skill source identity changed"),
    ],
)
def test_replay_rejects_runtime_identity_drift(
    monkeypatch: pytest.MonkeyPatch, changed_identity: str, message: str
) -> None:
    registered = {
        "research_code_sha256": {"runner.py": "frozen"},
        "host_execution_contract": {"digest": "frozen"},
        "variants": {
            "proposed": {"skill_source_digests": {"skill": "frozen"}},
            "legacy": {
                "source_revision": gh66_experiment.LEGACY_SOURCE_REVISION,
                "skill_source_digests": {"skill": "frozen"},
            },
        },
        "original_saved_runs": {"skill": {"original_strategy_source_digest": "frozen"}},
    }
    monkeypatch.setattr(
        gh66_experiment,
        "_research_source_digests",
        lambda: {
            "runner.py": "changed" if changed_identity == "research" else "frozen"
        },
    )
    monkeypatch.setattr(
        gh66_experiment,
        "_host_execution_identity",
        lambda: {"digest": "changed" if changed_identity == "host" else "frozen"},
    )
    monkeypatch.setattr(
        gh66_experiment,
        "_current_skill_source_digests",
        lambda: {"skill": "changed" if changed_identity == "skill" else "frozen"},
    )

    with pytest.raises(RuntimeError, match=message):
        gh66_experiment._assert_frozen_runtime(registered)


def test_resume_rejects_result_from_a_different_frozen_experiment() -> None:
    registered = {
        "experiment_id": "frozen-experiment",
        "fixed_inputs": {"start_month": "2016-09", "end_month": "2026-08"},
        "original_saved_runs": {
            "rtly-backtest-moving-average": {
                "original_run_id": "original-run",
                "original_manifest_digest": "original-manifest",
                "original_strategy_source_digest": "legacy-skill-source",
            }
        },
        "variants": {
            "legacy": {
                "source_revision": gh66_experiment.LEGACY_SOURCE_REVISION,
                "skill_source_digests": {
                    "rtly-backtest-moving-average": "legacy-skill-source"
                },
            },
            "proposed": {
                "skill_source_digests": {"rtly-backtest-moving-average": "skill-source"}
            },
        },
        "research_code_sha256": {"runner.py": "research-source"},
        "host_execution_contract": {"digest": "execution-contract"},
    }
    result = {
        "experiment_id": "older-experiment",
        "strategy_id": "rtly-backtest-moving-average",
        "variant": "proposed",
        "seed": None,
        "start_month": "2016-09",
        "end_month": "2026-08",
        "status": "failed",
        "original_run_id": "original-run",
        "original_manifest_digest": "original-manifest",
    }

    with pytest.raises(RuntimeError, match="frozen run identity: experiment_id"):
        gh66_experiment._validate_existing_result(
            result,
            registered,
            strategy_id="rtly-backtest-moving-average",
            variant="proposed",
            seed=None,
        )


def test_resume_checks_completed_run_key_digest() -> None:
    registered = {
        "experiment_id": "frozen-experiment",
        "fixed_inputs": {"start_month": "2016-09", "end_month": "2026-08"},
        "original_saved_runs": {
            "rtly-backtest-moving-average": {
                "original_run_id": "original-run",
                "original_manifest_digest": "original-manifest",
            }
        },
        "variants": {
            "proposed": {
                "skill_source_digests": {"rtly-backtest-moving-average": "skill-source"}
            }
        },
        "research_code_sha256": {"runner.py": "research-source"},
        "host_execution_contract": {"digest": "execution-contract"},
    }
    run_key = {
        "experiment_id": "frozen-experiment",
        "strategy_id": "rtly-backtest-moving-average",
        "variant": "proposed",
        "seed": None,
        "start_month": "2016-09",
        "end_month": "2026-08",
        "manifest_digest": "resolved-manifest",
    }
    result = {
        **run_key,
        "run_id": gh66_experiment._sha256(run_key),
        "status": "completed",
        "original_run_id": "original-run",
        "original_manifest_digest": "original-manifest",
        "research_code_sha256": {"runner.py": "research-source"},
        "execution_contract_digest": "execution-contract",
        "proposed_skill_source_digest": "skill-source",
        "active_skill_source_digest": "skill-source",
    }

    gh66_experiment._validate_existing_result(
        result,
        registered,
        strategy_id="rtly-backtest-moving-average",
        variant="proposed",
        seed=None,
    )
    result["run_id"] = "tampered"
    with pytest.raises(RuntimeError, match="invalid run key digest"):
        gh66_experiment._validate_existing_result(
            result,
            registered,
            strategy_id="rtly-backtest-moving-average",
            variant="proposed",
            seed=None,
        )


def test_only_preregistered_random_seeds_can_be_run() -> None:
    registered = {
        "original_saved_runs": {"rtly-backtest-moving-average": {}},
        "variants": {"random": {"seeds": [11, 23]}},
    }

    gh66_experiment._validate_requested_arm(
        registered, "rtly-backtest-moving-average", "random", 11
    )
    with pytest.raises(ValueError, match="outside the frozen experiment"):
        gh66_experiment._validate_requested_arm(
            registered, "rtly-backtest-moving-average", "random", 37
        )


def _horizon_fixture() -> dict[str, object]:
    return {
        "fixed_inputs": {"start_month": "2016-09", "end_month": "2026-08"},
        "short_replay": {
            "strategy_id": "rtly-backtest-moving-average",
            "start_month": "2016-09",
            "end_month": "2016-11",
            "variants": ["legacy", "proposed", "random seed 11"],
            "repeats_per_variant": 3,
            "schedule": "interleaved by repeat across variants",
            "share_strategy_signals": False,
        },
    }


def test_run_horizon_allows_only_frozen_or_preregistered_short_period() -> None:
    registered = _horizon_fixture()
    gh66_experiment._validate_run_horizon(
        registered,  # type: ignore[arg-type]
        strategy_id="rtly-backtest-moving-average",
        variant="random",
        seed=11,
        start_month="2016-09",
        end_month="2016-11",
    )
    with pytest.raises(ValueError, match="outside the frozen experiment"):
        gh66_experiment._validate_run_horizon(
            registered,  # type: ignore[arg-type]
            strategy_id="rtly-backtest-moving-average",
            variant="proposed",
            seed=None,
            start_month="2020-01",
            end_month="2020-12",
        )


def test_short_group_must_use_the_preregistered_arms_and_schedule() -> None:
    registered = _horizon_fixture()
    gh66_experiment._validate_group_horizon(
        registered,  # type: ignore[arg-type]
        strategy_id="rtly-backtest-moving-average",
        arms=["legacy", "proposed", "random:11"],
        repeat_count=3,
        start_month="2016-09",
        end_month="2016-11",
        share_strategy_signals=False,
        interleave_repeats=True,
    )
    with pytest.raises(ValueError, match="outside the frozen experiment"):
        gh66_experiment._validate_group_horizon(
            registered,  # type: ignore[arg-type]
            strategy_id="rtly-backtest-moving-average",
            arms=["legacy", "proposed", "random:11"],
            repeat_count=1,
            start_month="2016-09",
            end_month="2016-11",
            share_strategy_signals=False,
            interleave_repeats=True,
        )


def test_legacy_and_proposed_repeats_are_interleaved_for_timing() -> None:
    assert gh66_experiment._group_schedule(
        ["legacy", "proposed", "random:11"], 3, True
    ) == [
        ("legacy", None, 1),
        ("proposed", None, 1),
        ("random", 11, 1),
        ("legacy", None, 2),
        ("proposed", None, 2),
        ("random", 11, 2),
        ("legacy", None, 3),
        ("proposed", None, 3),
        ("random", 11, 3),
    ]


def test_legacy_result_identity_uses_pinned_historical_skill_digest() -> None:
    strategy = "rtly-backtest-moving-average"
    registered = {
        "original_saved_runs": {strategy: {}},
        "variants": {
            "legacy": {"skill_source_digests": {strategy: "historical"}},
            "proposed": {"skill_source_digests": {strategy: "current"}},
        },
    }
    assert (
        gh66_experiment._active_skill_source_digest(registered, strategy, "legacy")
        == "historical"
    )
    assert (
        gh66_experiment._active_skill_source_digest(registered, strategy, "proposed")
        == "current"
    )
