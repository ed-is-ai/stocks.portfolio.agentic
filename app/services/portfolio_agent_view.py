"""Pure builder for the Portfolio tab's agent layer (GH-19).

Joins what the agents already said about a portfolio -- the assigned
Strategy's recommendations and evidence coverage, and the Risk Coach report
-- onto the holdings they judge. No I/O and no calculation of its own: every
figure comes from the typed inputs, and a missing input becomes a declared,
named unavailable state rather than a guess.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.schemas.portfolio_recommendation import (
    EvaluationUnavailable,
    NoAssignment,
    RecommendationEvidenceDiagnosticV1,
    RecommendationResultV1,
    RecommendationV1,
)
from app.schemas.portfolio_risk import RiskFindingV1, RiskReportV1
from app.schemas.position_thesis import STATUS_LABELS, ThesisSummary, describe_rule
from app.schemas.trade import Position

Tone = Literal["risk", "good", "warn", "info", "muted"]
RecommendationOutcome = RecommendationResultV1 | NoAssignment | EvaluationUnavailable
#: Each held security's thesis state; ``None`` means the store failed.
ThesisStates = Mapping[str, ThesisSummary] | None

_ACTION_TONE: dict[str, Tone] = {"sell": "risk", "hold": "good", "buy": "info"}
_SEVERITY_TONE: dict[str, Tone] = {"high": "risk", "medium": "warn"}
_THESIS_TONE: dict[str, Tone] = {
    "invalidated": "risk",
    "weakened": "warn",
    "evidence_limited": "warn",
    "confirmed": "good",
}
#: Findings about one holding. Portfolio-wide ones (capital at risk, sector
#: concentration) name every holding they cover, so they must not flag each row.
_POSITION_KINDS = frozenset(
    {"position_concentration", "below_stop", "unpriced", "no_stop"}
)
NO_STRATEGY = "No Strategy assigned"
STRATEGY_UNAVAILABLE = "Strategy unavailable"
RISK_UNAVAILABLE = "Risk unavailable"
NOT_EVALUATED = "Not evaluated"
THESIS_UNAVAILABLE = "Thesis unavailable"
NO_THESIS = "No thesis"
DRAFT_TO_CONFIRM = "Draft to confirm"
AWAITING_SCAN = "Awaiting scan"
REVIEW_DUE = "Review due"
EARLIER_RUN = "last checked on an earlier run"


@dataclass(frozen=True)
class AgentCell:
    """One agent-derived table cell: a status badge plus an optional note."""

    text: str
    tone: Tone = "muted"
    note: str = ""


@dataclass(frozen=True)
class HoldingAgentRow:
    """The agent cells for one holding, addressed by its DOM-safe slug."""

    slug: str
    strategy: AgentCell
    risk: AgentCell
    evidence: AgentCell
    thesis: AgentCell


@dataclass(frozen=True)
class PortfolioAgentView:
    """Everything the agents partial swaps into the Portfolio tab."""

    #: The portfolio the view was built for; scopes every swapped DOM id.
    portfolio_id: int | None
    rows: tuple[HoldingAgentRow, ...]
    #: One line per urgent item; its length is the count.
    attention: tuple[str, ...]
    #: Sources that could not be read, so the count is known to be partial.
    unavailable: tuple[str, ...]
    open_risk: AgentCell


def agent_slug(ticker: str) -> str:
    """Encode ``ticker`` as a DOM-id-safe, collision-free slug.

    Every character outside ``[A-Za-z0-9]`` becomes ``_<hex>_`` (``_`` too),
    so ``BRK.B`` and ``BRK-B`` never share an id.
    """
    return re.sub(r"[^A-Za-z0-9]", lambda m: f"_{ord(m[0]):x}_", ticker)


def build_agent_view(
    portfolio_id: int | None,
    positions: Sequence[Position],
    outcome: RecommendationOutcome,
    risk: RiskReportV1 | None,
    theses: ThesisStates,
) -> PortfolioAgentView:
    """Join ``outcome``, ``risk`` and ``theses`` (``None`` = failed) onto
    ``positions``.

    A Strategy or thesis gap only makes the count partial when there are
    holdings for it to judge.
    """
    rows = tuple(
        HoldingAgentRow(
            slug=agent_slug(p.ticker),
            strategy=_strategy_cell(p, outcome),
            risk=_risk_cell(p, risk),
            evidence=_evidence_cell(p, outcome),
            thesis=_thesis_for(p, theses),
        )
        for p in positions
    )
    unavailable = tuple(
        reason
        for reason, missing in (
            (NO_STRATEGY, bool(positions) and isinstance(outcome, NoAssignment)),
            (
                STRATEGY_UNAVAILABLE,
                bool(positions) and isinstance(outcome, EvaluationUnavailable),
            ),
            (RISK_UNAVAILABLE, risk is None),
            (THESIS_UNAVAILABLE, bool(positions) and theses is None),
        )
        if missing
    )
    return PortfolioAgentView(
        portfolio_id=portfolio_id,
        rows=rows,
        attention=_attention(positions, outcome, risk, theses),
        unavailable=unavailable,
        open_risk=_open_risk(risk),
    )


def _recommendation(
    p: Position, result: RecommendationResultV1
) -> RecommendationV1 | None:
    return next((r for r in result.recommendations if r.security_id == p.ticker), None)


def _strategy_cell(p: Position, outcome: RecommendationOutcome) -> AgentCell:
    if isinstance(outcome, NoAssignment):
        return AgentCell(NO_STRATEGY)
    if isinstance(outcome, EvaluationUnavailable):
        return AgentCell(STRATEGY_UNAVAILABLE, "warn", outcome.reason)
    rec = _recommendation(p, outcome)
    if rec is None:
        return AgentCell(NOT_EVALUATED)
    return AgentCell(rec.action.title(), _ACTION_TONE[rec.action], rec.reason)


def _exit_diagnostic(
    p: Position, result: RecommendationResultV1
) -> RecommendationEvidenceDiagnosticV1 | None:
    return next(
        (
            d
            for d in result.coverage.diagnostics
            if d.path == "exit" and d.security_id == p.ticker
        ),
        None,
    )


def _diagnostic_text(d: RecommendationEvidenceDiagnosticV1) -> str:
    """``available / required`` for a session shortfall, else the cause."""
    if 0 < d.required_sessions and d.available_sessions < d.required_sessions:
        return f"{d.available_sessions} / {d.required_sessions}"
    return d.cause.replace("_", " ").capitalize()


def _evidence_cell(p: Position, outcome: RecommendationOutcome) -> AgentCell:
    """Exit-path evidence; "Complete" only when nothing says otherwise."""
    if not isinstance(outcome, RecommendationResultV1):
        return _strategy_cell(p, outcome)
    diagnostic = _exit_diagnostic(p, outcome)
    if diagnostic is not None:
        return AgentCell(_diagnostic_text(diagnostic), "warn", "exit evidence")
    if _recommendation(p, outcome) is None:
        return AgentCell(NOT_EVALUATED)
    coverage = outcome.coverage
    if coverage.exit_state != "compatible" or p.ticker in coverage.degraded_securities:
        reason = ", ".join(coverage.exit_missing_evidence) or (
            f"exit evidence {coverage.exit_state}"
        )
        return AgentCell("Degraded", "warn", reason)
    return AgentCell("Complete", "good")


def _risk_cell(p: Position, risk: RiskReportV1 | None) -> AgentCell:
    """Weight plus the holding's own worst finding; sector breaches as a note."""
    if risk is None:
        return AgentCell(RISK_UNAVAILABLE)
    weight = risk.position_weights.get(p.display_symbol)
    text = "—" if weight is None else f"{weight}%"
    named = [f for f in risk.findings if p.display_symbol in f.tickers]
    # Findings arrive ordered high -> medium -> info, so the first match is
    # the most severe.
    own = next(
        (
            f
            for f in named
            if f.kind in _POSITION_KINDS and f.severity in _SEVERITY_TONE
        ),
        None,
    )
    if own is not None:
        return AgentCell(text, _SEVERITY_TONE[own.severity], own.title)
    sector = next(
        (f for f in named if f.kind == "sector_concentration" and f.severity == "high"),
        None,
    )
    return AgentCell(text, "muted", sector.title if sector else "Within policy")


