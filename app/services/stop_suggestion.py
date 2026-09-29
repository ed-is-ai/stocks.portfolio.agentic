"""Suggest a holding's stop-loss from its portfolio's assigned Strategy.

Pure and deterministic: mirrors each Strategy's price exit rule from data the
Portfolio tab already holds (position, published analysis record, stored
assignment parameters). A suggestion is never evidence -- risk checks keep
reading only recorded stops -- and a level is never invented: every case
without one carries a declared reason instead.

The maximum-loss stop is a *position-level* stop: it is measured from the
holding's average cost (not any single lot's price), and "Use" records it on
the latest BUY the position replay reads, which is where the replay takes a
holding's stop from.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.record import StockRecord
from app.schemas.trade import Position

MINERVINI = "rtly-backtest-minervini"
WEINSTEIN = "rtly-backtest-weinstein"

#: Strategies with a price stop rule, and whether it also honours the 50-day
#: average (Minervini's ``close < sma50`` exit). Weinstein's other exits
#: (``close < sma150``, stage) are not price stops for the entry, so only its
#: maximum-loss stop is suggested.
STOP_RULES: dict[str, bool] = {MINERVINI: True, WEINSTEIN: False}

MAX_LOSS_PARAM = "maximum_loss_pct"
MAX_LOSS_NOTE = "Position-level stop from average cost"
#: A record's 50-day average older than this versus the tab's newest record
#: is stale -- the suggestion falls back to the maximum-loss stop.
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


def _max_loss_pct(
    parameters: Mapping[str, Any], defaults: Mapping[str, Any] | None
) -> float | None:
    """Return the stored maximum-loss %, else the descriptor default."""
    pct = valid_max_loss_pct(parameters.get(MAX_LOSS_PARAM))
    if pct is None and defaults is not None:
        pct = valid_max_loss_pct(defaults.get(MAX_LOSS_PARAM))
    return pct


def _is_stale(record_as_of: str, as_of: date | None) -> bool:
    """Whether ``record_as_of`` is too old versus the tab's ``as_of``."""
    if as_of is None:
        return False
    try:
        recorded = date.fromisoformat(record_as_of[:10])
    except ValueError:
        return True
    return (as_of - recorded).days > SMA50_MAX_AGE_DAYS


def _sma50(
    position: Position, record: StockRecord | None, as_of: date | None
) -> tuple[float | None, str | None]:
    """Return the record's usable 50-day average, or None with a note."""
    # Deferred: portfolio_service imports this module.
    from app.services.portfolio_service import PortfolioService

    sma50 = None if record is None else _positive(record.sma50)
    if record is None or sma50 is None:
        return None, "50-day average unavailable"
    units = {
        PortfolioService._quote_currency(record.currency),
        PortfolioService._quote_currency(position.price_currency),
    }
    if len(units) > 1:
        if units & _PENCE_UNITS:
            return None, "50-day average in a different unit"
        return None, "50-day average is in another currency"
    if _is_stale(record.as_of, as_of):
        return None, "50-day average is stale"
    return sma50, None


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
    Strategy descriptor's default parameters, consulted only when the stored
    maximum-loss setting is missing or unusable (None when discovery
    failed). ``as_of`` is the tab's newest analysis date: a record more than
    ``SMA50_MAX_AGE_DAYS`` older loses its 50-day average. The
    maximum-loss stop is position-level, from average cost.
    """
    if strategy_id is None:
        return StopSuggestion(note="No Strategy assigned")
    if strategy_id not in STOP_RULES:
        name = display_name or strategy_id
        return StopSuggestion(note=f"No price stop rule for {name}")
    if position.cost_currency != position.price_currency:
        return StopSuggestion(note="Cost and price are in different units")
    avg_cost = _positive(position.avg_cost)
    if avg_cost is None:
        return StopSuggestion(note="No average cost to base a stop on")
    pct = _max_loss_pct(parameters, defaults)
    if pct is None:
        return StopSuggestion(note="Maximum-loss setting unavailable")
    level = avg_cost * (1 - pct / 100)
    rule = f"max loss {pct:g}%"
    sma_note = None
    if STOP_RULES[strategy_id]:
        sma50, sma_note = _sma50(position, record, as_of)
        if sma50 is not None and sma50 > level:
            level, rule = sma50, "50-day avg"
    notes = [sma_note, MAX_LOSS_NOTE if rule.startswith("max loss") else None]
    price = _positive(position.current_price)
    return StopSuggestion(
        level=level,
        rule=rule,
        note=" · ".join(n for n in notes if n) or None,
        distance_pct=None if price is None else (level / price - 1) * 100,
        at_or_below=price is not None and price <= level,
    )
