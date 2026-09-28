"""Unit tests for the deterministic position-thesis evaluator (GH-14)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.agents.thesis.evaluator import WEAKENED_MARGIN_PCT, evaluate_thesis
from app.schemas.analysis_artifact import AnalysisArtifactMeta
from app.schemas.position_thesis import (
    RULE_ADAPTER,
    PositionThesisV1,
    ThesisEvaluationV1,
)
from app.schemas.record import StockRecord
from app.schemas.scan import StockAnalysis
from app.services.freshness_service import Freshness, FreshnessState

META = AnalysisArtifactMeta(
    run_id="run-1", generated_at=datetime(2026, 9, 25, tzinfo=UTC)
)
FRESH = Freshness(state=FreshnessState.FRESH)
STALE = Freshness(state=FreshnessState.STALE)


def make_thesis(*rules: dict[str, Any], **overrides: Any) -> PositionThesisV1:
    """Return an active thesis version for ``AAA`` with the given rules."""
    fields: dict[str, Any] = {
        "id": 1,
        "portfolio_id": 7,
        "security_id": "AAA",
        "version": 1,
        "rationale": "Stage 2 leader.",
        "expected_setup": "Holds above its 50-day SMA.",
        "rules": tuple(RULE_ADAPTER.validate_python(r) for r in rules),
        "text_source": "user",
        "active": True,
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    return PositionThesisV1.model_validate({**fields, **overrides})


def make_record(
    price: float = 100.0,
    *,
    stop: float | None = 90.0,
    stage: str = "Stage 2",
    score: int = 8,
    analysis: bool = True,
    **scan: Any,
) -> StockRecord:
    """Return a published record for ``AAA`` with a 2026-09-25 session."""
    fields: dict[str, Any] = {
        "ticker": "AAA",
        "as_of": "2026-09-25",
        "price": price,
        "sma50": 95.0,
        "sma150": 80.0,
        "sma200": 70.0,
        "volume": 1000,
        "rel_volume": 1.2,
        "high_52w": 120.0,
        "low_52w": 60.0,
        "pct_from_52w_high": -10.0,
        "pct_change_week": 1.0,
        **scan,
    }
    if analysis:
        fields["analysis"] = StockAnalysis(
            score=score, stage=stage, stop_loss=stop, summary="AAA holds up"
        )
    return StockRecord.model_validate(fields)


def _evaluate(
    thesis: PositionThesisV1,
    record: StockRecord | None,
    freshness: Freshness = FRESH,
) -> ThesisEvaluationV1:
    return evaluate_thesis(thesis, record, META, freshness)


def test_invalidated_cites_rule_fields_session_and_run() -> None:
    thesis = make_thesis({"kind": "close_below_sma", "period": 50})
    result = _evaluate(thesis, make_record(price=94.0))

    assert result.status == "invalidated"
    fired = result.first_fired
    assert fired is not None
    assert (fired.index, fired.rule.kind) == (1, "close_below_sma")
    assert [ref.id for ref in fired.evidence] == ["price", "sma50"]
    for ref in fired.evidence:
        assert ref.kind == "scan_field"
        assert ref.as_of == date(2026, 9, 25)
        assert ref.source == "analysis run run-1"
    assert fired.observed == {"price": 94.0, "sma50": 95.0}
    assert fired.citation == (
        "rule 1 close_below_sma (price, sma50) · session 2026-09-25 · "
        "analysis run run-1"
    )


def test_weakened_when_price_is_near_the_stop() -> None:
    thesis = make_thesis({"kind": "close_below_stop"})
    result = _evaluate(thesis, make_record(price=91.8, stop=90.0))

    assert WEAKENED_MARGIN_PCT == 3
    assert result.status == "weakened"


def test_confirmed_when_every_rule_is_clear_and_fresh() -> None:
    thesis = make_thesis(
        {"kind": "close_below_stop"},
        {"kind": "close_below_sma", "period": 200},
        {"kind": "stage_2_lost"},
        {"kind": "score_below", "min_score": 5},
    )
    result = _evaluate(thesis, make_record())

    assert result.status == "confirmed"
    assert {r.outcome for r in result.results} == {"clear"}
    assert result.limitations == ()


def test_missing_record_is_evidence_limited() -> None:
    thesis = make_thesis({"kind": "stage_2_lost"}, {"kind": "close_below_stop"})
    result = _evaluate(thesis, None)

    assert result.status == "evidence_limited"
    assert {r.outcome for r in result.results} == {"limited"}
    assert result.session is None


def test_stale_artifact_is_evidence_limited_not_confirmed() -> None:
    result = _evaluate(make_thesis({"kind": "stage_2_lost"}), make_record(), STALE)

    assert result.status == "evidence_limited"
    assert result.limitations == ("The published analysis is stale.",)


def test_missing_stop_is_evidence_limited() -> None:
    result = _evaluate(
        make_thesis({"kind": "close_below_stop"}), make_record(stop=None)
    )

    assert result.status == "evidence_limited"
    assert result.results[0].observed == {"price": 100.0, "stop_loss": None}


def test_non_finite_field_is_limited() -> None:
    thesis = make_thesis({"kind": "close_below_sma", "period": 50})
    result = _evaluate(thesis, make_record(sma50=float("nan")))

    assert result.results[0].outcome == "limited"


def test_fired_rule_wins_over_missing_evidence() -> None:
    thesis = make_thesis({"kind": "close_below_stop"}, {"kind": "stage_2_lost"})
    result = _evaluate(thesis, make_record(stop=None, stage="Stage 3"), STALE)

    assert result.status == "invalidated"


def test_stage_and_score_rules() -> None:
    thesis = make_thesis(
        {"kind": "stage_2_lost"}, {"kind": "score_below", "min_score": 6}
    )
    result = _evaluate(thesis, make_record(stage="Stage 3", score=5))

    assert [r.outcome for r in result.results] == ["fired", "fired"]
    assert result.results[1].observed == {"score": 5}


def test_no_analysis_section_limits_analysis_rules() -> None:
    thesis = make_thesis({"kind": "score_below", "min_score": 6})
    result = _evaluate(thesis, make_record(analysis=False))

    assert result.status == "evidence_limited"


def test_sma_rule_needs_its_volume_to_fire() -> None:
    thesis = make_thesis(
        {"kind": "close_below_sma", "period": 50, "min_rel_volume": 1.5}
    )

    confirmed = _evaluate(thesis, make_record(price=94.0, rel_volume=1.6))
    unconfirmed = _evaluate(thesis, make_record(price=94.0, rel_volume=1.1))

    assert confirmed.status == "invalidated"
    assert confirmed.results[0].observed["rel_volume"] == 1.6
    # Below the SMA without the volume: clear, but right at its trigger.
    assert unconfirmed.results[0].outcome == "clear"
    assert unconfirmed.status == "weakened"


def test_same_inputs_give_identical_facts_json() -> None:
    thesis = make_thesis(
        {"kind": "close_below_sma", "period": 50, "min_rel_volume": 1.5},
        {"kind": "score_below", "min_score": 6},
    )
    record = make_record(price=94.0, rel_volume=1.6)

    first = _evaluate(thesis, record).model_dump_json()
    second = _evaluate(thesis, record.model_copy()).model_dump_json()

    assert first == second
    assert ThesisEvaluationV1.model_validate_json(first).model_dump_json() == first


@pytest.mark.parametrize(
    ("rule", "scan"),
    [
        ({"kind": "close_below_sma", "period": 50}, {"sma50": 0.0}),
        ({"kind": "close_below_sma", "period": 200}, {"sma200": -1.0}),
        ({"kind": "close_below_stop"}, {"stop": 0.0}),
        ({"kind": "close_below_stop"}, {"price": 0.0}),
    ],
    ids=["sma50-zero", "sma200-negative", "stop-zero", "price-zero"],
)
def test_non_positive_level_or_price_is_limited_not_clear(
    rule: dict[str, Any], scan: dict[str, Any]
) -> None:
    # The scanner writes 0.0 for a level it could not compute.
    result = _evaluate(make_thesis(rule), make_record(**scan))

    assert result.status == "evidence_limited"
    assert result.results[0].outcome == "limited"
