"""Deterministic long-only simple moving-average crossover strategy."""

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

STRATEGY_ID = "rtly-backtest-moving-average"
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


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    date_method = getattr(value, "date", None)
    if callable(date_method):
        try:
            result = date_method()
        except (TypeError, ValueError, OverflowError):
            return None
        return result if isinstance(result, date) else None
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
    sessions = [_as_date(value) for value in history.index]
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
        numerator = _finite_decimal(numerator_row["close"])
        denominator = _finite_decimal(denominator_row["close"])
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


def _fresh_history(view: MarketViewV1, security_id: str, *, limit: int) -> Any | None:
    try:
        history = view.price_history(security_id, limit=limit, columns=("close",))
        if history is None or len(history.index) == 0:
            return None
        latest_session = _as_date(history.index[-1])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    return history if latest_session == view.as_of_session else None


def _finite_decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _close_values(history: Any) -> list[Decimal] | None:
    try:
        raw_values = list(history["close"])
    except (KeyError, TypeError, AttributeError):
        return None
    values = [_finite_decimal(value) for value in raw_values]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


def _windows(parameters: StrategyParameters) -> tuple[int, int] | None:
    fast = parameters.get("fast_window", 50)
    slow = parameters.get("slow_window", 200)
    if (
        isinstance(fast, bool)
        or not isinstance(fast, int)
        or isinstance(slow, bool)
        or not isinstance(slow, int)
        or fast < 1
        or slow < 2
        or fast >= slow
    ):
        return None
    return fast, slow


class _CrossoverReading(NamedTuple):
    """The crossover verdict plus the moving averages that produced it."""

    direction: int
    fast_window: int
    slow_window: int
    previous_fast: Decimal
    previous_slow: Decimal
    current_fast: Decimal
    current_slow: Decimal


def _crossover(
    view: MarketViewV1, parameters: StrategyParameters, security_id: str
) -> _CrossoverReading | None:
    """Return today's crossover reading, or ``None`` without enough evidence.

    The decision itself is unchanged; the computed averages ride along so
    the emitted signal can explain itself (#472).
    """
    windows = _windows(parameters)
    history = _fresh_history(
        view,
        security_id,
        limit=windows[1] + 1 if windows is not None else 201,
    )
    if windows is None or history is None:
        return None
    fast, slow = windows
    closes = _close_values(history)
    if closes is None or len(closes) < slow + 1:
        return None

    previous_fast = sum(closes[-fast - 1 : -1]) / Decimal(fast)
    previous_slow = sum(closes[-slow - 1 : -1]) / Decimal(slow)
    current_fast = sum(closes[-fast:]) / Decimal(fast)
    current_slow = sum(closes[-slow:]) / Decimal(slow)
    if previous_fast <= previous_slow and current_fast > current_slow:
        direction = 1
    elif previous_fast >= previous_slow and current_fast < current_slow:
        direction = -1
    else:
        direction = 0
    return _CrossoverReading(
        direction=direction,
        fast_window=fast,
        slow_window=slow,
        previous_fast=previous_fast,
        previous_slow=previous_slow,
        current_fast=current_fast,
        current_slow=current_slow,
    )


def _crossover_explanation(
    reading: _CrossoverReading, session: date
) -> SignalExplanationV1:
    """Explain one bullish/bearish SMA crossover in shared, generic terms."""
    bullish = reading.direction == 1
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="bullish_ma_crossover" if bullish else "bearish_ma_crossover",
                summary=(
                    "The fast moving average crossed above the slow moving average."
                    if bullish
                    else "The fast moving average crossed below the slow "
                    "moving average."
                ),
                facts=[
                    ExplanationFactV1(
                        label="Fast moving average",
                        observed=reading.current_fast,
                        operator=(
                            ComparisonOperator.CROSSED_ABOVE
                            if bullish
                            else ComparisonOperator.CROSSED_BELOW
                        ),
                        threshold=reading.current_slow,
                        unit=EvidenceUnit.PRICE,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Previous fast moving average",
                        observed=reading.previous_fast,
                        unit=EvidenceUnit.PRICE,
                    ),
                    ExplanationFactV1(
                        label="Previous slow moving average",
                        observed=reading.previous_slow,
                        unit=EvidenceUnit.PRICE,
                    ),
                    ExplanationFactV1(
                        label="Fast window",
                        observed=Decimal(reading.fast_window),
                        unit=EvidenceUnit.SESSIONS,
                    ),
                    ExplanationFactV1(
                        label="Slow window",
                        observed=Decimal(reading.slow_window),
                        unit=EvidenceUnit.SESSIONS,
                    ),
                ],
            ),
        ]
    )


