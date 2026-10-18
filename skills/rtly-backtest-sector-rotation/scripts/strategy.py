"""Deterministic long-only sector-rotation strategy."""

from __future__ import annotations

from collections.abc import Mapping
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
    Signal,
    SignalSide,
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-sector-rotation"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
ASSIGNMENTS_PARAMETER = "sector_assignments"
DEFAULT_LOOKBACK = 126


def _int_parameter(
    parameters: StrategyParameters, name: str, default: int
) -> int | None:
    value = parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


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


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the host-bound selected universe as a canonical ID tuple."""
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _assignments(parameters: StrategyParameters) -> dict[str, str]:
    """Return the pinned ``{security_id: sector}`` map; empty fails closed."""
    raw = parameters.get(ASSIGNMENTS_PARAMETER)
    if not isinstance(raw, Mapping):
        return {}
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in raw.items()):
        return {}
    universe = set(_universe(parameters))
    return {k: v for k, v in raw.items() if k in universe}


class _Reading(NamedTuple):
    """One security's lookback return and the session before the latest."""

    momentum: Decimal | None
    previous_session: date | None


def _read(view: MarketViewV1, security_id: str, lookback: int) -> _Reading | None:
    """Return the fresh-history reading, or ``None`` without current closes."""
    try:
        history = view.price_history(
            security_id, limit=lookback + 1, columns=("close",)
        )
        if history is None or len(history.index) == 0:
            return None
        if _as_date(history.index[-1]) != view.as_of_session:
            return None
        closes = [_decimal(value) for value in history["close"]]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    previous = _as_date(history.index[-2]) if len(history.index) > 1 else None
    first, last = (
        (closes[0], closes[-1]) if len(closes) == lookback + 1 else (None,) * 2
    )
    if first is None or last is None or first <= 0 or last <= 0:
        return _Reading(None, previous)
    return _Reading(last / first - Decimal(1), previous)


def _readings(
    view: MarketViewV1, ids: tuple[str, ...], lookback: int
) -> dict[str, _Reading]:
    found = {sid: _read(view, sid, lookback) for sid in ids}
    return {sid: reading for sid, reading in found.items() if reading is not None}


def _is_rebalance(
    view: MarketViewV1, readings: Mapping[str, _Reading], cadence: object
) -> bool:
    """True on the first session of a new month/quarter.

    The prior session is the latest one any fresh security traded, so a
    single security's gap cannot fake a rebalance.
    """
    previous = max(
        (r.previous_session for r in readings.values() if r.previous_session),
        default=None,
    )
    if previous is None:
        return False
    today = view.as_of_session
    if cadence == "monthly":
        return (today.year, today.month) != (previous.year, previous.month)
    if cadence == "quarterly":
        return (today.year, (today.month - 1) // 3) != (
            previous.year,
            (previous.month - 1) // 3,
        )
    return False


class _SectorRank(NamedTuple):
    leaders: tuple[str, ...]
    scores: dict[str, Decimal]


def _rank_sectors(
    assignments: Mapping[str, str],
    readings: Mapping[str, _Reading],
    top_n: int,
    min_members: int,
) -> _SectorRank:
    members: dict[str, list[Decimal]] = {}
    for security_id, sector in assignments.items():
        reading = readings.get(security_id)
        if reading is not None and reading.momentum is not None:
            members.setdefault(sector, []).append(reading.momentum)
    scores = {
        sector: sum(values, Decimal(0)) / Decimal(len(values))
        for sector, values in members.items()
        if len(values) >= min_members
    }
    ordered = sorted(scores, key=lambda sector: (-scores[sector], sector))
    return _SectorRank(tuple(ordered[:top_n]), scores)


def _percent(value: Decimal | None) -> Decimal | None:
    return None if value is None else value * Decimal(100)


def _facts(
    view: MarketViewV1,
    sector: str | None,
    rank: _SectorRank,
    top_n: int,
    cadence: object,
) -> list[ExplanationFactV1]:
    score = None if sector is None else rank.scores.get(sector)
    position = rank.leaders.index(sector) + 1 if sector in rank.leaders else None
    return [
        ExplanationFactV1(label="Sector", observed=sector),
        ExplanationFactV1(
            label="Sector momentum",
            observed=_percent(score),
            unit=EvidenceUnit.PERCENT,
            as_of=view.as_of_session,
        ),
        ExplanationFactV1(
            label="Sector rank",
            observed=None if position is None else Decimal(position),
            unit=EvidenceUnit.COUNT,
        ),
        ExplanationFactV1(
            label="Leading sectors held",
            observed=Decimal(top_n),
            unit=EvidenceUnit.COUNT,
        ),
        ExplanationFactV1(label="Rebalance cadence", observed=str(cadence)),
    ]


def _entry_signal(
    view: MarketViewV1,
    security_id: str,
    sector: str,
    momentum: Decimal,
    rank: _SectorRank,
    top_n: int,
    cadence: object,
) -> Signal:
    facts = _facts(view, sector, rank, top_n, cadence)
    facts.append(
        ExplanationFactV1(
            label="Security momentum",
            observed=_percent(momentum),
            unit=EvidenceUnit.PERCENT,
            as_of=view.as_of_session,
        )
    )
    return Signal(
        security_id=security_id,
        side=SignalSide.BUY,
        session=view.as_of_session,
        rule_id="sector_rotation_leading_sector_v1",
        explanation=SignalExplanationV1(
            reasons=[
                SignalReasonV1(
                    code="leading_sector_entry",
                    summary="Member of a leading sector on a rebalance session.",
                    facts=facts,
                )
            ]
        ),
    )


