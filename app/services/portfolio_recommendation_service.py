"""Portfolio-specific Sell/Hold/Buy recommendations (#441).

Evaluates the assigned Strategy's own runtime code against the already
published scan artifact plus the portfolio's current holdings, through the
current-scan view adapter (``app.services.backtest.scan_view``). Read-only
by construction: no network fetch, no trade placement, no Strategy-ID-
specific branches. Every failure mode — no assignment, an unavailable
Strategy, a runtime that will not load or raises — resolves to a typed
state the route renders as an actionable alert, never a 500.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from app.core.config import ANALYSIS_JSON, SKILLS_DIR
from app.core.ticker_identity import (
    AmbiguousTickerAliasError,
    canonical_ticker,
    load_aliases,
)
from app.schemas.analysis_artifact import (
    read_analysis_artifact_meta,
    read_analysis_records,
)
from app.schemas.portfolio_recommendation import (
    NO_ASSIGNMENT,
    EvaluationCoverageV1,
    EvidenceState,
    EvaluationUnavailable,
    NoAssignment,
    RecommendationResultV1,
    RecommendationV1,
)
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.backtest.scan_view import (
    CurrentScanMarketView,
    build_portfolio_view,
    build_scan_market_view,
)
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.strategy_evidence import (
    EvidencePreflightV1,
    StrategyEvidenceRequirementsV1,
    preflight_evidence,
    strategy_support_label,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    Signal,
    SignalSide,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
)
from app.services.portfolio_service import PortfolioService
from app.services.strategy_assignment_service import StrategyAssignmentService
from app.services.trader_service import TraderService

# Reuse the worker's single-class runtime loader verbatim — never a second
# loading convention for turning a pinned Strategy identity into a live
# instance (plan 024 Design Notes).
from app.services.backtest.worker import _load_strategy_instance

logger = logging.getLogger(__name__)

#: Action sort rank — the screen's fixed Sell → Hold → Buy order.
_ACTION_RANK: dict[str, int] = {"sell": 0, "hold": 1, "buy": 2}

#: The one rule-code → plain-language wording table. Host-generated rule
#: codes live here; a Strategy's own rule_id falls back to a generic
#: sentence, so reason text always has exactly one home (plan 024).
_RULE_REASONS: dict[str, str] = {
    "no_exit_signal": ("No exit signal from the assigned Strategy — position held."),
    "scan_evidence_missing": (
        "Held security is not resolvable against the current scan/alias "
        "evidence — fails safe to Hold."
    ),
    "exit_evidence_unsupported": (
        "The assigned Strategy needs evidence the current scan does not "
        "carry, so its exit rules were not evaluated — fails safe to Hold."
    ),
    "evidence_incomplete": (
        "Not enough evidenced history for the assigned Strategy's rules on "
        "this security — fails safe to Hold."
    ),
}

#: The label used when a Strategy's recommendation support cannot be
#: determined at all (discovery, runtime load, or scan evidence missing).
SUPPORT_UNKNOWN = "unknown"


def _reason_for(rule_id: str) -> str:
    """Return the plain-language reason for one rule code."""
    return _RULE_REASONS.get(rule_id, f"Strategy rule {rule_id}")


def _declared_requirements(
    strategy: object, parameters: Mapping[str, Any]
) -> StrategyEvidenceRequirementsV1 | str | None:
    """Read one Strategy's evidence declaration (#471).

    Returns the validated declaration, ``None`` when the runtime declares
    nothing at all (never treated as "safe"), or a plain-language failure
    reason when the declaration raises or is the wrong type.
    """
    declare = getattr(strategy, "evidence_requirements", None)
    if not callable(declare):
        return None
    try:
        declared = declare(parameters)
    except Exception as exc:
        logger.exception("Strategy evidence declaration raised")
        return f"Strategy evidence declaration failed: {exc}"
    if not isinstance(declared, StrategyEvidenceRequirementsV1):
        return (
            "Strategy evidence declaration is not a "
            "StrategyEvidenceRequirementsV1 (evidence contract v1)."
        )
    return declared


def _coverage(
    preflight: EvidencePreflightV1, evaluated_securities: int
) -> EvaluationCoverageV1:
    """Project a preflight onto the result's typed diagnostics."""
    return EvaluationCoverageV1(
        # ``EvidenceCompatibility`` is a ``StrEnum`` whose members are
        # exactly the schema's closed ``EvidenceState`` vocabulary.
        entry_state=cast(EvidenceState, preflight.entry.value),
        exit_state=cast(EvidenceState, preflight.exit.value),
        entry_missing_evidence=tuple(kind.value for kind in preflight.entry_missing),
        exit_missing_evidence=tuple(kind.value for kind in preflight.exit_missing),
        evaluated_securities=evaluated_securities,
        degraded_securities=preflight.degraded_securities,
    )


