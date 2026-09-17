"""Aggregate the evidence census for one published scan (#636).

Pure, read-only aggregation: it takes the already-built market view, the
published ``CurrentAnalysisEvidenceV1``, the holdings, any
``PortfolioHistoryRead`` outcomes and each Strategy's declared evidence
requirements, and answers "how much of this scan is usable, and why not".

No repository, no provider, no clock, and no new check — a fault the scan
already established is reported verbatim, never recomputed.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from app.core.ticker_identity import AmbiguousTickerAliasError, canonical_ticker
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceGapV1,
)
from app.schemas.evidence_census import (
    FAULT_DROPPED,
    FAULT_GAPPED,
    FAULT_MISSING_FRAGMENT,
    FAULT_THIN,
    EvidenceCensusSecurityV1,
    EvidenceCensusShortfallV1,
    EvidenceCensusV1,
)
from app.services.backtest.scan_view import (
    CurrentScanMarketView,
    PortfolioHistoryRead,
)
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    StrategyEvidenceRequirementsV1,
)

#: Each artifact gap reason's census fault. The reason itself is always
#: reported verbatim alongside; this mapping only classifies it.
GAP_REASON_FAULTS: Mapping[str, str] = {
    "insufficient_history": FAULT_THIN,
    "incomplete_history": FAULT_GAPPED,
    "malformed_history": FAULT_MISSING_FRAGMENT,
    "detector_failure": FAULT_MISSING_FRAGMENT,
    "stale_session": FAULT_DROPPED,
    "identity_conflict": FAULT_DROPPED,
}

_UNRESOLVED_CAUSE = "unresolved: canonicalisation collision"
_FAULT_ORDER = (FAULT_THIN, FAULT_GAPPED, FAULT_MISSING_FRAGMENT, FAULT_DROPPED)
_PORTFOLIO_PREFIX = "portfolio:"


def build_evidence_census(
    view: CurrentScanMarketView,
    *,
    current_evidence: CurrentAnalysisEvidenceV1 | None = None,
    unresolved: Iterable[str] = (),
    holdings: Iterable[str] = (),
    portfolio_reads: Iterable[PortfolioHistoryRead] = (),
    strategies: Mapping[str, StrategyEvidenceRequirementsV1] | None = None,
    aliases: Mapping[str, str] | None = None,
    currencies: Mapping[str, str] | None = None,
) -> EvidenceCensusV1:
    """Return the census for ``view``, one row per security.

    Rows are the union of the selected universe, the holdings, the
    artifact's gap securities and any portfolio reads, ordered by
    ``security_id``. Accounting totals are disjoint; ``fault_counts``
    overlap and may sum higher than ``faulted``.
    """
    alias_map = dict(aliases or {})
    universe = set(view.selected_universe)
    holding_ids = {_canonical(item, alias_map) for item in holdings if item}
    reads = {
        _strip_namespace(read.security_id): read
        for read in portfolio_reads
        if read.security_id
    }
    gaps = _artifact_gaps(current_evidence, alias_map)
    unresolved_ids = {_canonical(item, alias_map) for item in unresolved if item}
    declared = dict(strategies or {})
    currency_map = dict(currencies or {})

    rows = tuple(
        _build_row(
            security_id,
            view=view,
            universe=universe,
            holding_ids=holding_ids,
            read=reads.get(security_id),
            gap=gaps.get(security_id),
            unresolved=security_id in unresolved_ids,
            strategies=declared,
            currency=currency_map.get(security_id, ""),
        )
        for security_id in sorted(
            universe | holding_ids | set(gaps) | set(reads) | unresolved_ids
        )
    )

    dropped_rows = {
        row.security_id for row in rows if not row.in_universe and row.sessions == 0
    }
    dropped = len(dropped_rows)
    faulted = sum(
        1 for row in rows if row.faults and row.security_id not in dropped_rows
    )
    fault_counts: dict[str, int] = {}
    for row in rows:
        for fault in row.faults:
            fault_counts[fault] = fault_counts.get(fault, 0) + 1
    return EvidenceCensusV1(
        as_of_session=view.as_of_session,
        securities=rows,
        total=len(rows),
        clean=len(rows) - faulted - dropped,
        faulted=faulted,
        dropped=dropped,
        held=sum(1 for row in rows if row.is_holding),
        fault_counts={fault: fault_counts[fault] for fault in sorted(fault_counts)},
    )


def _build_row(
    security_id: str,
    *,
    view: CurrentScanMarketView,
    universe: set[str],
    holding_ids: set[str],
    read: PortfolioHistoryRead | None,
    gap: CurrentEvidenceGapV1 | None,
    unresolved: bool,
    strategies: Mapping[str, StrategyEvidenceRequirementsV1],
    currency: str = "",
) -> EvidenceCensusSecurityV1:
    """Return one accounted census row."""
    coverage = view.evidence_coverage(security_id)
    sessions = coverage.sessions
    portfolio_sessions = (
        None if read is None or read.history is None else int(len(read.history.index))
    )
    if sessions == 0 and portfolio_sessions is not None:
        sessions = portfolio_sessions
    in_universe = security_id in universe
    is_holding = security_id in holding_ids
    # Outside the universe, any session at all came from the
    # ``portfolio:`` namespace -- whether read here or already merged
    # into the view -- so it is never reported as no coverage.
    namespace = "scan" if in_universe else "portfolio" if sessions else "none"
    display_ticker = coverage.display_ticker or (
        read.display_ticker if read is not None else ""
    )

    shortfalls = _shortfalls(
        security_id,
        sessions=sessions,
        in_universe=in_universe,
        is_holding=is_holding,
        strategies=strategies,
    )
    faults: list[str] = []
    if gap is not None:
        faults.append(GAP_REASON_FAULTS.get(gap.reason, FAULT_MISSING_FRAGMENT))
    if not in_universe and sessions == 0:
        faults.append(FAULT_DROPPED)
    if shortfalls:
        faults.append(FAULT_THIN)

    cause = f"{gap.reason}: {gap.detail}" if gap is not None else None
    if cause is None and unresolved:
        cause = _UNRESOLVED_CAUSE
    entry_shortfall, exit_shortfall = _path_verdicts(shortfalls)
    return EvidenceCensusSecurityV1(
        security_id=security_id,
        display_ticker=display_ticker or security_id,
        namespace=namespace,
        in_universe=in_universe,
        is_holding=is_holding,
        sessions=sessions,
        portfolio_sessions=portfolio_sessions,
        currency=currency,
        first_session=coverage.session_dates[0] if coverage.session_dates else None,
        last_session=coverage.session_dates[-1] if coverage.session_dates else None,
        evidence_kinds=tuple(sorted(kind.value for kind in coverage.kinds)),
        missing_sessions=len(coverage.missing_sessions),
        entry_shortfall=entry_shortfall,
        exit_shortfall=exit_shortfall,
        gap_reason=gap.reason if gap is not None else None,
        gap_detail=gap.detail if gap is not None else None,
        cause=cause,
        faults=tuple(fault for fault in _FAULT_ORDER if fault in faults),
        shortfalls=shortfalls,
    )


def _shortfalls(
    security_id: str,
    *,
    sessions: int,
    in_universe: bool,
    is_holding: bool,
    strategies: Mapping[str, StrategyEvidenceRequirementsV1],
) -> tuple[EvidenceCensusShortfallV1, ...]:
    """Return the per-strategy, per-path session shortfalls.

    Entry is evaluated for securities in the selected universe, exit for
    holdings; the two paths never mark each other short.
    """
    shortfalls: list[EvidenceCensusShortfallV1] = []
    for strategy in sorted(strategies):
        requirements = strategies[strategy]
        paths = (
            ("entry", requirements.entry, in_universe),
            ("exit", requirements.exit, is_holding),
        )
        for path, declared, applies in paths:
            if not applies:
                continue
            required = max(
                (
                    requirement.minimum_sessions
                    for requirement in declared
                    if requirement.kind is EvidenceKind.PRICE_HISTORY
                ),
                default=0,
            )
            if sessions < required:
                shortfalls.append(
                    EvidenceCensusShortfallV1(
                        strategy=strategy,
                        path=path,
                        available=sessions,
                        required=required,
                    )
                )
    return tuple(shortfalls)


def _path_verdicts(
    shortfalls: tuple[EvidenceCensusShortfallV1, ...],
) -> tuple[str | None, str | None]:
    """Collapse per-strategy shortfalls into one verdict per path.

    A path is met (``None``) when no strategy declares a shortfall on it --
    including when no strategy asks anything of that path at all, which is
    reported as met rather than as a failure the data did not cause. When
    several strategies fall short, the *worst* one wins: the largest
    ``required - available`` deficit, and on a tie the alphabetically first
    strategy, so the same census always names the same reason.
    """

    def worst(path: str) -> str | None:
        candidates = [item for item in shortfalls if item.path == path]
        if not candidates:
            return None
        item = min(
            candidates,
            key=lambda row: (row.available - row.required, row.strategy),
        )
        return f"{item.strategy} needs {item.required}, has {item.available}"

    return worst("entry"), worst("exit")


def _artifact_gaps(
    current_evidence: CurrentAnalysisEvidenceV1 | None,
    aliases: Mapping[str, str],
) -> dict[str, CurrentEvidenceGapV1]:
    """Return the artifact's gaps keyed by canonical security id."""
    if current_evidence is None:
        return {}
    return {
        _canonical(entry.security_id, aliases): entry
        for entry in current_evidence.entries
        if isinstance(entry, CurrentEvidenceGapV1)
    }


def _canonical(security_id: str, aliases: Mapping[str, str]) -> str:
    """Return the canonical id, falling back to the raw id on failure."""
    raw = _strip_namespace(security_id)
    try:
        return canonical_ticker(raw, dict(aliases))
    except (AmbiguousTickerAliasError, TypeError, ValueError):
        return raw


def _strip_namespace(security_id: str) -> str:
    """Drop the ``portfolio:`` namespace prefix if present."""
    if security_id.startswith(_PORTFOLIO_PREFIX):
        return security_id[len(_PORTFOLIO_PREFIX) :]
    return security_id
