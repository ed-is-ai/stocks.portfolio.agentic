"""Deterministic long-only Weinstein Stage 2 breakout backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.backtest.regime_filter import entry_signals_permitted
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-weinstein"
STRATEGY_API_VERSION = 1
_ENTRY_RULE = "weinstein_stage2_breakout_v1"
_EXIT_RULE = "weinstein_stage_exit_v1"
_UPGRADE_EXIT_RULE = "weinstein_upgrade_exit_v1"


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
    if not {"high", "close", "volume"}.issubset(history.columns):
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


def _sma_slope(prices: list[Decimal], window: int, lookback: int = 4) -> Decimal | None:
    if len(prices) < window + lookback:
        return None
    current = sum(prices[-window:], Decimal(0)) / Decimal(window)
    past = sum(prices[-(window + lookback) : -lookback], Decimal(0)) / Decimal(window)
    return current - past


def _weekly_closes(history: Any) -> list[Decimal] | None:
    grouped: dict[tuple[int, int], Decimal] = {}
    try:
        sessions = history.index
        closes = history["close"]
    except (AttributeError, KeyError, TypeError):
        return None
    for session, raw_close in zip(sessions, closes, strict=True):
        session_date = _session_date(session)
        close = _decimal(raw_close)
        if session_date is None or close is None:
            return None
        iso = session_date.isocalendar()
        grouped[(iso.year, iso.week)] = close
    return [grouped[key] for key in sorted(grouped)][-52:]


def _classify_stage(
    *,
    price: Decimal,
    sma150: Decimal,
    sma200: Decimal,
    weekly: list[Decimal],
) -> str | None:
    slope150 = _sma_slope(weekly, window=30)
    slope200 = _sma_slope(weekly, window=40)
    if slope150 is None or slope200 is None:
        return None
    above_150 = price > sma150
    above_200 = price > sma200
    ma_bullish = sma150 > sma200
    if above_150 and ma_bullish and slope200 > 0:
        return "Stage 2"
    if not above_150 and not above_200 and slope150 < 0:
        return "Stage 4"
    if (not above_150 and above_200) or (above_150 and ma_bullish and slope200 <= 0):
        return "Stage 3"
    return "Stage 1"


class WeinsteinStrategy:
    """Apply Stage 2 breakout and Stage/risk exit rules without state."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare the trend window *and* the Weinstein stage evidence.

        The stage classification is not derivable from OHLCV alone: the
        entry rule and the stage-failure exit both read the committed
        monthly scan's ``stage``. Declaring it means a view that cannot
        evidence a stage is reported incompatible rather than silently
        read as "not Stage 2" (#471).
        """
        lookback = _plain_int(parameters.get("breakout_lookback_sessions")) or 50
        stage = EvidenceRequirementV1(kind=EvidenceKind.SCAN_STAGE)
        return StrategyEvidenceRequirementsV1(
            entry=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=max(204, lookback + 1, 51),
                    columns=("high", "close", "volume"),
                ),
                stage,
            ),
            exit=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=150,
                    columns=("close",),
                ),
                stage,
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
    ) -> Decimal | None:
        """Return this security's trend-strength score -- percent close is
        above its 150-session SMA -- if it qualifies for entry today, else
        ``None``. Factored out of :meth:`_entry_signal` so the upgrade-exit
        ranking (below) can score a would-be candidate using the exact same
        qualification rules, without duplicating them."""
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        lookback = _plain_int(parameters["breakout_lookback_sessions"])
        if lookback is None:
            return None
        required = max(204, lookback + 1, 51)
        if history is None or scan is None or len(history) < required:
            return None

        closes = _decimals(history["close"])
        highs = _decimals(history["high"])
        volumes = _decimals(history["volume"])
        weekly = _weekly_closes(history)
        if closes is None or highs is None or volumes is None or weekly is None:
            return None
        close = closes[-1]
        sma150 = sum(closes[-150:], Decimal(0)) / Decimal(150)
        sma200 = sum(closes[-200:], Decimal(0)) / Decimal(200)
        daily_stage = _classify_stage(
            price=close,
            sma150=sma150,
            sma200=sma200,
            weekly=weekly,
        )
        prior_high = max(highs[-(lookback + 1) : -1])
        prior_volume_mean = sum(volumes[-51:-1], Decimal(0)) / Decimal(50)
        minimum_volume = _decimal(parameters["minimum_relative_volume"])
        scan_stage = getattr(getattr(scan, "stage", None), "value", None)
        if (
            minimum_volume is None
            or prior_volume_mean <= 0
            or volumes[-1] < 0
            or scan_stage != "Stage 2"
            or daily_stage != "Stage 2"
            or close <= prior_high
            or volumes[-1] < prior_volume_mean * minimum_volume
            or sma150 <= 0
        ):
            return None
        return (close - sma150) / sma150 * Decimal(100)

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

    def _held_trend_strength(
        self, view: MarketViewV1, security_id: str
    ) -> Decimal | None:
        """Return a held position's current percent-above-150-session-SMA
        for upgrade ranking, or ``None`` if there isn't enough bounded
        history today -- a position with no computable score is never
        treated as the weakest holding."""
        history = _current_history(view, security_id)
        if history is None or len(history) < 150:
            return None
        closes = _decimals(history["close"].iloc[-150:])
        if closes is None:
            return None
        close = closes[-1]
        sma150 = sum(closes, Decimal(0)) / Decimal(150)
        if sma150 <= 0:
            return None
        return (close - sma150) / sma150 * Decimal(100)

    def _upgrade_exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        already_exiting: frozenset[str],
    ) -> Signal | None:
        """Portfolio upgrading: rotate capital toward the strongest Stage 2
        leadership when a slot isn't otherwise free.

        When a stronger unheld candidate's percent-above-150-session-SMA
        clears the weakest held position's own current reading by at least
        ``upgrade_score_margin_pct`` points, sell the weakest holding to
        free cash for the stronger setup -- mirroring Weinstein's own
        practice of rotating out of laggards into leadership during a Stage
        2 advance. This never overrides the mechanical stop/SMA/stage
        exits above and never buys anything itself -- the freed cash is
        picked up by the ordinary entry path on a later qualifying
        session.
        """
        if parameters.get("enable_position_upgrade") is not True:
            return None
        margin = _decimal(parameters["upgrade_score_margin_pct"])
        if margin is None:
            return None
        # The shared allocator owns BUY affordability.  Do not liquidate a
        # holding while a cash slot remains for its next cohort.
        if portfolio.cash > 0:
            return None

        held_ids = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0 and position.security_id not in already_exiting
        }
        if not held_ids:
            return None

        candidates: list[tuple[Decimal, str]] = []
        for security_id in _universe(parameters):
            if security_id in held_ids:
                continue
            score = self._entry_qualification(view, parameters, security_id)
            if score is not None:
                candidates.append((score, security_id))
        if not candidates:
            return None
        best_score, best_security_id = max(candidates, key=lambda item: item)

        held_scored = [
            (score, security_id)
            for security_id in held_ids
            if (score := self._held_trend_strength(view, security_id)) is not None
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
        if held is None or held.quantity <= 0 or history is None or len(history) < 150:
            return None
        closes = _decimals(history["close"].iloc[-150:])
        maximum_loss = _decimal(parameters["maximum_loss_pct"])
        if closes is None or maximum_loss is None:
            return None
        close = closes[-1]
        sma150 = sum(closes, Decimal(0)) / Decimal(150)
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        scan_stage = getattr(getattr(scan, "stage", None), "value", None)
        # Only an *evidenced* stage can fail: a view carrying no stage
        # evidence at all must never be read as "not Stage 2", which
        # would manufacture a Sell out of missing evidence (#471).
        scan_failure = scan_stage is not None and scan_stage != "Stage 2"
        if not (close <= stop or close < sma150 or scan_failure):
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
        # The engine reserves equal capital and determines whole shares.
        return 0
