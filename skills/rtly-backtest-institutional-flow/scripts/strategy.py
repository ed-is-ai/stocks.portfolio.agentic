"""Deterministic long-only institutional-flow (13F funds_net) Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from app.services.backtest.regime_filter import entry_signals_permitted
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
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-institutional-flow"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
OBSERVED = "observed_bau"
_ENTRY_RULE = "institutional_flow_entry_v1"
_EXIT_RULE = "institutional_flow_exit_v1"


class _Settings(NamedTuple):
    """The validated parameter set; ``None`` from :func:`_settings` if unusable."""

    min_funds_net: int
    exit_funds_net: int
    max_age_days: int
    maximum_loss_pct: Decimal


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the selected universe sorted and deduplicated, or ``()``."""
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _plain_int(parameters: StrategyParameters, name: str) -> int | None:
    value = parameters.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _loss_pct(parameters: StrategyParameters) -> Decimal | None:
    """Return ``maximum_loss_pct`` when it is a finite ``0 <= pct < 100``."""
    raw = parameters.get("maximum_loss_pct")
    if isinstance(raw, bool):
        return None
    try:
        pct = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return pct if pct.is_finite() and 0 <= pct < 100 else None


def _settings(parameters: StrategyParameters) -> _Settings | None:
    """Validate every threshold; any missing or inconsistent one fails closed.

    ``exit_funds_net >= min_funds_net`` is refused: it would let the same
    reading both enter and exit a position.
    """
    entry = _plain_int(parameters, "min_funds_net")
    exit_ = _plain_int(parameters, "exit_funds_net")
    max_age = _plain_int(parameters, "max_scan_age_days")
    loss = _loss_pct(parameters)
    if entry is None or exit_ is None or max_age is None or loss is None:
        return None
    if exit_ >= entry:
        return None
    return _Settings(entry, exit_, max_age, loss)


def _session_date(value: Any) -> date | None:
    if type(value) is date:
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, date) else None
    return None


def _current_close(view: MarketViewV1, security_id: str) -> Decimal | None:
    """Return the close on ``view.as_of_session``, or ``None`` without one."""
    history = view.price_history(security_id, limit=1, columns=("close",))
    if history.empty or "close" not in history.columns:
        return None
    if _session_date(history.index[-1]) != view.as_of_session:
        return None
    try:
        close = Decimal(str(history["close"].iloc[-1]))
    except (InvalidOperation, ValueError):
        return None
    return close if close.is_finite() else None


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


def _held(portfolio: PortfolioView, security_id: str) -> Any | None:
    return next(
        (
            position
            for position in portfolio.positions
            if position.security_id == security_id and position.quantity > 0
        ),
        None,
    )


def _flow_reason(
    buy: bool, funds_net: int, scanned: date, threshold: int
) -> SignalReasonV1:
    return SignalReasonV1(
        code="funds_net_strong" if buy else "funds_net_selling",
        summary=(
            "Net 13F fund buying met the entry threshold."
            if buy
            else "Net 13F fund flow fell to the exit threshold."
        ),
        facts=[
            ExplanationFactV1(
                label="Net funds buying",
                observed=Decimal(funds_net),
                operator=ComparisonOperator.GTE if buy else ComparisonOperator.LTE,
                threshold=Decimal(threshold),
                unit=EvidenceUnit.COUNT,
                as_of=scanned,
            )
        ],
    )


def _stop_reason(
    view: MarketViewV1, close: Decimal, stop: Decimal, loss: Decimal
) -> SignalReasonV1:
    return SignalReasonV1(
        code="maximum_loss_stop",
        summary="Close hit the maximum-loss stop for this position.",
        facts=[
            ExplanationFactV1(
                label="Close",
                observed=close,
                operator=ComparisonOperator.LTE,
                threshold=stop,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
            ExplanationFactV1(
                label="Maximum loss", observed=loss, unit=EvidenceUnit.PERCENT
            ),
        ],
    )


def _stop_price(average_cost: Decimal, loss: Decimal) -> Decimal:
    return average_cost * (Decimal(1) - loss / Decimal(100))


class InstitutionalFlowStrategy:
    """Buy strong net 13F fund buying; sell on net selling or the risk stop."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """A current close gates entry and the risk stop; flow is from the scan."""
        bar = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=1, columns=("close",)
        )
        return StrategyEvidenceRequirementsV1(entry=(bar,), exit=(bar,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        settings = _settings(parameters)
        universe = _universe(parameters)
        if settings is None or not entry_signals_permitted(view, parameters, universe):
            return []
        signals: list[Signal] = []
        for security_id in universe:
            flow = _fresh_funds_net(view, security_id, settings.max_age_days)
            if flow is None or flow[0] < settings.min_funds_net:
                continue
            if _current_close(view, security_id) is None:
                continue
            reason = _flow_reason(True, flow[0], flow[1], settings.min_funds_net)
            signals.append(
                Signal(
                    security_id=security_id,
                    side=SignalSide.BUY,
                    session=view.as_of_session,
                    rule_id=_ENTRY_RULE,
                    priority=Decimal(flow[0]),
                    explanation=SignalExplanationV1(reasons=[reason]),
                )
            )
        return signals

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        settings = _settings(parameters)
        if settings is None:
            return []
        signals: list[Signal] = []
        for security_id in _universe(parameters):
            held = _held(portfolio, security_id)
            if held is None:
                continue
            reasons: list[SignalReasonV1] = []
            # The risk stop reads prices only, so it fires even when the
            # scan record is stale, reconstructed or has null enrichment.
            close = _current_close(view, security_id)
            stop = _stop_price(held.average_cost, settings.maximum_loss_pct)
            if close is not None and close <= stop:
                reasons.append(
                    _stop_reason(view, close, stop, settings.maximum_loss_pct)
                )
            flow = _fresh_funds_net(view, security_id, settings.max_age_days)
            if flow is not None and flow[0] <= settings.exit_funds_net:
                reasons.append(
                    _flow_reason(False, flow[0], flow[1], settings.exit_funds_net)
                )
            if reasons:
                signals.append(
                    Signal(
                        security_id=security_id,
                        side=SignalSide.SELL,
                        session=view.as_of_session,
                        rule_id=_EXIT_RULE,
                        explanation=SignalExplanationV1(reasons=reasons),
                    )
                )
        return signals

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the cost-based close at which the risk stop fires.

        Settings are validated as the exit validates them, with no defaults,
        so an advertised level is always one the exit would act on.
        """
        held = _held(portfolio, security_id)
        if held is None:
            return None
        settings = _settings(parameters)
        if settings is None:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy settings are missing, unusable or inconsistent.",
            )
        loss = settings.maximum_loss_pct
        stop = _stop_price(held.average_cost, loss)
        facts = [
            ExplanationFactV1(
                label="Maximum-loss stop", observed=stop, unit=EvidenceUnit.PRICE
            ),
            ExplanationFactV1(
                label="Maximum loss", observed=loss, unit=EvidenceUnit.PERCENT
            ),
        ]
        if stop <= 0:
            return StopLevelV1(
                rule_code="no_level", summary="No positive stop level.", facts=facts
            )
        return StopLevelV1(
            level=stop,
            rule_code="maximum_loss_stop",
            summary=f"Max loss {loss.normalize():f}% from average cost",
            facts=facts,
            basis="average_cost",
            trigger="close_lte",
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int | Decimal:
        """SELL the held quantity; BUY sizing is engine-owned (returns 0)."""
        if signal.side == SignalSide.SELL:
            held = _held(portfolio, signal.security_id)
            return Decimal(0) if held is None else held.quantity
        return 0


strategy = InstitutionalFlowStrategy()
