"""Deterministic passive buy-and-hold benchmark strategy."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Any

from app.services.backtest.regime_filter import entry_signals_permitted
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionV1,
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-buy-and-hold"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
METRIC_ID = "split_adjusted_close_return_252_sessions"
METRIC_VERSION = "v1"
RULE_ID = "buy_and_hold_top_x_entry_v1"
_LOOKBACK_CLOSES = 253
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_EXCLUDED_CUTOFF = "entry_cutoff_not_reached"
_EXCLUDED_INVALID_CUTOFF = "invalid_entry_cutoff"
_EXCLUDED_HISTORY_UNAVAILABLE = "history_unavailable"
_EXCLUDED_INSUFFICIENT_HISTORY = "insufficient_history"
_EXCLUDED_INVALID_CLOSE = "invalid_close_history"
_EXCLUDED_REGIME_FILTER = "regime_filter_not_permitted"


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


def _strength_score(
    view: MarketViewV1, security_id: str
) -> tuple[Decimal | None, str | None]:
    """Return the point-in-time 252-session return or a stable exclusion.

    The Strategy sees the bounded split-continuous plane only.  Filtering the
    frame by a session strictly before ``as_of_session`` makes the metric
    independent of selection-day and future evidence even when a test double
    accidentally offers it.
    """
    try:
        history: Any = view.price_history(security_id)
        if history is None or not hasattr(history, "index"):
            return None, _EXCLUDED_HISTORY_UNAVAILABLE
        rows = [
            row
            for index, (_, row) in zip(history.index, history.iterrows(), strict=True)
            if (session := _as_date(index)) is not None and session < view.as_of_session
        ]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None, _EXCLUDED_HISTORY_UNAVAILABLE

    if len(rows) < _LOOKBACK_CLOSES:
        return None, _EXCLUDED_INSUFFICIENT_HISTORY

    window = rows[-_LOOKBACK_CLOSES:]
    try:
        closes = tuple(Decimal(str(row["close"])) for row in window)
    except InvalidOperation:
        return None, _EXCLUDED_INVALID_CLOSE
    except (KeyError, TypeError, ValueError):
        return None, _EXCLUDED_INVALID_CLOSE
    if any(not close.is_finite() or close <= 0 for close in closes):
        return None, _EXCLUDED_INVALID_CLOSE
    try:
        with localcontext(_DECIMAL_CONTEXT):
            return (closes[-1] / closes[0]) - Decimal(1), None
    except (ArithmeticError, InvalidOperation):
        return None, _EXCLUDED_INVALID_CLOSE


def _cutoff(parameters: StrategyParameters) -> date | None:
    raw = parameters.get("entry_on_or_after", "2000-01-01")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


class BuyAndHoldStrategy:
    """Select one ranked passive basket and never emit an ordinary exit."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        """Initial-entry providers have no recurring V1 entry path."""
        del view, parameters
        return []

    def initial_entry_selection(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> InitialEntrySelectionV1:
        cutoff = _cutoff(parameters)
        universe = _universe(parameters)
        excluded_reason = (
            _EXCLUDED_INVALID_CUTOFF
            if cutoff is None
            else _EXCLUDED_CUTOFF
            if view.as_of_session < cutoff
            else _EXCLUDED_REGIME_FILTER
            if not entry_signals_permitted(view, parameters, universe)
            else None
        )
        eligible: list[tuple[str, Decimal]] = []
        excluded: list[tuple[str, str]] = []
        for security_id in universe:
            if excluded_reason is not None:
                excluded.append((security_id, excluded_reason))
                continue
            score, reason = _strength_score(view, security_id)
            if score is None:
                excluded.append((security_id, reason or _EXCLUDED_HISTORY_UNAVAILABLE))
            else:
                eligible.append((security_id, score))

        top_x = parameters.get("top_x", 10)
        # Shared parameter validation canonicalizes this before the engine
        # executes.  This defensive fallback stays deterministic for direct
        # protocol consumers without accepting bool as an integer.
        if isinstance(top_x, bool) or not isinstance(top_x, int) or top_x < 1:
            top_x = 10
        ranked = sorted(eligible, key=lambda item: (-item[1], item[0]))
        selected_ids = {security_id for security_id, _ in ranked[:top_x]}
        decisions: list[EntrySelectionDecisionV1] = []
        for rank, (security_id, score) in enumerate(ranked, start=1):
            decisions.append(
                EntrySelectionDecisionV1(
                    security_id=security_id,
                    rank=rank,
                    state=(
                        EntrySelectionState.SELECTED
                        if security_id in selected_ids
                        else EntrySelectionState.ELIGIBLE_NOT_SELECTED
                    ),
                    score=score,
                )
            )
        for security_id, reason in sorted(excluded):
            decisions.append(
                EntrySelectionDecisionV1(
                    security_id=security_id,
                    rank=len(decisions) + 1,
                    state=EntrySelectionState.EXCLUDED,
                    reason_code=reason,
                )
            )
        signals = tuple(
            Signal(
                security_id=decision.security_id,
                side=SignalSide.BUY,
                session=view.as_of_session,
                rule_id=RULE_ID,
            )
            for decision in decisions
            if decision.state is EntrySelectionState.SELECTED
        )
        return InitialEntrySelectionV1(
            session=view.as_of_session,
            metric_id=METRIC_ID,
            metric_version=METRIC_VERSION,
            rule_id=RULE_ID,
            decisions=tuple(decisions),
            signals=signals,
        )

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        return []

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.BUY:
            # The engine reserves equal capital and determines whole shares.
            return 0
        for position in portfolio.positions:
            if position.security_id == signal.security_id:
                integral = position.quantity.to_integral_value()
                return int(integral) if position.quantity == integral else 0
        return 0
