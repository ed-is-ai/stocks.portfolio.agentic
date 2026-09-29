"""Suggest a holding's stop-loss from its portfolio's assigned Strategy.

Pure and deterministic: mirrors each Strategy's own exit rule from data the
Portfolio tab already holds (position, published analysis record and its daily
``ohlcv_history``, stored assignment parameters with descriptor-default
fallback). A suggestion is never evidence -- risk checks keep reading only
recorded stops -- and a level is never invented: every case without one
carries a declared reason instead.

Per Strategy (the level is the price tomorrow's exit would fire through):

- Minervini: max(maximum-loss stop, 50-day average).
- Weinstein: max(maximum-loss stop, 150-day average).
- Darvas Box: the box bottom, the lowest low of the latest
  ``box_lookback_sessions`` sessions (exit: close below it).
- Turtle Trend: the exit channel, the lowest low of the latest
  ``exit_lookback_sessions`` sessions (exit: low below it).
- Moving Average: the close at which the fast average would cross below the
  slow one (exit: close below it).
- Buy and Hold never sells: a default risk stop of ``DEFAULT_RISK_STOP_PCT``.

The maximum-loss stop is a *position-level* stop: it is measured from the
holding's average cost (not any single lot's price), and "Use" records it on
the latest BUY the position replay reads, which is where the replay takes a
holding's stop from.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from functools import partial
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.record import StockRecord
from app.schemas.trade import Position

MINERVINI = "rtly-backtest-minervini"
WEINSTEIN = "rtly-backtest-weinstein"
DARVAS_BOX = "rtly-backtest-darvas-box"
TURTLE_TREND = "rtly-backtest-turtle-trend"
MOVING_AVERAGE = "rtly-backtest-moving-average"
BUY_AND_HOLD = "rtly-backtest-buy-and-hold"

MAX_LOSS_PARAM = "maximum_loss_pct"
MAX_LOSS_NOTE = "Position-level stop from average cost"
#: Buy and Hold has no exit: its suggestion is this maximum-loss stop.
DEFAULT_RISK_STOP_PCT = 10
BUY_AND_HOLD_NOTE = (
    "Buy and Hold never sells — this is a default risk stop, not part of its rules."
)
NO_RECORD_NOTE = "Needs price history — this holding isn't in the latest scan"
#: A record older than this versus the tab's newest record is stale -- its
#: averages and price history are not used.
SMA50_MAX_AGE_DAYS = 10
_PENCE_UNITS = frozenset({"GBp", "GBX"})


class StopSuggestion(BaseModel):
    """A suggested stop level with its binding rule, or the reason for none."""

    model_config = ConfigDict(frozen=True)

    level: float | None = None
    rule: str | None = None
    note: str | None = None
    distance_pct: float | None = None
    at_or_below: bool = False


def stop_refusal(position: Position | None) -> str | None:
    """Return why a stop cannot be recorded on ``position``, or None.

    Only a currently held (positive shares) holding with no recorded stop
    may take one: an existing stop is edited with Adjust, never overwritten.
    Shared by the set-stop route and its write transaction.
    """
    if position is None or position.shares <= 0:
        return "Not currently held."
    if position.stop_loss is not None:
        return (
            f"A stop is already recorded for {position.display_symbol}; "
            "edit it with Adjust."
        )
    return None


def _positive(value: Any) -> float | None:
    """Return ``value`` as a float when it is a positive finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def valid_max_loss_pct(value: Any) -> float | None:
    """Return ``value`` when it is a usable maximum-loss % (0 <= pct < 100).

    0 is valid: a stop at average cost.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and 0 <= value < 100 else None


def _window(value: Any) -> int | None:
    """Return ``value`` when it is a usable session count (a plain int >= 1)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    MAX_LOSS_PARAM: valid_max_loss_pct,
    "box_lookback_sessions": _window,
    "exit_lookback_sessions": _window,
    "fast_window": _window,
    "slow_window": _window,
}

#: The stored parameters each Strategy's stop rule reads.
_RULE_PARAMS: dict[str, tuple[str, ...]] = {
    MINERVINI: (MAX_LOSS_PARAM,),
    WEINSTEIN: (MAX_LOSS_PARAM,),
    DARVAS_BOX: ("box_lookback_sessions",),
    TURTLE_TREND: ("exit_lookback_sessions",),
    MOVING_AVERAGE: ("fast_window", "slow_window"),
    BUY_AND_HOLD: (),
}


def needs_descriptor_defaults(strategy_id: str, parameters: Mapping[str, Any]) -> bool:
    """Whether a stored parameter ``strategy_id``'s stop rule reads is unusable.

    Only then are the Strategy descriptor's defaults worth discovering.
    """
    return any(
        _VALIDATORS[name](parameters.get(name)) is None
        for name in _RULE_PARAMS.get(strategy_id, ())
    )


