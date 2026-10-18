"""Deterministic long-only z-score mean-reversion strategy."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

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
    PositionSummaryV1,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-mean-reversion"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
_ENTRY_RULE = "mean_reversion_oversold_v1"
_EXIT_RULE = "mean_reversion_reverted_v1"
_TIME_STOP_RULE = "mean_reversion_time_stop_v1"


class _Settings(NamedTuple):
    lookback: int
    entry_z: Decimal
    exit_z: Decimal
    max_holding: int


class _Reading(NamedTuple):
    close: Decimal
    mean: Decimal
    zscore: Decimal
    sessions: int


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the host-bound selected universe as a canonical ID tuple."""
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _int_param(parameters: StrategyParameters, name: str, default: int) -> int | None:
    value = parameters.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _settings(parameters: StrategyParameters) -> _Settings | None:
    """Validate parameters; ``None`` fails the strategy closed."""
    lookback = _int_param(parameters, "lookback_sessions", 20)
    max_holding = _int_param(parameters, "max_holding_sessions", 10)
    entry_z = _decimal(parameters.get("entry_zscore", 2.0))
    exit_z = _decimal(parameters.get("exit_zscore", 0.0))
    if (
        lookback is None
        or lookback < 2
        or max_holding is None
        or max_holding < 1
        or entry_z is None
        or entry_z <= 0
        or exit_z is None
    ):
        return None
    return _Settings(lookback, entry_z, exit_z, max_holding)


def _history_sessions(
    view: MarketViewV1, security_id: str, limit: int
) -> tuple[list[date], list[Decimal]] | None:
    """Return fresh, clean ``(sessions, closes)`` or ``None``."""
    try:
        history = view.price_history(security_id, limit=limit, columns=("close",))
        if history is None:
            return None
        sessions = [_as_date(value) for value in history.index]
        closes = [_decimal(value) for value in history["close"]]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if (
        not sessions
        or any(s is None for s in sessions)
        or any(c is None for c in closes)
        or sessions[-1] != view.as_of_session
    ):
        return None
    return (
        [s for s in sessions if s is not None],
        [c for c in closes if c is not None],
    )


def _reading(
    view: MarketViewV1, security_id: str, settings: _Settings
) -> _Reading | None:
    """Return today's z-score, or ``None`` without fresh, clean evidence."""
    data = _history_sessions(view, security_id, settings.lookback)
    if data is None or len(data[1]) < settings.lookback:
        return None
    closes = data[1][-settings.lookback :]
    count = Decimal(len(closes))
    mean = sum(closes, Decimal(0)) / count
    variance = sum(((c - mean) ** 2 for c in closes), Decimal(0)) / count
    if variance <= 0:
        return None
    zscore = (closes[-1] - mean) / variance.sqrt()
    return _Reading(closes[-1], mean, zscore, len(closes))


def _sessions_held(
    view: MarketViewV1, position: PositionSummaryV1, settings: _Settings
) -> int | None:
    """Sessions held, entry session counted first; ``None`` if unknown."""
    if position.entry_session is None:
        return None
    data = _history_sessions(view, position.security_id, settings.max_holding + 1)
    if data is None:
        return None
    return sum(1 for s in data[0] if s >= position.entry_session)


def _signal(
    view: MarketViewV1,
    security_id: str,
    side: SignalSide,
    rule_id: str,
    code: str,
    summary: str,
    reading: _Reading,
    threshold: Decimal,
) -> Signal:
    session = view.as_of_session
    return Signal(
        security_id=security_id,
        side=side,
        session=session,
        rule_id=rule_id,
        explanation=SignalExplanationV1(
            reasons=[
                SignalReasonV1(
                    code=code,
                    summary=summary,
                    facts=[
                        ExplanationFactV1(
                            label="Close z-score",
                            observed=reading.zscore,
                            operator=(
                                ComparisonOperator.LTE
                                if side == SignalSide.BUY
                                else ComparisonOperator.GTE
                            ),
                            threshold=threshold,
                            unit=EvidenceUnit.SCORE,
                            as_of=session,
                        ),
                        ExplanationFactV1(
                            label="Close",
                            observed=reading.close,
                            unit=EvidenceUnit.PRICE,
                            as_of=session,
                        ),
                        ExplanationFactV1(
                            label="Rolling mean",
                            observed=reading.mean,
                            unit=EvidenceUnit.PRICE,
                        ),
                        ExplanationFactV1(
                            label="Lookback",
                            observed=Decimal(reading.sessions),
                            unit=EvidenceUnit.SESSIONS,
                        ),
                    ],
                )
            ]
        ),
    )


def _rank(candidates: list[tuple[Signal, Decimal]]) -> list[Signal]:
    """Order by depth of oversold; encode as descending positive priority."""
    ordered = sorted(candidates, key=lambda item: (item[1], item[0].security_id))
    count = len(ordered)
    return [
        signal.model_copy(update={"priority": Decimal(count - index)})
        for index, (signal, _) in enumerate(ordered)
    ]


class MeanReversionStrategy:
    """Buy statistically oversold closes; sell on reversion or a time stop."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare ``lookback_sessions`` closes for entry and exit."""
        settings = _settings(parameters)
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=settings.lookback if settings else 20,
            columns=("close",),
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        settings = _settings(parameters)
        universe = _universe(parameters)
        if settings is None or not entry_signals_permitted(view, parameters, universe):
            return []
        candidates: list[tuple[Signal, Decimal]] = []
        for security_id in universe:
            reading = _reading(view, security_id, settings)
            if reading is None or reading.zscore > -settings.entry_z:
                continue
            signal = _signal(
                view,
                security_id,
                SignalSide.BUY,
                _ENTRY_RULE,
                "oversold_vs_mean",
                "The close fell far below its rolling mean.",
                reading,
                -settings.entry_z,
            )
            candidates.append((signal, reading.zscore))
        return _rank(candidates)

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        settings = _settings(parameters)
        if settings is None:
            return []
        universe = set(_universe(parameters))
        signals: list[Signal] = []
        for position in portfolio.positions:
            if position.quantity <= 0 or position.security_id not in universe:
                continue
            reading = _reading(view, position.security_id, settings)
            if reading is None:
                continue
            held = _sessions_held(view, position, settings)
            if reading.zscore >= settings.exit_z:
                outcome = (_EXIT_RULE, "reverted_to_mean", "The close recovered.")
            elif held is not None and held >= settings.max_holding:
                outcome = (_TIME_STOP_RULE, "time_stop", "Holding period reached.")
            else:
                continue
            signals.append(
                _signal(
                    view,
                    position.security_id,
                    SignalSide.SELL,
                    *outcome,
                    reading,
                    settings.exit_z,
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
        if signal.side == SignalSide.BUY:
            # The engine reserves equal capital and determines whole shares.
            return 0
        for position in portfolio.positions:
            if position.security_id == signal.security_id:
                return position.quantity
        return 0
