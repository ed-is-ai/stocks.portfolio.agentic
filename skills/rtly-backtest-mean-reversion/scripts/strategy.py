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
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-mean-reversion"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
_ENTRY_RULE = "mean_reversion_oversold_v1"
_EXIT_RULE = "mean_reversion_reverted_v1"
_TIME_STOP_RULE = "mean_reversion_time_stop_v1"
_RISK_STOP_RULE = "mean_reversion_risk_stop_v1"


class _Settings(NamedTuple):
    lookback: int
    entry_z: Decimal
    exit_z: Decimal
    max_holding: int
    trend_lookback: int
    maximum_loss: Decimal

    @property
    def sessions(self) -> int:
        """Closes needed: z-score window, rising trend SMA, holding period."""
        return max(self.lookback, self.trend_lookback + 1, self.max_holding + 1)


class _Reading(NamedTuple):
    close: Decimal
    mean: Decimal
    zscore: Decimal
    sessions: int


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the host-bound selected universe as a sorted, de-duplicated tuple."""
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
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
    """Validate parameters; ``None`` fails the strategy closed.

    ``maximum_loss_pct`` has no default, like the sibling loss stops. An
    ``entry_zscore`` above ``sqrt(lookback - 1)`` (the largest population
    z-score ``lookback`` closes can produce) could never fire, and an
    ``exit_zscore`` at or below ``-entry_zscore`` would sell on the entry bar.
    """
    lookback = _int_param(parameters, "lookback_sessions", 20)
    max_holding = _int_param(parameters, "max_holding_sessions", 10)
    trend = _int_param(parameters, "trend_lookback_sessions", 200)
    entry_z = _decimal(parameters.get("entry_zscore", 2.0))
    exit_z = _decimal(parameters.get("exit_zscore", 0.0))
    maximum_loss = _decimal(parameters.get("maximum_loss_pct"))
    if (
        lookback is None
        or lookback < 2
        or max_holding is None
        or max_holding < 1
        or trend is None
        or trend < 2
        or entry_z is None
        or entry_z <= 0
        or entry_z > Decimal(lookback - 1).sqrt()
        or exit_z is None
        or exit_z <= -entry_z
        or maximum_loss is None
        or not 0 <= maximum_loss < 100
    ):
        return None
    return _Settings(lookback, entry_z, exit_z, max_holding, trend, maximum_loss)


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


def _reading(closes: list[Decimal], settings: _Settings) -> _Reading | None:
    """Return today's z-score, or ``None`` when short or zero-variance."""
    if len(closes) < settings.lookback:
        return None
    window = closes[-settings.lookback :]
    count = Decimal(len(window))
    mean = sum(window, Decimal(0)) / count
    variance = sum(((c - mean) ** 2 for c in window), Decimal(0)) / count
    if variance <= 0:
        return None
    zscore = (window[-1] - mean) / variance.sqrt()
    return _Reading(window[-1], mean, zscore, len(window))


def _trend_sma(closes: list[Decimal], settings: _Settings) -> Decimal | None:
    """Return today's trend SMA when the close is above it and it is rising.

    The SMA rose from yesterday exactly when today's close exceeds the close
    ``trend_lookback`` sessions ago, so ``trend_lookback + 1`` closes suffice.
    """
    trend = settings.trend_lookback
    if len(closes) < trend + 1:
        return None
    sma = sum(closes[-trend:], Decimal(0)) / Decimal(trend)
    close = closes[-1]
    return sma if close > sma and close > closes[-trend - 1] else None


def _sessions_held(sessions: list[date], position: PositionSummaryV1) -> int | None:
    """Sessions held, entry session counted first; ``None`` if unknown."""
    if position.entry_session is None:
        return None
    entry = position.entry_session
    return sum(1 for s in sessions if s >= entry)


def _risk_stop(position: PositionSummaryV1, settings: _Settings) -> Decimal:
    return position.average_cost * (Decimal(1) - settings.maximum_loss / Decimal(100))


def _zscore_facts(
    reading: _Reading, operator: ComparisonOperator, threshold: Decimal, session: date
) -> list[ExplanationFactV1]:
    return [
        ExplanationFactV1(
            label="Close z-score",
            observed=reading.zscore,
            operator=operator,
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
            label="Rolling mean", observed=reading.mean, unit=EvidenceUnit.PRICE
        ),
        ExplanationFactV1(
            label="Lookback",
            observed=Decimal(reading.sessions),
            unit=EvidenceUnit.SESSIONS,
        ),
    ]