def _param(
    name: str, parameters: Mapping[str, Any], defaults: Mapping[str, Any] | None
) -> Any:
    """Return the stored ``name`` when usable, else the descriptor default."""
    valid = _VALIDATORS[name]
    value = valid(parameters.get(name))
    if value is None and defaults is not None:
        value = valid(defaults.get(name))
    return value


def _is_stale(record_as_of: str, as_of: date | None) -> bool:
    """Whether ``record_as_of`` is too old versus the tab's ``as_of``."""
    if as_of is None:
        return False
    try:
        recorded = date.fromisoformat(record_as_of[:10])
    except ValueError:
        return True
    return (as_of - recorded).days > SMA50_MAX_AGE_DAYS


def _record_refusal(
    position: Position, record: StockRecord, as_of: date | None, subject: str
) -> str | None:
    """Why ``record``'s prices can't set ``position``'s stop, or None."""
    # Deferred: portfolio_service imports this module.
    from app.services.portfolio_service import PortfolioService

    units = {
        PortfolioService._quote_currency(record.currency),
        PortfolioService._quote_currency(position.price_currency),
    }
    if len(units) > 1:
        if units & _PENCE_UNITS:
            return f"{subject} in a different unit"
        return f"{subject} is in another currency"
    if _is_stale(record.as_of, as_of):
        return f"{subject} is stale"
    return None


def _record_average(
    position: Position, record: StockRecord | None, as_of: date | None, window: int
) -> tuple[float | None, str | None]:
    """Return the record's usable ``window``-day average, or None with a note."""
    subject = f"{window}-day average"
    average = None if record is None else _positive(getattr(record, f"sma{window}"))
    if record is None or average is None:
        return None, f"{subject} unavailable"
    refusal = _record_refusal(position, record, as_of, subject)
    return (None, refusal) if refusal else (average, None)


def _recent_values(
    position: Position,
    record: StockRecord | None,
    as_of: date | None,
    sessions: int,
    field: str,
) -> tuple[list[float] | None, str | None]:
    """Return ``field`` of the latest ``sessions`` bars, newest first, or a note.

    ``ohlcv_history`` is stored most recent first (the Scanner reverses the
    daily frame), so the latest sessions are its head.
    """
    if record is None:
        return None, NO_RECORD_NOTE
    refusal = _record_refusal(position, record, as_of, "Price history")
    if refusal:
        return None, refusal
    bars = record.ohlcv_history[:sessions]
    if len(bars) < sessions:
        return None, f"Needs {sessions} sessions of price history"
    values = [_positive(bar.get(field)) for bar in bars]
    if any(value is None for value in values):
        return None, "Price history has unusable values"
    return [value for value in values if value is not None], None


def _suggestion(
    position: Position, level: float, rule: str, *notes: str | None
) -> StopSuggestion:
    """Build a suggestion for ``level`` with its distance from the price."""
    price = _positive(position.current_price)
    return StopSuggestion(
        level=level,
        rule=rule,
        note=" · ".join(n for n in notes if n) or None,
        distance_pct=None if price is None else (level / price - 1) * 100,
        at_or_below=price is not None and price <= level,
    )


def _average_cost(position: Position) -> tuple[float | None, str | None]:
    """Return the position's usable average cost, or None with a note."""
    if position.cost_currency != position.price_currency:
        return None, "Cost and price are in different units"
    avg_cost = _positive(position.avg_cost)
    if avg_cost is None:
        return None, "No average cost to base a stop on"
    return avg_cost, None


def _max_loss_or_average(
    window: int,
    position: Position,
    record: StockRecord | None,
    parameters: Mapping[str, Any],
    defaults: Mapping[str, Any] | None,
    as_of: date | None,
) -> StopSuggestion:
    """max(maximum-loss stop from average cost, the ``window``-day average).

    Minervini exits on ``close < sma50``, Weinstein on ``close < sma150``;
    both also exit at the maximum-loss stop.
    """
    avg_cost, note = _average_cost(position)
    if avg_cost is None:
        return StopSuggestion(note=note)
    pct = _param(MAX_LOSS_PARAM, parameters, defaults)
    if pct is None:
        return StopSuggestion(note="Maximum-loss setting unavailable")
    level = avg_cost * (1 - pct / 100)
    average, average_note = _record_average(position, record, as_of, window)
    if average is not None and average > level:
        return _suggestion(position, average, f"{window}-day avg")
    return _suggestion(
        position, level, f"max loss {pct:g}%", average_note, MAX_LOSS_NOTE
    )