class PortfolioRecommendationService:
    """Read-only Sell/Hold/Buy evaluation of a portfolio's Strategy (#441).

    Composes the #440 assignment seam, the published scan artifact, and the
    portfolio's current holdings through the current-scan view adapter, and
    invokes the assigned Strategy's own runtime code. Every dependency is
    injectable for tests.
    """

    def __init__(
        self,
        assignment_service: StrategyAssignmentService,
        trader: TraderService,
        portfolio_service: PortfolioService | None = None,
        skills_root: Path | None = None,
        loader: Callable[[Path], StrategyProtocolV1] | None = None,
    ) -> None:
        """Store dependencies; ``loader`` defaults to the worker's loader."""
        self._assignment_service = assignment_service
        self._trader = trader
        self._portfolio_service = portfolio_service
        self._skills_root = skills_root if skills_root is not None else SKILLS_DIR
        self._analysis_path = ANALYSIS_JSON
        self._loader = loader if loader is not None else _load_strategy_instance

    # --- typed outcome ----------------------------------------------------

    def recommend(
        self, portfolio_id: int
    ) -> RecommendationResultV1 | NoAssignment | EvaluationUnavailable:
        """Evaluate the assigned Strategy for one portfolio (read-only).

        Deterministic: identical inputs (assignment, artifact, holdings)
        produce an identical result apart from the ``evaluated_at`` stamp.
        """
        assignment_view = self._assignment_service.assignment_view(portfolio_id)
        if assignment_view is None:
            return NO_ASSIGNMENT
        if not assignment_view.available:
            return EvaluationUnavailable(
                reason="Assigned Strategy is no longer discoverable."
            )
        assignment = assignment_view.assignment
        descriptor = self._descriptor(assignment.strategy_id)
        if descriptor is None:
            return EvaluationUnavailable(
                reason=(
                    f"Strategy {assignment.strategy_id!r} is no longer discoverable."
                )
            )
        freshness = self._assignment_service.freshness()
        meta = read_analysis_artifact_meta(self._analysis_path)
        records = self._load_analysis_records()
        if not records:
            return EvaluationUnavailable(
                reason="No published scan artifact to evaluate against.",
                freshness=freshness,
            )
        if meta is None:
            return EvaluationUnavailable(
                reason="Published scan artifact has no readable metadata.",
                freshness=freshness,
            )
        aliases = load_aliases()
        try:
            scan_view, unresolved = build_scan_market_view(records, aliases)
        except ValueError:
            # No record carries OHLCV history — no usable evidence.
            return EvaluationUnavailable(
                reason="Scan artifact carries no usable price evidence.",
                freshness=freshness,
            )
        if not scan_view.selected_universe:
            return EvaluationUnavailable(
                reason="Scan artifact carries no usable price evidence.",
                freshness=freshness,
            )
        try:
            strategy = self._loader(self._skills_root / descriptor.runtime_path)
        except Exception as exc:
            logger.exception(
                "Strategy runtime failed to load for portfolio %s", portfolio_id
            )
            return EvaluationUnavailable(
                reason=f"Strategy runtime could not be loaded: {exc}",
                freshness=freshness,
            )
        held = self._held_positions(portfolio_id, aliases)
        try:
            # The stored snapshot must never carry a stale universe: the
            # host-bound parameter always comes from the current scan.
            # ``bind_universe`` validates that selection and raises for a
            # malformed one — a typed state here, never a 500.
            parameters = {
                key: value
                for key, value in assignment.parameters.items()
                if key != descriptor.universe.parameter
            } | dict(descriptor.bind_universe(scan_view.selected_universe))
        except Exception as exc:
            logger.exception("Universe binding failed for portfolio %s", portfolio_id)
            return EvaluationUnavailable(
                reason=f"Strategy universe could not be bound: {exc}",
                freshness=freshness,
            )
        requirements = _declared_requirements(strategy, parameters)
        if requirements is None:
            return EvaluationUnavailable(
                reason=(
                    f"Strategy {descriptor.strategy_id} does not declare its "
                    "evidence requirements (evidence contract v1)."
                ),
                freshness=freshness,
            )
        if isinstance(requirements, str):
            return EvaluationUnavailable(reason=requirements, freshness=freshness)
        # Each path is preflighted only against the securities it can act
        # on: entry considers scan candidates, exit only the holdings. A
        # thin-history candidate must not degrade the exit path, and a
        # holding the entry path never looks at must not degrade entry.
        entry_securities = set(scan_view.selected_universe)
        exit_securities = set(held)
        try:
            preflight = preflight_evidence(
                requirements, scan_view, entry_securities, exit_securities
            )
        except Exception as exc:
            logger.exception("Evidence preflight failed for portfolio %s", portfolio_id)
            return EvaluationUnavailable(
                reason=f"Strategy evidence preflight failed: {exc}",
                freshness=freshness,
            )
        try:
            portfolio_view = build_portfolio_view(
                [
                    position.model_copy(update={"ticker": security_id})
                    for security_id, position in held.items()
                ],
                self._trader.get_cash_balance(portfolio_id),
                scan_view.as_of_session,
            )
            # The current-scan view deliberately returns an honest, narrower
            # scan projection (no fabricated stage/vcp) rather than a full
            # ``HistoricalScanRecordV1``; runtimes read those fields
            # defensively via ``getattr``, so the structural narrowing is
            # safe to assert here (plan 024: extend the boundary
            # structurally, never fabricate provenance).
            protocol_view = cast(MarketViewV1, scan_view)
            # An unsupported path is never invoked: asking a Strategy for
            # rules it cannot evaluate is exactly how a missing stage
            # became a false Sell (#471).
            exits = (
                validate_exit_signals(
                    strategy.exit_signals(protocol_view, portfolio_view, parameters)
                )
                if preflight.exit_supported
                else ()
            )
            entries = (
                validate_entry_signals(
                    strategy.entry_signals(protocol_view, parameters)
                )
                if preflight.entry_supported
                else ()
            )
        except Exception as exc:
            logger.exception(
                "Strategy evaluation failed for portfolio %s", portfolio_id
            )
            return EvaluationUnavailable(
                reason=f"Strategy evaluation failed: {exc}",
                freshness=freshness,
            )
        return RecommendationResultV1(
            portfolio_id=portfolio_id,
            analysis_run_id=meta.run_id,
            generated_at=meta.generated_at,
            market_session=scan_view.as_of_session,
            freshness=freshness,
            strategy_id=descriptor.strategy_id,
            strategy_source_digest=descriptor.source_digest,
            parameters=parameters,
            recommendations=self._map_actions(
                held, scan_view, exits, entries, preflight
            ),
            unresolved=tuple(sorted(set(unresolved))),
            coverage=_coverage(preflight, len(entry_securities | exit_securities)),
            evaluated_at=datetime.now(UTC),
        )

    def strategy_support(self) -> Mapping[str, str]:
        """Return ``{strategy_id: support label}`` for every discoverable
        Strategy, evaluated against the current scan evidence (#471).

        Fail-soft by construction: a Strategy whose runtime will not load,
        whose declaration is missing or invalid, or which cannot be
        compared because no usable scan artifact exists is labelled
        ``unknown`` rather than raising. Read-only — no fetch, no trade,
        no persistence.
        """
        try:
            choices = tuple(self._assignment_service.list_choices())
        except Exception:
            logger.exception("Strategy support lookup could not list choices")
            return {}
        view = self._support_scan_view()
        return {
            descriptor.strategy_id: self._support_for(descriptor, view)
            for descriptor in choices
        }

    # --- helpers ----------------------------------------------------------

    def _support_scan_view(self) -> CurrentScanMarketView | None:
        """Build the current-scan view for support labelling, or ``None``."""
        try:
            records = self._load_analysis_records()
            if not records:
                return None
            view, _ = build_scan_market_view(records, load_aliases())
        except Exception:
            logger.exception("Strategy support lookup could not build a scan view")
            return None
        return view if view.selected_universe else None

    def _support_for(
        self, descriptor: StrategyDescriptorV1, view: CurrentScanMarketView | None
    ) -> str:
        """Label one Strategy's current recommendation support, fail-soft."""
        if view is None:
            return SUPPORT_UNKNOWN
        try:
            strategy = self._loader(self._skills_root / descriptor.runtime_path)
            parameters = dict(descriptor.default_parameters) | dict(
                descriptor.bind_universe(view.selected_universe)
            )
            requirements = _declared_requirements(strategy, parameters)
            if not isinstance(requirements, StrategyEvidenceRequirementsV1):
                return SUPPORT_UNKNOWN
            # No portfolio is in play for a support label, so the exit
            # path is judged on the same candidate set as entry.
            preflight = preflight_evidence(
                requirements, view, view.selected_universe, view.selected_universe
            )
        except Exception:
            logger.exception(
                "Strategy support lookup failed for %s", descriptor.strategy_id
            )
            return SUPPORT_UNKNOWN
        return strategy_support_label(preflight)

    def _descriptor(self, strategy_id: str) -> StrategyDescriptorV1 | None:
        """Resolve one descriptor from the current discovery choices.

        No discovery re-run: ``list_choices()`` is the assignment service's
        already-cached, fail-soft scan result.
        """
        by_id = {
            descriptor.strategy_id: descriptor
            for descriptor in self._assignment_service.list_choices()
        }
        return by_id.get(strategy_id)

    def _load_analysis_records(self) -> list[StockRecord]:
        """Load published scan records fail-soft, one malformed row skipped.

        Mirrors ``PortfolioService.load_analysis``; an injected portfolio
        service reuses its implementation directly.
        """
        if self._portfolio_service is not None:
            return self._portfolio_service.load_analysis()
        try:
            data = read_analysis_records(self._analysis_path)
        except Exception:
            return []
        records: list[StockRecord] = []
        for row in data:
            try:
                records.append(StockRecord.model_validate(row))
            except Exception:
                continue
        return records

    def _held_positions(
        self, portfolio_id: int, aliases: dict[str, str]
    ) -> dict[str, Position]:
        """Return ``{canonical security_id: Position}`` for open holdings.

        An ambiguous alias can never match the scan universe, so its raw
        ticker is kept and the fail-safe Hold rule handles it below.
        """
        held: dict[str, Position] = {}
        for position in self._trader.get_portfolio(portfolio_id=portfolio_id):
            if position.shares <= 0:
                continue
            try:
                security_id = canonical_ticker(position.ticker, aliases)
            except AmbiguousTickerAliasError:
                security_id = position.ticker
            if security_id in held:
                # Two lots canonicalizing to one id: keep the first, log the
                # collapse — one recommendation row still covers the holding.
                logger.warning(
                    "Duplicate canonical holding %r for portfolio %s",
                    security_id,
                    portfolio_id,
                )
                continue
            held[security_id] = position
        return held

    def _map_actions(
        self,
        held: Mapping[str, Position],
        scan_view: CurrentScanMarketView,
        exits: tuple[Signal, ...],
        entries: tuple[Signal, ...],
        preflight: EvidencePreflightV1,
    ) -> tuple[RecommendationV1, ...]:
        """Apply the deterministic action mapping (Sell takes precedence).

        Held + exit → Sell; held without exit → Hold; held outside the
        scan universe → Hold with a ``scan_evidence_missing`` warning
        (never Sell/Buy from missing evidence); unheld + entry → Buy;
        unheld without entry → omitted. Entry signals on held securities
        are ignored — Sell precedence — and recorded as nothing.

        Evidence outranks every signal (#471): a holding whose exit path
        the current view cannot evidence — structurally unsupported, or
        supported but under-evidenced for that security — is always a
        Hold with a typed warning, and an unheld security the entry path
        could not properly evaluate can never become a Buy.
        """
        exit_by_security = {
            signal.security_id: signal
            for signal in exits
            if signal.side == SignalSide.SELL
            and signal.session == scan_view.as_of_session
        }
        entry_rules = {
            signal.security_id: signal.rule_id
            for signal in entries
            if signal.side == SignalSide.BUY
            and signal.session == scan_view.as_of_session
        }
        recommendations: list[RecommendationV1] = []
        for security_id, position in held.items():
            if security_id not in scan_view.selected_universe:
                # Fail-safe first: a holding the scan cannot evidence is
                # never a Sell, even if the runtime emitted an exit for it
                # from portfolio-only data (AC #441.6).
                recommendations.append(
                    RecommendationV1(
                        action="hold",
                        ticker=position.ticker,
                        security_id=security_id,
                        rule_id="scan_evidence_missing",
                        reason=_reason_for("scan_evidence_missing"),
                        evidence_warnings=("scan_evidence_missing",),
                    )
                )
                continue
            unsupported = _exit_evidence_gap(preflight, security_id)
            if unsupported is not None:
                rule_id, warnings = unsupported
                recommendations.append(
                    RecommendationV1(
                        action="hold",
                        ticker=position.ticker,
                        security_id=security_id,
                        rule_id=rule_id,
                        reason=_reason_for(rule_id),
                        evidence_warnings=warnings,
                    )
                )
                continue
            exit_signal = exit_by_security.get(security_id)
            if exit_signal is not None:
                recommendations.append(
                    RecommendationV1(
                        action="sell",
                        ticker=position.ticker,
                        security_id=security_id,
                        rule_id=exit_signal.rule_id,
                        reason=_reason_for(exit_signal.rule_id),
                    )
                )
            else:
                recommendations.append(
                    RecommendationV1(
                        action="hold",
                        ticker=position.ticker,
                        security_id=security_id,
                        rule_id="no_exit_signal",
                        reason=_reason_for("no_exit_signal"),
                    )
                )
        for security_id in sorted(entry_rules):
            if security_id in held:
                continue
            if security_id not in scan_view.selected_universe:
                continue
            if not preflight.entry_supported or preflight.entry_degraded_for(
                security_id
            ):
                # Evidence the entry rules needed was absent for this
                # security — omitted, and surfaced through ``coverage``.
                continue
            rule_id = entry_rules[security_id]
            recommendations.append(
                RecommendationV1(
                    action="buy",
                    ticker=security_id,
                    security_id=security_id,
                    rule_id=rule_id,
                    reason=_reason_for(rule_id),
                )
            )
        recommendations.sort(
            key=lambda rec: (_ACTION_RANK[rec.action], rec.security_id)
        )
        return tuple(recommendations)


def _exit_evidence_gap(
    preflight: EvidencePreflightV1, security_id: str
) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(rule_id, warnings)`` when the exit path lacked evidence.

    ``None`` means the exit rules were genuinely evaluated for this
    security, so the ordinary Sell/Hold mapping applies.
    """
    if not preflight.exit_supported:
        return "exit_evidence_unsupported", ("exit_evidence_unsupported",) + tuple(
            kind.value for kind in preflight.exit_missing
        )
    missing = preflight.degraded_exit.get(security_id)
    if missing:
        return "evidence_incomplete", ("evidence_incomplete",) + tuple(
            kind.value for kind in missing
        )
    return None
