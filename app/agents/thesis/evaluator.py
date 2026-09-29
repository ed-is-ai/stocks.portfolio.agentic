"""Deterministic position-thesis evaluator (GH-14).

Pure: no LLM, no I/O, no wall clock. One thesis version plus the security's
published record, the artifact's run identity and its freshness become a
``ThesisEvaluationV1`` whose JSON is byte-identical for identical inputs.

Status precedence: any rule fired → ``invalidated``; else a missing record,
a non-fresh artifact or any limited rule → ``evidence_limited``; else a
price-level rule within ``WEAKENED_MARGIN_PCT`` of its trigger →
``weakened``; else ``confirmed``.
"""

from __future__ import annotations

import math
from datetime import date

from app.agents.research.evidence import UNKNOWN_RUN_ID
from app.core.recommendation import STAGE_2
from app.schemas.analysis_artifact import AnalysisArtifactMeta
from app.schemas.evidence_ref import EvidenceRefV1
from app.schemas.position_thesis import (
    CloseBelowSmaRule,
    CloseBelowStopRule,
    PositionThesisV1,
    RuleOutcome,
    RuleResultV1,
    ScoreBelowRule,
    ThesisEvaluationV1,
    ThesisRuleV1,
    ThesisStatus,
)
from app.schemas.record import StockRecord
from app.services.freshness_service import Freshness, FreshnessState

#: A price-level rule whose close sits at most this many percent above its
#: trigger level marks the thesis ``weakened``.
WEAKENED_MARGIN_PCT = 3

Observed = dict[str, float | int | str | None]


def evaluate_thesis(
    thesis: PositionThesisV1,
    record: StockRecord | None,
    meta: AnalysisArtifactMeta | None,
    freshness: Freshness,
) -> ThesisEvaluationV1:
    """Evaluate every rule of ``thesis`` against the published evidence."""
    run_id = meta.run_id if meta else UNKNOWN_RUN_ID
    session = _session(record)
    source = f"analysis run {run_id}"
    evaluated = [
        _evaluate_rule(index, rule, record, session, source)
        for index, rule in enumerate(thesis.rules, start=1)
    ]
    results = tuple(result for result, _near in evaluated)
    limitations = _limitations(record, meta, freshness)
    return ThesisEvaluationV1(
        thesis_id=thesis.id,
        thesis_version=thesis.version,
        security_id=thesis.security_id,
        analysis_run_id=run_id,
        session=session,
        status=_status(results, limitations, any(near for _r, near in evaluated)),
        results=results,
        limitations=limitations,
    )


def _status(
    results: tuple[RuleResultV1, ...], limitations: tuple[str, ...], near: bool
) -> ThesisStatus:
    outcomes = {result.outcome for result in results}
    if "fired" in outcomes:
        return "invalidated"
    if limitations or "limited" in outcomes:
        return "evidence_limited"
    return "weakened" if near else "confirmed"


def _limitations(
    record: StockRecord | None,
    meta: AnalysisArtifactMeta | None,
    freshness: Freshness,
) -> tuple[str, ...]:
    limits: list[str] = []
    if record is None:
        limits.append("No published analysis record for this security.")
    if meta is None:
        limits.append("The analysis run identity is unknown.")
    if freshness.state is not FreshnessState.FRESH:
        limits.append(f"The published analysis is {freshness.state.value}.")
    return tuple(limits)


def _session(record: StockRecord | None) -> date | None:
    if record is None:
        return None
    try:
        return date.fromisoformat(record.as_of[:10])
    except ValueError:
        return None


def _finite(value: float | int | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _result(
    index: int,
    rule: ThesisRuleV1,
    outcome: RuleOutcome,
    observed: Observed,
    session: date | None,
    source: str,
) -> RuleResultV1:
    refs = tuple(
        EvidenceRefV1(kind="scan_field", id=field, as_of=session, source=source)
        for field in observed
    )
    return RuleResultV1(
        index=index, rule=rule, outcome=outcome, evidence=refs, observed=observed
    )


def _evaluate_rule(
    index: int,
    rule: ThesisRuleV1,
    record: StockRecord | None,
    session: date | None,
    source: str,
) -> tuple[RuleResultV1, bool]:
    """Return one rule's result and whether its price sits near the trigger."""
    outcome, observed, near = _check(rule, record)
    return _result(index, rule, outcome, observed, session, source), near


def _check(
    rule: ThesisRuleV1, record: StockRecord | None
) -> tuple[RuleOutcome, Observed, bool]:
    """Return (outcome, observed fields in citation order, near trigger)."""
    analysis = record.analysis if record else None
    price = _finite(record.price) if record else None
    if isinstance(rule, CloseBelowStopRule):
        stop = _finite(analysis.stop_loss) if analysis else None
        return _price_level(price, stop, {"price": price, "stop_loss": stop})
    if isinstance(rule, CloseBelowSmaRule):
        field = f"sma{rule.period}"
        sma = _finite(getattr(record, field)) if record else None
        observed: Observed = {"price": price, field: sma}
        if rule.min_rel_volume is None:
            return _price_level(price, sma, observed)
        volume = _finite(record.rel_volume) if record else None
        observed["rel_volume"] = volume
        outcome, _observed, near = _price_level(price, sma, observed)
        if outcome != "fired":
            return outcome, observed, near
        if volume is None:
            return "limited", observed, False
        confirmed = volume >= rule.min_rel_volume
        # Below the SMA without the volume: clear, but at its trigger.
        return ("fired" if confirmed else "clear"), observed, not confirmed
    if analysis is None:
        field = "score" if isinstance(rule, ScoreBelowRule) else "stage"
        return "limited", {field: None}, False
    if isinstance(rule, ScoreBelowRule):
        fired = analysis.score < rule.min_score
        return ("fired" if fired else "clear"), {"score": analysis.score}, False
    # Without SMA150/SMA200 the stage classifier falls back to price and
    # reports "Stage 1", so the stage is not evidence the trend was lost.
    smas: dict[str, float | None] = {
        field: _finite(getattr(record, field)) if record else None
        for field in ("sma150", "sma200")
    }
    if any(sma is None or sma <= 0 for sma in smas.values()):
        return "limited", {"stage": analysis.stage, **smas}, False
    fired = analysis.stage != STAGE_2
    return ("fired" if fired else "clear"), {"stage": analysis.stage}, False


def _price_level(
    price: float | None, trigger: float | None, observed: Observed
) -> tuple[RuleOutcome, Observed, bool]:
    """Fire when ``price`` is below ``trigger``; near when within the margin.

    A missing or non-positive level is limited evidence, never clear: the
    scanner writes 0.0 for a level it could not compute.
    """
    if price is None or trigger is None or price <= 0 or trigger <= 0:
        return "limited", observed, False
    if price < trigger:
        return "fired", observed, False
    # Compare against the scaled trigger: (price / trigger - 1) * 100 reads
    # exactly 3% as 3.0000000000000027 and misses the boundary.
    near = price <= trigger * (1 + WEAKENED_MARGIN_PCT / 100)
    return "clear", observed, near