def _directional_signal(
    view: MarketViewV1,
    parameters: StrategyParameters,
    security_id: str,
    *,
    direction: int,
) -> Signal | None:
    """Return the crossover signal for ``direction``, or ``None``."""
    reading = _crossover(view, parameters, security_id)
    if reading is None or reading.direction != direction:
        return None
    bullish = direction == 1
    return Signal(
        security_id=security_id,
        side=SignalSide.BUY if bullish else SignalSide.SELL,
        session=view.as_of_session,
        rule_id=(
            "moving_average_bullish_crossover_v1"
            if bullish
            else "moving_average_bearish_crossover_v1"
        ),
        explanation=_crossover_explanation(reading, view.as_of_session),
    )


class MovingAverageStrategy:
    """Emit signals only on a true fast/slow SMA crossover."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare ``slow_window + 1`` closes — the crossover's own guard."""
        windows = _windows(parameters)
        slow = 200 if windows is None else windows[1]
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=slow + 1,
            columns=("close",),
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        signals = [
            _directional_signal(view, parameters, security_id, direction=1)
            for security_id in universe
        ]
        return _rank_entries(
            view,
            [signal for signal in signals if signal is not None],
            policy="moving_average_momentum_v1",
        )

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        held = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0
        }
        signals = [
            _directional_signal(view, parameters, security_id, direction=-1)
            for security_id in _universe(parameters)
            if security_id in held
        ]
        return [signal for signal in signals if signal is not None]

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the close below which the next session's exit crossover fires.

        With ``Sf``/``Ss`` the sums of today's latest ``fast - 1``/``slow - 1``
        closes, the next session's fast SMA falls below its slow SMA exactly
        when its close ``x < (fast * Ss - slow * Sf) / (slow - fast)``. A fast
        SMA already below the slow one leaves no crossover to fire.
        """
        if not any(
            position.security_id == security_id and position.quantity > 0
            for position in portfolio.positions
        ):
            return None
        windows = _windows(parameters)
        if windows is None:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary=(
                    "Strategy setting fast_window/slow_window is missing or unusable."
                ),
            )
        fast, slow = windows
        history = _fresh_history(view, security_id, limit=slow)
        closes = None if history is None else _close_values(history)
        if closes is None or len(closes) < slow:
            return StopLevelV1(
                rule_code="insufficient_history",
                summary=f"Needs {slow} sessions of current closes.",
            )
        fast_today = sum(closes[-fast:], Decimal(0)) / Decimal(fast)
        slow_today = sum(closes, Decimal(0)) / Decimal(slow)
        facts = [
            ExplanationFactV1(
                label="Fast moving average",
                observed=fast_today,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
            ExplanationFactV1(
                label="Slow moving average",
                observed=slow_today,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
        ]
        if fast_today < slow_today:
            return StopLevelV1(
                rule_code="already_crossed",
                summary="Fast average already below slow — no crossover to fire.",
                facts=facts,
            )
        fast_sum = sum(closes[slow - fast + 1 :], Decimal(0))
        slow_sum = sum(closes[1:], Decimal(0))
        level = (fast * slow_sum - slow * fast_sum) / Decimal(slow - fast)
        if level <= 0:
            return StopLevelV1(
                rule_code="no_level",
                summary="No crossover price within reach.",
                facts=facts,
            )
        return StopLevelV1(
            level=level,
            rule_code="bearish_ma_crossover",
            summary=f"Close below the {fast}/{slow}-day crossover price",
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
        if signal.side == SignalSide.BUY:
            # The engine reserves equal capital and determines whole shares.
            return 0
        for position in portfolio.positions:
            if position.security_id == signal.security_id:
                return position.quantity
        return 0
