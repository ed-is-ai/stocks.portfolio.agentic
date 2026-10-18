"""Local eligibility for handing a Strategy insight to the GH-15 draft form.

Pure and read-only: it only decides whether to *offer* a link. Drafting,
validation, approval and enqueue stay exclusively in StrategyExperimentService.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import re
from urllib.parse import urlencode

from app.schemas.strategy_insights import (
    StrategyInsightClaimV1,
    StrategyQuestionContextV1,
)
from app.schemas.strategy_outcomes import OutcomeRunV1
from app.services.backtest.skill_discovery import StrategyDescriptorV1

DeclaredParameters = Callable[
    [OutcomeRunV1, StrategyDescriptorV1], StrategyQuestionContextV1 | None
]


def _mentions(text: str, name: str) -> bool:
    pattern = re.escape(name).replace("_", "[_ ]")
    return (
        re.search(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", text, re.I) is not None
    )


def handoff_url(
    claim: StrategyInsightClaimV1,
    runs: Iterable[OutcomeRunV1],
    *,
    baseline_ids: frozenset[str],
    strategies: Mapping[str, StrategyDescriptorV1],
    declared: DeclaredParameters,
) -> str | None:
    """Return a prefilled draft-form link, or None when the idea is research-only.

    Eligible: the cited runs belong to one currently discovered Strategy whose
    source still matches, a cited run is a live verified baseline, and the text
    names exactly one declared parameter of that Strategy.
    """
    by_handle = {row.evidence_handle: row for row in runs}
    cited = [by_handle[h] for h in claim.evidence_handles if h in by_handle]
    if not cited or len({row.strategy_id for row in cited}) != 1:
        return None
    strategy = strategies.get(cited[0].strategy_id)
    baseline = next((row for row in cited if row.run_id in baseline_ids), None)
    if strategy is None or baseline is None:
        return None
    context = declared(baseline, strategy)
    if context is None:
        return None
    named = [
        p.name for p in context.declared_parameters if _mentions(claim.text, p.name)
    ]
    if len(named) != 1:
        return None
    query = urlencode({"baseline_run_id": baseline.run_id, "hypothesis": claim.text})
    return f"/strategy-manager/experiments?{query}"