def _lowest_low(
    param: str,
    label: str,
    position: Position,
    record: StockRecord | None,
    parameters: Mapping[str, Any],
    defaults: Mapping[str, Any] | None,
    as_of: date | None,
) -> StopSuggestion:
    """The lowest low of the latest ``param`` sessions, today included.

    Tomorrow's exit compares against ``min(low)`` of the prior window
    (``history.iloc[-lookback - 1 : -1]``), which ends today.
    """
    lookback = _param(param, parameters, defaults)
    if lookback is None:
        return StopSuggestion(note="Lookback setting unavailable")
    lows, note = _recent_values(position, record, as_of, lookback, "low")
    if lows is None:
        return StopSuggestion(note=note)
    return _suggestion(position, min(lows), label.format(n=lookback))


def _crossover_price(closes: Sequence[Decimal], fast: int, slow: int) -> Decimal:
    """Tomorrow's close below which the fast average falls below the slow one.

    ``closes`` are newest first. With ``Sf``/``Ss`` the sums of the latest
    ``fast - 1``/``slow - 1`` closes, ``(Sf + x) / fast < (Ss + x) / slow``
    exactly when ``x < (fast * Ss - slow * Sf) / (slow - fast)``.
    """
    fast_sum = sum(closes[: fast - 1], Decimal(0))
    slow_sum = sum(closes[: slow - 1], Decimal(0))
    return (fast * slow_sum - slow * fast_sum) / (slow - fast)


def _moving_average(
    position: Position,
    record: StockRecord | None,
    parameters: Mapping[str, Any],
    defaults: Mapping[str, Any] | None,
    as_of: date | None,
) -> StopSuggestion:
    """Moving Average: exits when the fast SMA crosses below the slow SMA."""
    fast = _param("fast_window", parameters, defaults)
    slow = _param("slow_window", parameters, defaults)
    if fast is None or slow is None or slow < 2 or fast >= slow:
        return StopSuggestion(note="Moving-average windows unavailable")
    values, note = _recent_values(position, record, as_of, slow, "close")
    if values is None:
        return StopSuggestion(note=note)
    closes = [Decimal(str(value)) for value in values]
    fast_today = sum(closes[:fast], Decimal(0)) / Decimal(fast)
    if fast_today < sum(closes, Decimal(0)) / Decimal(slow):
        return StopSuggestion(
            note="Fast average already below slow — exit condition met"
        )
    bound = _crossover_price(closes, fast, slow)
    if bound <= 0:
        return StopSuggestion(note="No crossover price within reach")
    return _suggestion(position, float(bound), f"{fast}/{slow} crossover price")


def _buy_and_hold(
    position: Position,
    record: StockRecord | None,
    parameters: Mapping[str, Any],
    defaults: Mapping[str, Any] | None,
    as_of: date | None,
) -> StopSuggestion:
    """Buy and Hold never sells: a default maximum-loss risk stop."""
    avg_cost, note = _average_cost(position)
    if avg_cost is None:
        return StopSuggestion(note=note)
    level = avg_cost * (1 - DEFAULT_RISK_STOP_PCT / 100)
    rule = f"default risk stop {DEFAULT_RISK_STOP_PCT}%"
    return _suggestion(position, level, rule, BUY_AND_HOLD_NOTE)


#: Each Strategy's stop rule, mirroring its own exit.
STOP_RULES: dict[str, Callable[..., StopSuggestion]] = {
    MINERVINI: partial(_max_loss_or_average, 50),
    WEINSTEIN: partial(_max_loss_or_average, 150),
    # Darvas exits on a close, Turtle on a low, strictly below the level.
    DARVAS_BOX: partial(
        _lowest_low, "box_lookback_sessions", "box bottom ({n}-day low)"
    ),
    TURTLE_TREND: partial(_lowest_low, "exit_lookback_sessions", "{n}-day low"),
    MOVING_AVERAGE: _moving_average,
    BUY_AND_HOLD: _buy_and_hold,
}


def suggest_stop(
    position: Position,
    record: StockRecord | None,
    strategy_id: str | None,
    parameters: Mapping[str, Any],
    defaults: Mapping[str, Any] | None,
    display_name: str | None = None,
    as_of: date | None = None,
) -> StopSuggestion:
    """Derive ``position``'s suggested stop from its assigned Strategy.

    ``parameters`` is the assignment's stored snapshot; ``defaults`` the
    Strategy descriptor's default parameters, consulted only for a stored
    setting that is missing or unusable (None when discovery failed).
    ``record`` is the holding's latest published analysis record; ``as_of``
    the tab's newest analysis date: a record more than
    ``SMA50_MAX_AGE_DAYS`` older loses its averages and price history.
    """
    if strategy_id is None:
        return StopSuggestion(note="No Strategy assigned")
    rule = STOP_RULES.get(strategy_id)
    if rule is None:
        name = display_name or strategy_id
        return StopSuggestion(note=f"No price stop rule for {name}")
    return rule(position, record, parameters, defaults, as_of)