def _rotation_exit(
    view: MarketViewV1,
    security_id: str,
    sector: str | None,
    rank: _SectorRank,
    top_n: int,
    cadence: object,
) -> Signal:
    return Signal(
        security_id=security_id,
        side=SignalSide.SELL,
        session=view.as_of_session,
        rule_id="sector_rotation_sector_left_leaders_v1",
        explanation=SignalExplanationV1(
            reasons=[
                SignalReasonV1(
                    code="sector_left_leaders",
                    summary="Sector left the leading sectors at rebalance.",
                    facts=_facts(view, sector, rank, top_n, cadence),
                )
            ]
        ),
    )


def _loss_stop(
    average_cost: Decimal, parameters: StrategyParameters
) -> tuple[Decimal, Decimal] | None:
    loss = _decimal(parameters.get("maximum_loss_pct"))
    if loss is None or not 0 < loss < 100:
        return None
    return average_cost * (Decimal(1) - loss / Decimal(100)), loss


def _current_close(view: MarketViewV1, security_id: str) -> Decimal | None:
    try:
        history = view.price_history(security_id, limit=1, columns=("close",))
        if history is None or len(history.index) == 0:
            return None
        if _as_date(history.index[-1]) != view.as_of_session:
            return None
        return _decimal(history["close"].iloc[-1])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


def _stop_exit(
    view: MarketViewV1, security_id: str, close: Decimal, stop: Decimal, loss: Decimal
) -> Signal:
    return Signal(
        security_id=security_id,
        side=SignalSide.SELL,
        session=view.as_of_session,
        rule_id="sector_rotation_maximum_loss_stop_v1",
        explanation=SignalExplanationV1(
            reasons=[
                SignalReasonV1(
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
                            label="Maximum loss",
                            observed=loss,
                            unit=EvidenceUnit.PERCENT,
                        ),
                    ],
                )
            ]
        ),
    )


class SectorRotationStrategy:
    """Hold members of the leading sectors, rotating at each rebalance."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare ``momentum_lookback_sessions + 1`` closes."""
        lookback = _int_parameter(
            parameters, "momentum_lookback_sessions", DEFAULT_LOOKBACK
        )
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=(lookback or DEFAULT_LOOKBACK) + 1,
            columns=("close",),
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))

    def _context(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> tuple[dict[str, str], dict[str, _Reading], _SectorRank, int, object] | None:
        """Return the rebalance-session ranking, or ``None`` off-rebalance."""
        lookback = _int_parameter(
            parameters, "momentum_lookback_sessions", DEFAULT_LOOKBACK
        )
        top_n = _int_parameter(parameters, "top_n_sectors", 3)
        min_members = _int_parameter(parameters, "min_sector_members", 3)
        assignments = _assignments(parameters)
        if None in (lookback, top_n, min_members) or not assignments:
            return None
        assert lookback is not None and top_n is not None and min_members is not None
        readings = _readings(view, _universe(parameters), lookback)
        cadence = parameters.get("rebalance_cadence", "monthly")
        if not _is_rebalance(view, readings, cadence):
            return None
        rank = _rank_sectors(assignments, readings, top_n, min_members)
        return assignments, readings, rank, top_n, cadence

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        if not entry_signals_permitted(view, parameters, _universe(parameters)):
            return []
        context = self._context(view, parameters)
        if context is None:
            return []
        assignments, readings, rank, top_n, cadence = context
        candidates = sorted(
            (
                (readings[sid].momentum, sid)
                for sid, sector in assignments.items()
                if sector in rank.leaders
                and sid in readings
                and readings[sid].momentum is not None
            ),
            key=lambda item: (-(item[0] or Decimal(0)), item[1]),
        )
        count = len(candidates)
        signals = []
        for position, (momentum, sid) in enumerate(candidates):
            assert momentum is not None
            signal = _entry_signal(
                view, sid, assignments[sid], momentum, rank, top_n, cadence
            )
            signals.append(
                signal.model_copy(update={"priority": Decimal(count - position)})
            )
        return signals

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        universe = set(_universe(parameters))
        held = sorted(
            (
                p
                for p in portfolio.positions
                if p.quantity > 0 and p.security_id in universe
            ),
            key=lambda position: position.security_id,
        )
        if not held:
            return []
        context = self._context(view, parameters)
        signals: dict[str, Signal] = {}
        for position in held:
            sid = position.security_id
            stop = _loss_stop(position.average_cost, parameters)
            close = _current_close(view, sid)
            if stop is not None and close is not None and close <= stop[0]:
                signals[sid] = _stop_exit(view, sid, close, *stop)
            elif context is not None:
                assignments, _readings_, rank, top_n, cadence = context
                sector = assignments.get(sid)
                if sector not in rank.leaders:
                    signals[sid] = _rotation_exit(
                        view, sid, sector, rank, top_n, cadence
                    )
        return [signals[sid] for sid in sorted(signals)]

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the maximum-loss price from average cost."""
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
        stop = _loss_stop(held.average_cost, parameters)
        if stop is None:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy setting maximum_loss_pct is missing or unusable.",
            )
        level, loss = stop
        return StopLevelV1(
            level=level,
            rule_code="maximum_loss_stop",
            summary=f"Max loss {loss.normalize():f}% from average cost",
            facts=[
                ExplanationFactV1(
                    label="Maximum loss", observed=loss, unit=EvidenceUnit.PERCENT
                )
            ],
            basis="mixed",
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
