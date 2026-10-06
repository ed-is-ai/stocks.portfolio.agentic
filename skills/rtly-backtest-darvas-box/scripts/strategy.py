"""Deterministic long-only Darvas box Strategy for bounded backtests."""

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

STRATEGY_ID = "rtly-backtest-darvas-box"
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


def _ranking_reason(
    *,
    policy: str,
    reading: _MomentumReading,
    rank: int,
    count: int,
    priority: Decimal,
    component_facts: tuple[ExplanationFactV1, ...] = (),
) -> SignalReasonV1:
    facts = [
        ExplanationFactV1(label="Ranking policy", observed=policy),
        ExplanationFactV1(
            label="Momentum",
            observed=None if reading.value is None else reading.value * Decimal(100),
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
            label="Candidate count", observed=Decimal(count), unit=EvidenceUnit.COUNT
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
    facts.extend(component_facts)
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
    return SignalReasonV1(
        code="entry_ranking",
        summary=f"Ranked {rank} of {count} under {policy}; momentum {momentum_text}.",
        facts=facts,
    )


class _EntryCandidate(NamedTuple):
    signal: Signal
    current_volume: Decimal
    mean_volume: Decimal
    relative_volume: Decimal


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


class DarvasBoxStrategy:
    """Buy strict prior-box breakouts and sell strict box-bottom breaks."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare the box window both paths guard on (``lookback + 1``)."""
        lookback = _plain_int(parameters, "box_lookback_sessions") or 20
        sessions = max(lookback, 1) + 1
        return StrategyEvidenceRequirementsV1(
            entry=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=sessions,
                    columns=("high", "low", "close", "volume"),
                ),
            ),
            exit=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=sessions,
                    columns=("low", "close"),
                ),
            ),
        )

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        candidates = [
            self._entry_signal(view, parameters, security_id)
            for security_id in universe
        ]
        qualified = [candidate for candidate in candidates if candidate is not None]
        ranked = [
            (
                candidate,
                _momentum_reading(view, candidate.signal.security_id),
            )
            for candidate in qualified
        ]
        ordered = sorted(
            ranked,
            key=lambda item: (
                item[1].value is None,
                -(item[1].value or Decimal(0)),
                -item[0].relative_volume,
                item[0].signal.security_id,
            ),
        )
        count = len(ordered)
        signals: list[Signal] = []
        for rank, (candidate, reading) in enumerate(ordered, start=1):
            priority = Decimal(count - rank + 1)
            assert candidate.signal.explanation is not None
            explanation = SignalExplanationV1(
                reasons=(
                    *candidate.signal.explanation.reasons,
                    _ranking_reason(
                        policy="darvas_momentum_then_relative_volume_v1",
                        reading=reading,
                        rank=rank,
                        count=count,
                        priority=priority,
                        component_facts=(
                            ExplanationFactV1(
                                label="Current volume",
                                observed=candidate.current_volume,
                                unit=EvidenceUnit.COUNT,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Prior box-window mean volume",
                                observed=candidate.mean_volume,
                                unit=EvidenceUnit.COUNT,
                            ),
                            ExplanationFactV1(
                                label="Current volume / prior mean",
                                observed=candidate.relative_volume,
                                unit=EvidenceUnit.RATIO,
                            ),
                        ),
                    ),
                )
            )
            signals.append(
                candidate.signal.model_copy(
                    update={"priority": priority, "explanation": explanation}
                )
            )
        return signals

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
    ) -> _EntryCandidate | None:
        lookback = _plain_int(parameters, "box_lookback_sessions")
        maximum_depth = _decimal(parameters.get("maximum_box_depth_pct"))
        volume_multiplier = _decimal(parameters.get("volume_multiplier"))
        history = _bounded_history(
            view,
            security_id,
            limit=max(lookback, 1) + 1 if lookback is not None else 1,
            columns=("high", "low", "close", "volume"),
        )
        if (
            lookback is None
            or lookback < 1
            or maximum_depth is None
            or volume_multiplier is None
            or history is None
            or len(history) < lookback + 1
        ):
            return None

        prior = history.iloc[-lookback - 1 : -1]
        current = history.iloc[-1]
        try:
            highs = [_decimal(value) for value in prior["high"]]
            lows = [_decimal(value) for value in prior["low"]]
            volumes = [_decimal(value) for value in prior["volume"]]
            current_close = _decimal(current["close"])
            current_volume = _decimal(current["volume"])
        except (KeyError, TypeError):
            return None
        if (
            any(value is None for value in highs + lows + volumes)
            or current_close is None
            or current_volume is None
        ):
            return None

        valid_highs = [value for value in highs if value is not None]
        valid_lows = [value for value in lows if value is not None]
        valid_volumes = [value for value in volumes if value is not None]
        box_top = max(valid_highs)
        box_bottom = min(valid_lows)
        if box_top <= 0 or box_bottom < 0:
            return None
        box_depth_pct = (box_top - box_bottom) * Decimal(100) / box_top
        mean_volume = sum(valid_volumes, Decimal(0)) / Decimal(lookback)
        if (
            mean_volume <= 0
            or current_volume < 0
            or box_depth_pct > maximum_depth
            or current_close <= box_top
            or current_volume < mean_volume * volume_multiplier
        ):
            return None
        signal = Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id="darvas_box_breakout_v1",
            explanation=SignalExplanationV1(
                reasons=[
                    SignalReasonV1(
                        code="box_breakout",
                        summary=("Close broke out above the prior Darvas box top."),
                        facts=[
                            ExplanationFactV1(
                                label="Close",
                                observed=current_close,
                                operator=ComparisonOperator.GT,
                                threshold=box_top,
                                unit=EvidenceUnit.PRICE,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Box window",
                                observed=Decimal(lookback),
                                unit=EvidenceUnit.SESSIONS,
                            ),
                        ],
                    ),
                    SignalReasonV1(
                        code="box_depth_within_limit",
                        summary=(
                            "The box was tight enough to trade -- its depth "
                            "stayed within the configured limit."
                        ),
                        facts=[
                            ExplanationFactV1(
                                label="Box depth",
                                observed=box_depth_pct,
                                operator=ComparisonOperator.LTE,
                                threshold=maximum_depth,
                                unit=EvidenceUnit.PERCENT,
                            ),
                        ],
                    ),
                    SignalReasonV1(
                        code="volume_expansion",
                        summary=(
                            "Breakout volume expanded above its prior-box average."
                        ),
                        facts=[
                            ExplanationFactV1(
                                label="Volume",
                                observed=current_volume,
                                operator=ComparisonOperator.GTE,
                                threshold=mean_volume * volume_multiplier,
                                unit=EvidenceUnit.COUNT,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Required multiple of average volume",
                                observed=volume_multiplier,
                                unit=EvidenceUnit.RATIO,
                            ),
                        ],
                    ),
                ]
            ),
        )
        return _EntryCandidate(
            signal=signal,
            current_volume=current_volume,
            mean_volume=mean_volume,
            relative_volume=current_volume / mean_volume,
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
        lookback = _plain_int(parameters, "box_lookback_sessions")
        history = _bounded_history(
            view,
            security_id,
            limit=max(lookback, 1) + 1 if lookback is not None else 1,
            columns=("low", "close"),
        )
        if (
            lookback is None
            or lookback < 1
            or history is None
            or len(history) < lookback + 1
        ):
            return None
        prior = history.iloc[-lookback - 1 : -1]
        current = history.iloc[-1]
        try:
            lows = [_decimal(value) for value in prior["low"]]
            current_close = _decimal(current["close"])
        except (KeyError, TypeError):
            return None
        if any(value is None for value in lows) or current_close is None:
            return None
        box_bottom = min(value for value in lows if value is not None)
        if current_close >= box_bottom:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id="darvas_box_breakdown_v1",
            explanation=SignalExplanationV1(
                reasons=[
                    SignalReasonV1(
                        code="box_bottom_break",
                        summary=("Close broke below the prior Darvas box bottom."),
                        facts=[
                            ExplanationFactV1(
                                label="Close",
                                observed=current_close,
                                operator=ComparisonOperator.LT,
                                threshold=box_bottom,
                                unit=EvidenceUnit.PRICE,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Box window",
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

        Next session's prior window is today's latest ``box_lookback_sessions``
        bars, so the level is their lowest low; a close strictly below it
        exits.
        """
        if _held_quantity(portfolio, security_id) == 0:
            return None
        lookback = _plain_int(parameters, "box_lookback_sessions")
        if lookback is None or lookback < 1:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy setting box_lookback_sessions is missing or unusable.",
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
                label="Box bottom",
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
            rule_code="close_below_box_bottom",
            summary=f"Close below the box bottom ({lookback}-day low)",
            facts=facts,
            basis="market",
            trigger="close_lt",
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


strategy = DarvasBoxStrategy()
