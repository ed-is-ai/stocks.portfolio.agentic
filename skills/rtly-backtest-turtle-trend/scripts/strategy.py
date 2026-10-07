"""Deterministic long-only Turtle channel Strategy for bounded backtests."""

from __future__ import annotations

from datetime import date, datetime
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
    BaseCurrencyCloseHistoryViewV1,
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-turtle-trend"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the host-bound selected universe as a canonical ID tuple.

    The host injects an already sorted, deduplicated tuple; re-deriving it
    here keeps iteration deterministic for any caller and makes a
    malformed or empty universe fail closed with no signals.
    """
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _session_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, date) else None
    return None


class _MomentumReading(NamedTuple):
    value: Decimal | None
    missing_reason: str | None
    numerator_session: date | None
    denominator_session: date | None
    currency: str


def _momentum_reading(view: MarketViewV1, security_id: str) -> _MomentumReading:
    """Read the fixed 21-session return from 253 bounded Run-currency rows."""
    if not isinstance(view, BaseCurrencyCloseHistoryViewV1):
        return _MomentumReading(
            None, "base_currency_history_unavailable", None, None, "unavailable"
        )
    currency = view.base_currency
    history = view.base_currency_close_history(security_id, limit=253)
    if history is None or getattr(history, "empty", True):
        return _MomentumReading(
            None, "current_base_currency_close_unavailable", None, None, currency
        )
    sessions = [_session_date(value) for value in history.index]
    if any(session is None for session in sessions):
        raise ValueError("Base-currency close history has an invalid session index.")
    canonical_sessions = [session for session in sessions if session is not None]
    if canonical_sessions != sorted(set(canonical_sessions)):
        raise ValueError(
            "Base-currency close history sessions are unordered or duplicated."
        )
    if any(session > view.as_of_session for session in canonical_sessions):
        raise ValueError("Base-currency close history contains a future session.")
    if len(canonical_sessions) < 253:
        numerator = canonical_sessions[-22] if len(canonical_sessions) >= 22 else None
        return _MomentumReading(
            None, "insufficient_price_history", numerator, None, currency
        )
    rows = history.iloc[-253:]
    canonical_sessions = canonical_sessions[-253:]
    numerator_session = canonical_sessions[-22]
    denominator_session = canonical_sessions[-253]
    if canonical_sessions[-1] != view.as_of_session:
        return _MomentumReading(
            None,
            "current_base_currency_close_unavailable",
            numerator_session,
            denominator_session,
            currency,
        )
    try:
        numerator_row = rows.iloc[-22]
        denominator_row = rows.iloc[-253]
        numerator = _decimal(numerator_row["close"])
        denominator = _decimal(denominator_row["close"])
    except (KeyError, IndexError, TypeError):
        return _MomentumReading(
            None,
            "close_endpoint_unavailable",
            numerator_session,
            denominator_session,
            currency,
        )
    if numerator is None:
        reason = numerator_row.get("reason")
        return _MomentumReading(
            None,
            reason
            if reason in {"fx_missing", "fx_stale", "fx_outside_coverage"}
            else "invalid_momentum_endpoint",
            numerator_session,
            denominator_session,
            currency,
        )
    if numerator <= 0 or denominator is None or denominator <= 0:
        reason = denominator_row.get("reason") if denominator is None else None
        return _MomentumReading(
            None,
            reason
            if reason in {"fx_missing", "fx_stale", "fx_outside_coverage"}
            else "invalid_momentum_endpoint",
            numerator_session,
            denominator_session,
            currency,
        )
    return _MomentumReading(
        numerator / denominator - Decimal(1),
        None,
        numerator_session,
        denominator_session,
        currency,
    )


def _rank_entries(
    view: MarketViewV1, signals: list[Signal], *, policy: str
) -> list[Signal]:
    ranked_candidates = [
        (signal, _momentum_reading(view, signal.security_id)) for signal in signals
    ]
    ordered = sorted(
        ranked_candidates,
        key=lambda item: (
            item[1].value is None,
            -(item[1].value or Decimal(0)),
            item[0].security_id,
        ),
    )
    count = len(ordered)
    ranked: list[Signal] = []
    for rank, (signal, reading) in enumerate(ordered, start=1):
        priority = Decimal(count - rank + 1)
        assert signal.explanation is not None
        facts = [
            ExplanationFactV1(label="Ranking policy", observed=policy),
            ExplanationFactV1(
                label="Momentum",
                observed=None
                if reading.value is None
                else reading.value * Decimal(100),
                unit=EvidenceUnit.PERCENT,
            ),
            ExplanationFactV1(label="Momentum currency", observed=reading.currency),
            ExplanationFactV1(
                label="Momentum numerator offset",
                observed=Decimal(21),
                unit=EvidenceUnit.SESSIONS,
            ),
            ExplanationFactV1(
                label="Momentum denominator offset",
                observed=Decimal(252),
                unit=EvidenceUnit.SESSIONS,
            ),
            ExplanationFactV1(
                label="Candidate count",
                observed=Decimal(count),
                unit=EvidenceUnit.COUNT,
            ),
            ExplanationFactV1(
                label="Ordinal rank", observed=Decimal(rank), unit=EvidenceUnit.COUNT
            ),
            ExplanationFactV1(
                label="Encoded priority", observed=priority, unit=EvidenceUnit.SCORE
            ),
            ExplanationFactV1(
                label="Momentum numerator session",
                observed=reading.numerator_session.isoformat()
                if reading.numerator_session
                else None,
                as_of=reading.numerator_session,
            ),
            ExplanationFactV1(
                label="Momentum denominator session",
                observed=reading.denominator_session.isoformat()
                if reading.denominator_session
                else None,
                as_of=reading.denominator_session,
            ),
        ]
        if reading.missing_reason is not None:
            facts.append(
                ExplanationFactV1(
                    label="Momentum unavailable reason", observed=reading.missing_reason
                )
            )
        momentum_text = (
            "unavailable"
            if reading.value is None
            else f"{(reading.value * Decimal(100)).to_eng_string()}%"
        )
        reason = SignalReasonV1(
            code="entry_ranking",
            summary=f"Ranked {rank} of {count} under {policy}; momentum {momentum_text}.",
            facts=facts,
        )
        explanation = SignalExplanationV1(reasons=(*signal.explanation.reasons, reason))
        ranked.append(
            signal.model_copy(update={"priority": priority, "explanation": explanation})
        )
    return ranked


def _bounded_history(
    view: MarketViewV1,
    security_id: str,
    *,
    limit: int,
    columns: tuple[str, ...],
) -> Any | None:
    history = view.price_history(security_id, limit=limit, columns=columns)
    if history is None or getattr(history, "empty", True):
        return None
    try:
        latest_session = _session_date(history.index[-1])
    except (IndexError, KeyError, TypeError):
        return None
    return history if latest_session == view.as_of_session else None


def _plain_int(parameters: StrategyParameters, name: str) -> int | None:
    value = parameters.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _held_quantity(portfolio: PortfolioView, security_id: str) -> Decimal:
    for position in portfolio.positions:
        if position.security_id != security_id:
            continue
        quantity = position.quantity
        return quantity if quantity > 0 else Decimal(0)
    return Decimal(0)


def _channel_values(
    history: Any, column: str, lookback: int
) -> tuple[list[Decimal], Decimal] | None:
    if len(history) < lookback + 1:
        return None
    prior = history.iloc[-lookback - 1 : -1]
    current = history.iloc[-1]
    try:
        prior_values = [_decimal(value) for value in prior[column]]
        current_value = _decimal(current[column])
    except (KeyError, TypeError):
        return None
    if any(value is None for value in prior_values) or current_value is None:
        return None
    return [value for value in prior_values if value is not None], current_value


class TurtleTrendStrategy:
    """Buy strict high-channel breaks and sell strict low-channel breaches."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare each channel's own ``lookback + 1`` session window."""
        entry_lookback = _plain_int(parameters, "entry_lookback_sessions") or 20
        exit_lookback = _plain_int(parameters, "exit_lookback_sessions") or 10
        return StrategyEvidenceRequirementsV1(
            entry=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=max(entry_lookback, 1) + 1,
                    columns=("high",),
                ),
            ),
            exit=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=max(exit_lookback, 1) + 1,
                    columns=("low",),
                ),
            ),
        )

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        signals = [
            self._entry_signal(view, parameters, security_id)
            for security_id in universe
        ]
        return _rank_entries(
            view,
            [signal for signal in signals if signal is not None],
            policy="turtle_momentum_v1",
        )

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        signals = [
            self._exit_signal(view, portfolio, parameters, security_id)
            for security_id in _universe(parameters)
        ]
        return [signal for signal in signals if signal is not None]

    def _entry_signal(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> Signal | None:
        lookback = _plain_int(parameters, "entry_lookback_sessions")
        history = _bounded_history(
            view,
            security_id,
            limit=max(lookback, 1) + 1 if lookback is not None else 1,
            columns=("high",),
        )
        if lookback is None or lookback < 1 or history is None:
            return None
        values = _channel_values(history, "high", lookback)
        if values is None:
            return None
        prior_highs, current_high = values
        channel_high = max(prior_highs)
        if current_high <= channel_high:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id="turtle_entry_channel_breakout_v1",
            explanation=SignalExplanationV1(
                reasons=[
                    SignalReasonV1(
                        code="channel_breakout",
                        summary=(
                            "Today's high broke above the prior entry "
                            "channel's highest high."
                        ),
                        facts=[
                            ExplanationFactV1(
                                label="High",
                                observed=current_high,
                                operator=ComparisonOperator.GT,
                                threshold=channel_high,
                                unit=EvidenceUnit.PRICE,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Entry channel lookback",
                                observed=Decimal(lookback),
                                unit=EvidenceUnit.SESSIONS,
                            ),
                        ],
                    ),
                ]
            ),
        )

    def _exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> Signal | None:
        if _held_quantity(portfolio, security_id) == 0:
            return None
        lookback = _plain_int(parameters, "exit_lookback_sessions")
        history = _bounded_history(
            view,
            security_id,
            limit=max(lookback, 1) + 1 if lookback is not None else 1,
            columns=("low",),
        )
        if lookback is None or lookback < 1 or history is None:
            return None
        values = _channel_values(history, "low", lookback)
        if values is None:
            return None
        prior_lows, current_low = values
        channel_low = min(prior_lows)
        if current_low >= channel_low:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id="turtle_exit_channel_breach_v1",
            explanation=SignalExplanationV1(
                reasons=[
                    SignalReasonV1(
                        code="channel_breach",
                        summary=(
                            "Today's low breached the prior exit channel's lowest low."
                        ),
                        facts=[
                            ExplanationFactV1(
                                label="Low",
                                observed=current_low,
                                operator=ComparisonOperator.LT,
                                threshold=channel_low,
                                unit=EvidenceUnit.PRICE,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Exit channel lookback",
                                observed=Decimal(lookback),
                                unit=EvidenceUnit.SESSIONS,
                            ),
                        ],
                    ),
                ]
            ),
        )

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the level :meth:`_exit_signal` breaks next session.

        Next session's prior window is today's latest ``exit_lookback_sessions``
        bars, so the level is their lowest low; a low strictly below it
        exits.
        """
        if _held_quantity(portfolio, security_id) == 0:
            return None
        lookback = _plain_int(parameters, "exit_lookback_sessions")
        if lookback is None or lookback < 1:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy setting exit_lookback_sessions is missing or unusable.",
            )
        history = _bounded_history(view, security_id, limit=lookback, columns=("low",))
        lows = (
            []
            if history is None or len(history) < lookback
            else [_decimal(value) for value in history["low"]]
        )
        if not lows or any(value is None for value in lows):
            return StopLevelV1(
                rule_code="insufficient_history",
                summary=f"Needs {lookback} sessions of current lows.",
            )
        level = min(value for value in lows if value is not None)
        facts = [
            ExplanationFactV1(
                label="Exit channel low",
                observed=level,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
            ExplanationFactV1(
                label="Lookback",
                observed=Decimal(lookback),
                unit=EvidenceUnit.SESSIONS,
            ),
        ]
        if level <= 0:
            return StopLevelV1(
                rule_code="no_level", summary="No positive stop level.", facts=facts
            )
        return StopLevelV1(
            level=level,
            rule_code="low_below_exit_channel",
            summary=f"Low below the {lookback}-day low",
            facts=facts,
            basis="market",
            trigger="low_lt",
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int | Decimal:
        if signal.side == SignalSide.SELL:
            return _held_quantity(portfolio, signal.security_id)
        # The engine reserves equal capital and determines whole shares.
        return 0


strategy = TurtleTrendStrategy()
