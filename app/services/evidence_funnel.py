"""Reconcile published evidence against its run log row (#635).

A security whose market-data fetch returned ``None`` produces no artifact
entry and never reaches the selected universe, so the universe reads as
complete while the loss is only visible inside
``SourceHealth[yahoo_market_data]`` for that run. This module joins the two
already-persisted records on ``run_id`` and reports the funnel between them.

Everything here is read-only: every figure is read from the published
artifact or from the existing run log row, never recomputed, never fetched
and never written back. ``unexplained`` is a residual derived from the other
stages, so it can never be asserted into agreement.
"""

from __future__ import annotations

from collections.abc import Mapping
import csv
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import PIPELINE_RUNS_CSV
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceSuccessV1,
)
from app.schemas.source_health import (
    SourceHealth,
    SourceName,
    SourceStage,
    SourceState,
)

# The scanner records market-data losses as a detail code plus its own
# message; ``requested`` is never persisted as an integer, so it is
# recovered from those two fields (see ``scanner_agent`` phase B).
_FAILURE_CODES = {"ticker_failures", "partial_ticker_failures"}
_FAILURE_COUNT = re.compile(r"^(\d+) ticker request\(s\) failed")


class DiscoverySourceCountV1(BaseModel):
    """Ticker count one discovery source contributed to a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceName
    count: int


class EvidenceFunnelV1(BaseModel):
    """Stage-by-stage reconciliation of one run's discovery against records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    run_id: str | None = None
    # ``unexplained`` is ``fetched - artifact_entries`` and ``universe_shortfall``
    # is ``successes - universe_size``. Both are signed: a negative value means
    # the later stage carries more securities than the earlier one, which is a
    # real inconsistency worth showing rather than clamping away.
    # ``discovery`` counts are per source and overlap: a ticker found by two
    # sources is counted by both, and the market-data stage is fed by their
    # deduplicated union. So ``sum(counts)`` is an upper bound on ``requested``,
    # not a funnel stage that reconciles into it.
    discovery: tuple[DiscoverySourceCountV1, ...] = ()
    requested: int | None = None
    fetched: int | None = None
    fetch_failures: int | None = None
    artifact_entries: int | None = None
    successes: int | None = None
    gaps: int | None = None
    universe_size: int | None = None
    unexplained: int | None = None
    universe_shortfall: int | None = None
    # ``unrecorded`` is ``requested - artifact_entries``: every ticker the
    # market-data stage was asked for that produced no artifact entry at
    # all, whether it failed to fetch (``fetch_failures``) or was fetched
    # and never recorded (``unexplained``). It is the single "excluded by
    # criteria" term of the balance equation the tab renders:
    # requested - unrecorded = artifact_entries - gaps = successes.
    unrecorded: int | None = None


def build_evidence_funnel(
    evidence: CurrentAnalysisEvidenceV1 | None,
    run_log_row: Mapping[str, str] | None,
    universe_size: int | None = None,
) -> EvidenceFunnelV1:
    """Join published evidence to its run log row and return the funnel.

    ``run_log_row`` of ``None`` (the crash window between publishing the
    artifact and appending the run log row) yields an explicitly unavailable
    funnel rather than an inferred shortfall. A row belonging to a different
    ``run_id`` is treated the same way: the join is on ``run_id``, so an
    unmatched pair is unavailable rather than confidently wrong.
    """
    row_run_id = (run_log_row.get("run_id") or "").strip() if run_log_row else None
    # A row carrying no run id cannot be shown to belong to this artifact, so
    # it is unavailable rather than joined on trust. Older logs predate the
    # column entirely, which is exactly the case that must not fail open.
    mismatched = bool(evidence) and row_run_id != (
        evidence.run_id if evidence else None
    )
    if run_log_row is None or mismatched:
        return EvidenceFunnelV1(
            available=False,
            run_id=evidence.run_id if evidence else None,
            universe_size=universe_size,
        )

    health = parse_run_log_source_health(run_log_row)
    fetched, fetch_failures, requested = _market_data_stage(health)
    entries, successes, gaps = _artifact_stage(evidence)
    unexplained = (
        fetched - entries if fetched is not None and entries is not None else None
    )
    shortfall = (
        successes - universe_size
        if successes is not None and universe_size is not None
        else None
    )
    return EvidenceFunnelV1(
        available=True,
        run_id=evidence.run_id if evidence else row_run_id or None,
        discovery=tuple(
            DiscoverySourceCountV1(source=item.source, count=item.count)
            for item in sorted(health, key=lambda item: item.source.value)
            if item.stage is SourceStage.DISCOVERY
        ),
        requested=requested,
        fetched=fetched,
        fetch_failures=fetch_failures,
        artifact_entries=entries,
        successes=successes,
        gaps=gaps,
        universe_size=universe_size,
        unexplained=unexplained,
        universe_shortfall=shortfall,
        unrecorded=(
            requested - entries
            if requested is not None and entries is not None
            else None
        ),
    )


def parse_run_log_source_health(row: Mapping[str, str]) -> list[SourceHealth]:
    """Return the row's parsed ``source_health_json``, empty if unusable.

    This is the single reusable parse for that column, shared with the run
    log partial. A malformed payload yields no health at all; one malformed
    entry inside a usable payload drops only that entry, so a market-data
    loss recorded beside it still reaches the funnel. The first entry for a
    source wins, so a duplicated source cannot double-count discovery.
    """
    try:
        payload = json.loads(row.get("source_health_json") or "{}")
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    seen: dict[SourceName, SourceHealth] = {}
    for value in payload.values():
        try:
            health = SourceHealth.model_validate(value)
        except ValidationError:
            continue
        seen.setdefault(health.source, health)
    return list(seen.values())


def find_run_log_row(run_id: str, path: Path | None = None) -> dict[str, str] | None:
    """Return the last run log row owning ``run_id``, or None if there is none.

    The log is append-only and does not enforce unique run ids, so a re-logged
    run must reconcile against its most recent row. An unreadable log degrades
    to ``None`` like every other missing input here.
    """
    path = path or PIPELINE_RUNS_CSV
    if not run_id or not path.exists():
        return None
    match: dict[str, str] | None = None
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("run_id") == run_id:
                    match = dict(row)
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    return match


def _market_data_stage(
    health: list[SourceHealth],
) -> tuple[int | None, int | None, int | None]:
    """Return ``(fetched, fetch_failures, requested)`` for the market-data source."""
    market = next(
        (item for item in health if item.source is SourceName.YAHOO_MARKET_DATA),
        None,
    )
    if market is None:
        return None, None, None
    if market.detail_code not in _FAILURE_CODES:
        if market.state in {SourceState.SKIPPED, SourceState.FAILED}:
            # The source did not run to completion, so no request count
            # survives: unknown stays unknown rather than reading as clean.
            return market.count, None, None
        return market.count, 0, market.count
    match = _FAILURE_COUNT.match(market.display_message)
    if match is None:
        # The code says tickers were lost but the count did not survive:
        # report unknown as unknown rather than guessing zero.
        return market.count, None, None
    failures = int(match.group(1))
    return market.count, failures, market.count + failures


def _artifact_stage(
    evidence: CurrentAnalysisEvidenceV1 | None,
) -> tuple[int | None, int | None, int | None]:
    """Return ``(entries, successes, gaps)`` from the published artifact."""
    if evidence is None:
        return None, None, None
    successes = sum(
        1 for item in evidence.entries if isinstance(item, CurrentEvidenceSuccessV1)
    )
    return len(evidence.entries), successes, len(evidence.entries) - successes
