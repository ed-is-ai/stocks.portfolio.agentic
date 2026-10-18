"""Deterministic long-only institutional-flow (13F funds_net) Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_explanation import (
    ComparisonOperator,
    EvidenceUnit,
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-institutional-flow"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
OBSERVED = "observed_bau"


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _plain_int(parameters: StrategyParameters, name: str) -> int | None:
    value = parameters.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _fresh_funds_net(
    view: MarketViewV1, security_id: str, max_age_days: int
) -> tuple[int, date] | None:
    """Return ``(funds_net, scan_date)`` or ``None`` when evidence is unusable.

    Unusable means: no record, wrong security, future-dated, reconstructed
    provenance, null ``funds_net``, or older than ``max_age_days`` (stale
    between 13F filings).
    """
    scan = view.scan_result(security_id)
    if scan is None or getattr(scan, "security_id", None) != security_id:
        return None
    scanned = getattr(scan, "as_of_session_date", None)
    if not isinstance(scanned, date) or scanned > view.as_of_session:
        return None
    if getattr(scan, "provenance_quality", None) != OBSERVED:
        return None
    if (view.as_of_session - scanned).days > max_age_days:
        return None
    funds_net = getattr(getattr(scan, "enrichment", None), "funds_net", None)
    if isinstance(funds_net, bool) or not isinstance(funds_net, int):
        return None
    return funds_net, scanned


def _held_quantity(portfolio: PortfolioView, security_id: str) -> Decimal:
    for position in portfolio.positions:
        if position.security_id == security_id:
            return max(position.quantity, Decimal(0))
    return Decimal(0)


def _signal(
    view: MarketViewV1,
    security_id: str,
    side: SignalSide,
    funds_net: int,
    scanned: date,
    threshold: int,
) -> Signal:
    buy = side == SignalSide.BUY
    return Signal(
        security_id=security_id,
        side=side,
        session=view.as_of_session,
        rule_id="institutional_flow_entry_v1" if buy else "institutional_flow_exit_v1",
        priority=Decimal(funds_net) if buy else None,
        explanation=SignalExplanationV1(
            reasons=[
                SignalReasonV1(
                    code="funds_net_strong" if buy else "funds_net_faded",
                    summary=(
                        "Net 13F fund buying met the entry threshold."
                        if buy
                        else "Net 13F fund buying fell to the exit threshold."
                    ),
                    facts=[
                        ExplanationFactV1(
                            label="Net funds buying",
                            observed=Decimal(funds_net),
                            operator=ComparisonOperator.GTE
                            if buy
                            else ComparisonOperator.LTE,
                            threshold=Decimal(threshold),
                            unit=EvidenceUnit.COUNT,
                            as_of=scanned,
                        )
                    ],
                )
            ]
        ),
    )


class InstitutionalFlowStrategy:
    """Buy strong net 13F fund buying; sell when it fades."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Only a current bar is required; flow comes from the scan record."""
        bar = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=1, columns=("close",)
        )
        return StrategyEvidenceRequirementsV1(entry=(bar,), exit=(bar,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        threshold = _plain_int(parameters, "min_funds_net")
        max_age = _plain_int(parameters, "max_scan_age_days")
        if threshold is None or max_age is None:
            return []
        signals: list[Signal] = []
        for security_id in _universe(parameters):
            flow = _fresh_funds_net(view, security_id, max_age)
            if flow is not None and flow[0] >= threshold:
                signals.append(
                    _signal(
                        view, security_id, SignalSide.BUY, flow[0], flow[1], threshold
                    )
                )
        return signals

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        threshold = _plain_int(parameters, "exit_funds_net")
        max_age = _plain_int(parameters, "max_scan_age_days")
        if threshold is None or max_age is None:
            return []
        signals: list[Signal] = []
        for security_id in _universe(parameters):
            if _held_quantity(portfolio, security_id) == 0:
                continue
            flow = _fresh_funds_net(view, security_id, max_age)
            if flow is not None and flow[0] <= threshold:
                signals.append(
                    _signal(
                        view, security_id, SignalSide.SELL, flow[0], flow[1], threshold
                    )
                )
        return signals

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int | Decimal:
        """SELL the held quantity; BUY sizing is engine-owned (returns 0)."""
        if signal.side == SignalSide.SELL:
            return _held_quantity(portfolio, signal.security_id)
        return 0


strategy = InstitutionalFlowStrategy()
