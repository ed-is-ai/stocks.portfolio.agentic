"""Deterministic long-only Minervini VCP backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-minervini"
STRATEGY_API_VERSION = 1
_ENTRY_RULE = "minervini_vcp_breakout_v1"
_EXIT_RULE = "minervini_risk_exit_v1"
_UPGRADE_EXIT_RULE = "minervini_upgrade_exit_v1"


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


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _decimals(values: Any) -> list[Decimal] | None:
    result = [_decimal(value) for value in values]
    return None if any(value is None for value in result) else list(result)  # type: ignore[arg-type]


def _plain_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _session_date(value: Any) -> date | None:
    if type(value) is date:
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, date) else None
    return None


def _current_history(view: MarketViewV1, security_id: str) -> Any | None:
    history = view.price_history(security_id)
    if history.empty or _session_date(history.index[-1]) != view.as_of_session:
        return None
    if not {"close", "volume"}.issubset(history.columns):
        return None
    return history


def _visible_scan(view: MarketViewV1, security_id: str) -> Any | None:
    scan = view.scan_result(security_id)
    if scan is None or getattr(scan, "security_id", None) != security_id:
        return None
    as_of = getattr(scan, "as_of_session_date", None)
    if not isinstance(as_of, date) or as_of > view.as_of_session:
        return None
    return scan


def _position(portfolio: PortfolioView, security_id: str) -> Any | None:
    return next(
        (item for item in portfolio.positions if item.security_id == security_id),
        None,
    )


def _integral_quantity(portfolio: PortfolioView, security_id: str) -> int:
    held = _position(portfolio, security_id)
    if held is None or held.quantity <= 0:
        return 0
    integral = held.quantity.to_integral_value()
    return int(integral) if held.quantity == integral else 0


class MinerviniStrategy:
    """Apply approved VCP entry and risk-exit rules without mutable state."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        signals = [
            self._entry_signal(view, parameters, security_id)
            for security_id in _universe(parameters)
        ]
        return [signal for signal in signals if signal is not None]

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
        signals = [signal for signal in signals if signal is not None]
        upgrade = self._upgrade_exit_signal(
            view,
            portfolio,
            parameters,
            frozenset(signal.security_id for signal in signals),
        )
        if upgrade is not None:
            signals.append(upgrade)
        return signals

    def _entry_qualification(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> int | None:
        """Return this security's VCP score if it qualifies for entry today,
        else ``None``. Factored out of :meth:`_entry_signal` so the upgrade-
        exit ranking (below) can score a would-be candidate using the exact
        same qualification rules, without duplicating them."""
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        if history is None or scan is None or len(history) < 51:
            return None

        closes = _decimals(history["close"])
        volumes = _decimals(history["volume"])
        if closes is None or volumes is None:
            return None
        close = closes[-1]
        current_volume = volumes[-1]
        mean_volume = sum(volumes[-51:-1], Decimal(0)) / Decimal(50)
        if mean_volume <= 0 or current_volume < 0:
            return None

        vcp = getattr(scan, "vcp", None)
        stage = getattr(getattr(scan, "stage", None), "value", None)
        pivot = _decimal(getattr(vcp, "pivot_price", None))
        score = getattr(vcp, "score", None)
        trend_score = _decimal(getattr(vcp, "trend_template_score", None))
        minimum_trend = _decimal(parameters["minimum_trend_score"])
        minimum_volume = _decimal(parameters["minimum_relative_volume"])
        maximum_extension = _decimal(parameters["maximum_pivot_extension_pct"])
        minimum_vcp_score = _plain_int(parameters["minimum_vcp_score"])
        if None in (
            pivot,
            trend_score,
            minimum_trend,
            minimum_volume,
            maximum_extension,
            minimum_vcp_score,
        ):
            return None
        assert pivot is not None
        assert trend_score is not None
        assert minimum_trend is not None
        assert minimum_volume is not None
        assert maximum_extension is not None
        assert minimum_vcp_score is not None
        qualifies = (
            stage == "Stage 2"
            and getattr(vcp, "valid_vcp", False) is True
            and getattr(vcp, "trend_template_passed", False) is True
            and getattr(vcp, "execution_state", None) == "Breakout"
            and getattr(vcp, "breakout_volume_detected", False) is True
            and isinstance(score, int)
            and not isinstance(score, bool)
            and score >= minimum_vcp_score
            and trend_score >= minimum_trend
            and close >= pivot
            and close <= pivot * (Decimal(1) + maximum_extension / Decimal(100))
            and current_volume >= mean_volume * minimum_volume
        )
        if not qualifies:
            return None
        assert isinstance(score, int)
        return score

    def _entry_signal(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> Signal | None:
        if self._entry_qualification(view, parameters, security_id) is None:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id=_ENTRY_RULE,
        )

    def _held_vcp_score(self, view: MarketViewV1, security_id: str) -> int | None:
        """Return a held position's current VCP score for upgrade ranking,
        or ``None`` if no visible scan evidence exists today -- a position
        with no computable score is never treated as the weakest holding."""
        scan = _visible_scan(view, security_id)
        if scan is None:
            return None
        score = getattr(getattr(scan, "vcp", None), "score", None)
        return score if isinstance(score, int) and not isinstance(score, bool) else None

    def _upgrade_exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        already_exiting: frozenset[str],
    ) -> Signal | None:
        """Story: portfolio upgrading (Minervini's "upgrade" discipline).

        When every position slot is committed (cash cannot fund the
        strongest unheld candidate's fixed-share entry) and that candidate's
        VCP score clears the weakest held position's own current score by
        at least ``upgrade_score_margin``, sell the weakest holding to free
        cash for the stronger setup -- exactly mirroring the mechanical
        stop/SMA/pattern-invalidation exits above, never overriding them.
        The freed cash is picked up by the ordinary ``entry_signals`` path
        on a later qualifying session; this method never buys anything
        itself.
        """
        if parameters.get("enable_position_upgrade") is not True:
            return None
        fixed_shares = _plain_int(parameters["fixed_shares"])
        margin = _plain_int(parameters["upgrade_score_margin"])
        if fixed_shares is None or fixed_shares <= 0 or margin is None:
            return None

        held_ids = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0 and position.security_id not in already_exiting
        }
        if not held_ids:
            return None

        candidates: list[tuple[int, str]] = []
        for security_id in _universe(parameters):
            if security_id in held_ids:
                continue
            score = self._entry_qualification(view, parameters, security_id)
            if score is not None:
                candidates.append((score, security_id))
        if not candidates:
            return None
        best_score, best_security_id = max(candidates, key=lambda item: item)

        history = _current_history(view, best_security_id)
        if history is None:
            return None
        closes = _decimals(history["close"])
        if closes is None:
            return None
        estimated_cost = closes[-1] * Decimal(fixed_shares)
        if portfolio.cash >= estimated_cost:
            # Cash already covers the strongest candidate -- the ordinary
            # entry_signals path will take it without sacrificing anything.
            return None

        held_scored = [
            (score, security_id)
            for security_id in held_ids
            if (score := self._held_vcp_score(view, security_id)) is not None
        ]
        if not held_scored:
            return None
        weakest_score, weakest_security_id = min(held_scored, key=lambda item: item)

        if best_score - weakest_score < margin:
            return None
        return Signal(
            security_id=weakest_security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_UPGRADE_EXIT_RULE,
        )

    def _exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> Signal | None:
        held = _position(portfolio, security_id)
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        if held is None or held.quantity <= 0 or history is None or len(history) < 50:
            return None
        closes = _decimals(history["close"].iloc[-50:])
        maximum_loss = _decimal(parameters["maximum_loss_pct"])
        if closes is None or maximum_loss is None:
            return None
        close = closes[-1]
        sma50 = sum(closes, Decimal(0)) / Decimal(50)
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        stage = getattr(getattr(scan, "stage", None), "value", None)
        state = getattr(getattr(scan, "vcp", None), "execution_state", None)
        scan_failure = scan is not None and (
            stage != "Stage 2" or state in {"Invalid", "Damaged"}
        )
        if not (close <= stop or close < sma50 or scan_failure):
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_EXIT_RULE,
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.SELL:
            return _integral_quantity(portfolio, signal.security_id)
        fixed_shares = parameters["fixed_shares"]
        if isinstance(fixed_shares, int) and not isinstance(fixed_shares, bool):
            return fixed_shares
        return 0
