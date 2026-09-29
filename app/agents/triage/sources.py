"""Pure mappers from each agent's typed output to attention events (GH-18).

Every mapper is deterministic: ids come from stable keys (run id,
portfolio, security, finding kind or rule id, source name), never from the
clock, and ``observed_at`` is the evidence time the caller passes in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from functools import partial
from typing import Literal

from app.core.alerting import classify_alert
from app.schemas.attention import (
    CATEGORY_SEVERITY,
    AttentionCategory,
    AttentionEventV1,
    portfolio_key,
    run_key,
)
from app.schemas.evidence_ref import EvidenceRefV1
from app.schemas.portfolio_recommendation import (
    RecommendationEvidenceDiagnosticV1,
    RecommendationResultV1,
)
from app.schemas.portfolio_risk import RiskReportV1
from app.schemas.position_thesis import ThesisSummary
from app.schemas.record import StockRecord
from app.schemas.source_health import SourceHealth, SourceName, SourceState
from app.schemas.trade import Position
from app.services.freshness_service import Freshness, FreshnessState

STALE = "Stale: "
#: Risk findings about one holding; the rest are portfolio-wide.
POSITION_KINDS = frozenset(
    {"position_concentration", "below_stop", "unpriced", "no_stop"}
)
HeldHit = Literal["stop", "target"]
#: Every held sell signal a run raises: the stop/target predicate, a
#: trailing stop, or a held security's watched-setup stop.
HeldSignal = Literal["stop", "target", "trailing", "watched_stop"]
#: Each held signal's (category, kind, title suffix, summary).
_HELD: dict[HeldSignal, tuple[AttentionCategory, str, str, str]] = {
    "stop": (
        "held_risk",
        "stop_hit",
        "at or below its stop",
        "The current price is at or below the recorded stop-loss.",
    ),
    "target": (
        "exit",
        "target_hit",
        "reached its profit target",
        "The current price is at or above the profit target.",
    ),
    "trailing": (
        "held_risk",
        "trailing_stop",
        "hit its trailing stop",
        "The price fell past the trailing-stop threshold from its peak.",
    ),
    "watched_stop": (
        "held_risk",
        "watched_stop",
        "broke its watched stop",
        "The price is at or below the watchlist stop level.",
    ),
}


def held_hit(pos: Position) -> HeldHit | None:
    """The held stop-loss / profit-target predicate; a stop hit wins."""
    if pos.current_price is None:
        return None
    if pos.stop_loss is not None and pos.current_price <= pos.stop_loss:
        return "stop"
    if pos.profit_target_20 is not None and pos.current_price >= pos.profit_target_20:
        return "target"
    return None


def diagnostic_text(d: RecommendationEvidenceDiagnosticV1) -> str:
    """``available / required`` for a session shortfall, else the cause."""
    if 0 < d.required_sessions and d.available_sessions < d.required_sessions:
        return f"{d.available_sessions} / {d.required_sessions}"
    return d.cause.replace("_", " ").capitalize()


def _event(
    category: AttentionCategory,
    *,
    source_event_id: str,
    kind: str,
    portfolio_id: int | None,
    security_id: str | None,
    title: str,
    summary: str,
    evidence: EvidenceRefV1,
    raised_by: str,
    observed_at: datetime | None = None,
    stale: bool = False,
) -> AttentionEventV1:
    """One event; a stale exit or setup is titled so it never reads as fresh."""
    stale_title = stale and category in ("exit", "new_setup")
    return AttentionEventV1(
        source_event_id=source_event_id,
        category=category,
        severity=CATEGORY_SEVERITY[category],
        kind=kind,
        portfolio_id=portfolio_id,
        security_id=security_id,
        title=f"{STALE}{title}" if stale_title else title,
        summary=f"{summary} Evidence is stale." if stale else summary,
        evidence=[evidence],
        raised_by=raised_by,
        observed_at=observed_at,
        stale=stale,
    )


def _price_date(prices_as_of: str | None) -> date | None:
    try:
        return date.fromisoformat((prices_as_of or "")[:10])
    except ValueError:
        return None


def risk_events(
    report: RiskReportV1,
    *,
    portfolio_id: int | None,
    positions: Sequence[Position],
    prices_as_of: str | None,
) -> list[AttentionEventV1]:
    """High-severity Risk Coach findings as ``held_risk`` events."""
    canonical = {p.display_symbol: p.ticker for p in positions}
    as_of = _price_date(prices_as_of)
    return [
        _event(
            "held_risk",
            source_event_id=(
                f"risk:{portfolio_key(portfolio_id)}:{f.kind}:{'+'.join(f.tickers) or '-'}"
            ),
            kind=f.kind,
            portfolio_id=portfolio_id,
            security_id=(
                canonical.get(f.tickers[0], f.tickers[0])
                if f.kind in POSITION_KINDS and len(f.tickers) == 1
                else None
            ),
            title=f.title,
            summary=f.detail or f.title,
            evidence=EvidenceRefV1(
                kind="risk_finding", id=f.kind, as_of=as_of, source="risk_coach"
            ),
            raised_by="risk_coach",
        )
        for f in report.findings
        if f.severity == "high"
    ]


def recommendation_events(
    result: RecommendationResultV1, *, positions: Sequence[Position]
) -> list[AttentionEventV1]:
    """Sell recommendations (``exit``) and held exit-evidence gaps
    (``evidence``); stale when the evaluated artifact was not fresh."""
    display = {p.ticker: p.display_symbol for p in positions}
    event = partial(
        _event,
        portfolio_id=result.portfolio_id,
        evidence=EvidenceRefV1(
            kind="recommendation",
            id=result.analysis_run_id,
            as_of=result.generated_at.date(),
            source=f"strategy:{result.strategy_id}",
        ),
        raised_by="strategy",
        observed_at=result.generated_at,
        stale=result.freshness != "fresh",
    )
    run = ":".join(
        (run_key(result.analysis_run_id), portfolio_key(result.portfolio_id))
    )
    sells = [
        event(
            "exit",
            source_event_id=f"sell:{run}:{r.security_id}:{r.rule_id}",
            kind="strategy_sell",
            security_id=r.security_id,
            title=f"{r.ticker}: Strategy says Sell",
            summary=r.reason,
        )
        for r in result.recommendations
        if r.action == "sell"
    ]
    # The first exit diagnostic per holding, as the Evidence cell shows it.
    gaps: dict[str, RecommendationEvidenceDiagnosticV1] = {}
    for d in result.coverage.diagnostics:
        if d.path == "exit" and d.security_id in display:
            gaps.setdefault(d.security_id, d)
    return sells + [
        event(
            "evidence",
            source_event_id=f"exit-evidence:{run}:{security}",
            kind="exit_evidence_gap",
            security_id=security,
            title=f"{display[security]}: exit evidence gap ({diagnostic_text(d)})",
            summary=f"Exit evidence is incomplete: {d.cause.replace('_', ' ')}.",
        )
        for security, d in gaps.items()
    ]


def thesis_events(
    theses: Mapping[str, ThesisSummary],
    *,
    portfolio_id: int | None,
    positions: Sequence[Position],
    observed_at: datetime | None,
) -> list[AttentionEventV1]:
    """Invalidated theses as ``exit`` events; stale unless checked against
    the published run (``observed_at`` is that run's time)."""
    events = []
    for p in positions:
        summary = theses.get(p.ticker)
        latest = summary.latest if summary else None
        if (
            summary is None
            or summary.active is None
            or latest is None
            or latest.status != "invalidated"
        ):
            continue
        version = f"{latest.thesis_id}v{latest.thesis_version}"
        events.append(
            _event(
                "exit",
                source_event_id=(
                    f"thesis:{run_key(latest.analysis_run_id)}:"
                    f"{portfolio_key(portfolio_id)}:{p.ticker}:{version}"
                ),
                kind="thesis_invalidated",
                portfolio_id=portfolio_id,
                security_id=p.ticker,
                title=f"{p.display_symbol}: thesis invalidated",
                summary="An invalidation rule of the confirmed thesis fired.",
                evidence=EvidenceRefV1(
                    kind="thesis_evaluation",
                    id=version,
                    as_of=latest.session,
                    source="thesis_monitor",
                ),
                raised_by="thesis_monitor",
                observed_at=observed_at if summary.current else None,
                stale=not summary.current,
            )
        )
    return events


def source_health_events(
    health: Mapping[SourceName, SourceHealth], *, run_id: str | None
) -> list[AttentionEventV1]:
    """Every non-``ok`` source as an ``evidence`` event."""
    return [
        _event(
            "evidence",
            source_event_id=f"source:{run_key(run_id)}:{h.source.value}",
            kind=f"source_{h.state.value}",
            portfolio_id=None,
            security_id=None,
            title=f"{h.label} source {h.state_label.lower()}",
            summary=h.display_message or f"{h.label} returned no usable data.",
            evidence=EvidenceRefV1(
                kind="source_health",
                id=h.source.value,
                as_of=h.data_as_of,
                source="pipeline",
            ),
            raised_by="pipeline",
            observed_at=h.completed_at,
            stale=(
                h.data_as_of is not None
                and h.completed_at is not None
                and h.data_as_of < h.completed_at.date()
            ),
        )
        for h in health.values()
        if h.state is not SourceState.OK
    ]


def freshness_events(
    freshness: Freshness, *, run_id: str | None
) -> list[AttentionEventV1]:
    """A stale or unknown published analysis as one ``evidence`` event."""
    if freshness.state is FreshnessState.FRESH:
        return []
    at = freshness.refreshed_at
    title = (
        f"Analysis is stale (as of {at.date().isoformat()})"
        if at is not None and freshness.state is FreshnessState.STALE
        else "Analysis freshness is unknown"
    )
    return [
        _event(
            "evidence",
            source_event_id=f"freshness:{run_key(run_id)}",
            kind=f"analysis_{freshness.state.value}",
            portfolio_id=None,
            security_id=None,
            title=title,
            summary="Findings built on this analysis may be out of date.",
            evidence=EvidenceRefV1(
                kind="analysis_artifact",
                id=run_key(run_id),
                as_of=at.date() if at else None,
                source="pipeline",
            ),
            raised_by="pipeline",
            observed_at=at,
            stale=True,
        )
    ]


def record_setups(
    records: Iterable[StockRecord], held: Iterable[str]
) -> list[tuple[str, str, str]]:
    """``(ticker, kind, label)`` for each unheld breakout ``classify_alert``
    finds in ``records``."""
    owned = set(held)
    return [
        (r.ticker, "breakout", trigger)
        for r in records
        if r.ticker not in owned and (trigger := classify_alert(r).trigger)
    ]


def setup_events(
    setups: Iterable[tuple[str, str, str]],
    *,
    run_id: str | None,
    observed_at: datetime | None,
    stale: bool = False,
) -> list[AttentionEventV1]:
    """``(ticker, kind, label)`` setups as ``new_setup`` events."""
    return [
        _event(
            "new_setup",
            source_event_id=f"setup:{run_key(run_id)}:{ticker}:{kind}",
            kind=kind,
            portfolio_id=None,
            security_id=ticker,
            title=f"{ticker}: {label}",
            summary=f"{label} on an unheld security.",
            evidence=EvidenceRefV1(
                kind="analysis_record",
                id=ticker,
                as_of=observed_at.date() if observed_at else None,
                source="scanner",
            ),
            raised_by="scanner",
            observed_at=observed_at,
            stale=stale,
        )
        for ticker, kind, label in setups
    ]


def held_events(
    positions: Iterable[Position],
    *,
    portfolio_id: int | None,
    portfolio_name: str,
    run_id: str | None,
    observed_at: datetime | None,
    signals: Mapping[str, HeldSignal] | None = None,
) -> list[AttentionEventV1]:
    """Held sell signals: the stop/target predicate on each position, else
    this run's alerter ``signals`` for its ticker (trailing / watched stop).

    Stops are ``held_risk``, profit targets ``exit``; one event per holding.
    """
    events = []
    for pos in positions:
        hit = held_hit(pos) or (signals or {}).get(pos.ticker)
        if hit is None:
            continue
        category, kind, suffix, summary = _HELD[hit]
        events.append(
            _event(
                category,
                source_event_id=(
                    f"held:{run_key(run_id)}:{portfolio_key(portfolio_id)}:"
                    f"{pos.ticker}:{hit}"
                ),
                kind=kind,
                portfolio_id=portfolio_id,
                security_id=pos.ticker,
                title=f"{portfolio_name}: {pos.display_symbol} {suffix}",
                summary=(
                    summary
                    if observed_at
                    else f"{summary} The price evidence date is unknown."
                ),
                evidence=EvidenceRefV1(
                    kind="held_position",
                    id=pos.ticker,
                    as_of=observed_at.date() if observed_at else None,
                    source="price_cache",
                ),
                raised_by="alert_agent",
                observed_at=observed_at,
            )
        )
    return events