def _attention(
    positions: Sequence[Position],
    outcome: RecommendationOutcome,
    risk: RiskReportV1 | None,
    theses: ThesisStates,
) -> tuple[str, ...]:
    """High risk findings, Sell recommendations, exit evidence gaps and
    invalidated theses.

    Every exit diagnostic the Evidence cell shows as a warning is counted,
    and every invalidated thesis the Thesis cell shows, so a cell's tone and
    the count always agree.
    """
    findings: tuple[RiskFindingV1, ...] = risk.findings if risk else ()
    items = [f.title for f in findings if f.severity == "high"]
    if isinstance(outcome, RecommendationResultV1):
        items += [
            f"{r.ticker}: Strategy says Sell"
            for r in outcome.recommendations
            if r.action == "sell"
        ]
        for p in positions:
            d = _exit_diagnostic(p, outcome)
            if d is not None:
                items.append(
                    f"{p.display_symbol}: exit evidence gap ({_diagnostic_text(d)})"
                )
    for p in positions:
        summary = theses.get(p.ticker) if theses else None
        latest = summary.latest if summary and summary.current else None
        if latest is not None and latest.status == "invalidated":
            items.append(f"{p.display_symbol}: thesis invalidated")
    return tuple(items)


def _thesis_for(p: Position, theses: ThesisStates) -> AgentCell:
    if theses is None:
        return AgentCell(THESIS_UNAVAILABLE)
    return thesis_cell(theses.get(p.ticker))


