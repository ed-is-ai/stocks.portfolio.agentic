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
    found = re.search(rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])", text, re.I)
    return found is not None


def handoff_url(
    claim: StrategyInsightClaimV1,
    runs: Iterable[OutcomeRunV1],
    *,
    baseline_ids: frozenset[str],
    strategies: Mapping[str, StrategyDescriptorV1],
    declared: DeclaredParameters,
) -> str | None:
    """Return a prefilled draft-form link, or None when the idea is research-only.

    Eligible: every cited handle is known, the cited runs (and any claimed
    Strategy IDs) name one currently discovered Strategy, and the first cited
    live verified baseline whose source still matches has exactly one declared
    parameter named in the text.
    """
    by_handle = {row.evidence_handle: row for row in runs}
    if any(handle not in by_handle for handle in claim.evidence_handles):
        return None
    cited = [by_handle[handle] for handle in claim.evidence_handles]
    strategy_ids = {row.strategy_id for row in cited}
    if len(strategy_ids) != 1:
        return None
    if claim.strategy_ids and set(claim.strategy_ids) != strategy_ids:
        return None
    strategy = strategies.get(cited[0].strategy_id)
    if strategy is None:
        return None
    for baseline in (row for row in cited if row.run_id in baseline_ids):
        if _names_one_declared_parameter(claim.text, declared(baseline, strategy)):
            query = urlencode(
                {"baseline_run_id": baseline.run_id, "hypothesis": claim.text}
            )
            return f"/strategy-manager/experiments?{query}"
    return None


def _names_one_declared_parameter(
    text: str, context: StrategyQuestionContextV1 | None
) -> bool:
    if context is None:
        return False
    named = [p.name for p in context.declared_parameters if _mentions(text, p.name)]
    return len(named) == 1