def _signal(
    view: MarketViewV1,
    security_id: str,
    side: SignalSide,
    rule_id: str,
    code: str,
    summary: str,
    facts: list[ExplanationFactV1],
) -> Signal:
    return Signal(
        security_id=security_id,
        side=side,
        session=view.as_of_session,
        rule_id=rule_id,
        explanation=SignalExplanationV1(
            reasons=[SignalReasonV1(code=code, summary=summary, facts=facts)]
        ),
    )


def _exit_signal(
    view: MarketViewV1, position: PositionSummaryV1, settings: _Settings
) -> Signal | None:
    """First firing exit: risk stop, reversion, then time stop."""
    data = _history_sessions(view, position.security_id, settings.sessions)
    if data is None:
        return None
    sessions, closes = data
    session = view.as_of_session
    close = closes[-1]
    stop = _risk_stop(position, settings)
    sid = position.security_id
    if close <= stop:
        facts = [
            ExplanationFactV1(
                label="Close",
                observed=close,
                operator=ComparisonOperator.LTE,
                threshold=stop,
                unit=EvidenceUnit.PRICE,
                as_of=session,
            ),
            ExplanationFactV1(
                label="Maximum loss",
                observed=settings.maximum_loss,
                unit=EvidenceUnit.PERCENT,
            ),
        ]
        summary = "Close hit the maximum-loss stop for this position."
        return _signal(
            view,
            sid,
            SignalSide.SELL,
            _RISK_STOP_RULE,
            "maximum_loss_stop",
            summary,
            facts,
        )
    reading = _reading(closes, settings)
    if reading is not None and reading.zscore >= settings.exit_z:
        facts = _zscore_facts(reading, ComparisonOperator.GTE, settings.exit_z, session)
        return _signal(
            view,
            sid,
            SignalSide.SELL,
            _EXIT_RULE,
            "reverted_to_mean",
            "The close recovered.",
            facts,
        )
    held = _sessions_held(sessions, position)
    if held is None or held < settings.max_holding:
        return None
    facts = [
        ExplanationFactV1(
            label="Sessions held",
            observed=Decimal(held),
            operator=ComparisonOperator.GTE,
            threshold=Decimal(settings.max_holding),
            unit=EvidenceUnit.SESSIONS,
            as_of=session,
        ),
        ExplanationFactV1(
            label="Close", observed=close, unit=EvidenceUnit.PRICE, as_of=session
        ),
    ]
    return _signal(
        view,
        sid,
        SignalSide.SELL,
        _TIME_STOP_RULE,
        "time_stop",
        "Holding period reached.",
        facts,
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
    """Buy oversold closes in an uptrend; sell on reversion or a stop."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare closes for the z-score, trend SMA and holding period."""
        settings = _settings(parameters)
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=settings.sessions if settings else 201,
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
            data = _history_sessions(view, security_id, settings.sessions)
            if data is None:
                continue
            reading = _reading(data[1], settings)
            if reading is None or reading.zscore > -settings.entry_z:
                continue
            sma = _trend_sma(data[1], settings)
            if sma is None:
                continue
            facts = _zscore_facts(
                reading, ComparisonOperator.LTE, -settings.entry_z, view.as_of_session
            )
            facts.append(
                ExplanationFactV1(
                    label="Rising trend SMA",
                    observed=reading.close,
                    operator=ComparisonOperator.GT,
                    threshold=sma,
                    unit=EvidenceUnit.PRICE,
                    as_of=view.as_of_session,
                )
            )
            signal = _signal(
                view,
                security_id,
                SignalSide.BUY,
                _ENTRY_RULE,
                "oversold_vs_mean",
                "The close fell far below its rolling mean inside an uptrend.",
                facts,
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
            signal = _exit_signal(view, position, settings)
            if signal is not None:
                signals.append(signal)
        return signals

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the maximum-loss stop below average cost.

        The reversion and time exits are not price stops, so the only level
        is the cost stop :meth:`exit_signals` fires on (``close <= level``).
        """
        held = next(
            (
                p
                for p in portfolio.positions
                if p.security_id == security_id and p.quantity > 0
            ),
            None,
        )
        if held is None:
            return None
        settings = _settings(parameters)
        if settings is None:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy settings are missing or unusable.",
            )
        pct = settings.maximum_loss
        return StopLevelV1(
            level=_risk_stop(held, settings),
            rule_code="maximum_loss_stop",
            summary=f"Max loss {pct.normalize():f}% from average cost",
            facts=[
                ExplanationFactV1(
                    label="Maximum loss", observed=pct, unit=EvidenceUnit.PERCENT
                )
            ],
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
        if signal.side == SignalSide.BUY:
            # The engine reserves equal capital and determines whole shares.
            return 0
        for position in portfolio.positions:
            if position.security_id == signal.security_id:
                return position.quantity
        return 0