def thesis_cell(summary: ThesisSummary | None) -> AgentCell:
    """The Thesis cell: evaluated status, else awaiting scan, draft or none.

    An active thesis's status wins over a pending draft (noted instead), so
    an invalidated thesis is never hidden behind a new draft. An evaluation
    of an earlier run than the published one is shown as evidence limited.
    """
    if summary is None or (summary.active is None and summary.pending is None):
        return AgentCell(NO_THESIS)
    if summary.active is None:
        return AgentCell(DRAFT_TO_CONFIRM, "info")
    notes = [DRAFT_TO_CONFIRM] if summary.pending else []
    if summary.review_due:
        notes.append(REVIEW_DUE)
    latest = summary.latest
    if latest is None:
        return AgentCell(AWAITING_SCAN, "info", " · ".join(notes))
    if not summary.current:
        return AgentCell(
            STATUS_LABELS["evidence_limited"], "warn", " · ".join([EARLIER_RUN, *notes])
        )
    fired = latest.first_fired
    if fired is not None:
        notes.insert(0, f"Rule {fired.index}: {describe_rule(fired.rule)}")
    return AgentCell(
        STATUS_LABELS[latest.status], _THESIS_TONE[latest.status], " · ".join(notes)
    )


def _open_risk(risk: RiskReportV1 | None) -> AgentCell:
    """Capital at risk; over the limit exactly when the engine said so."""
    if risk is None:
        return AgentCell("Unavailable")
    if risk.capital_at_risk_gbp is None:
        return AgentCell("No evidenced stops")
    over = any(
        f.kind == "capital_at_risk" and f.severity == "high" for f in risk.findings
    )
    return AgentCell(
        f"£{risk.capital_at_risk_gbp:,.2f} · {risk.capital_at_risk_pct}%",
        "risk" if over else "muted",
    )
