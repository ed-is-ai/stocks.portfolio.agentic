"""One honest GBP valuation rule for every portfolio snapshot writer.

The live summary cards and the stored value-history snapshots must agree, so
the native-currency -> GBP conversion lives here once (``amount_in_gbp``) and
is reused by ``PortfolioService`` and ``TraderAgent`` alike. Aggregation
(``value_positions_gbp``) is deliberately all-or-nothing: a holdings market
value is only reported when *every* open position could be valued, so a
partially-priced portfolio is recorded as unavailable rather than as a
plausible-looking under-count.

Dependencies are kept to ``app.core.money``, ``app.services.
gbp_valuation_service`` and ``app.schemas.trade`` so both callers can import
it without a cycle.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.money import Money
from app.schemas.trade import Position
from app.services.gbp_valuation_service import GbpValuationService

#: ``valued`` -- every open position priced and converted.
#: ``incomplete`` -- some, but not all, positions could be valued.
#: ``unavailable`` -- open positions exist but none could be valued.
#: ``empty`` -- no open positions at all (a genuine 0.0, not a gap).
SnapshotStatus = Literal["valued", "incomplete", "unavailable", "empty"]


def valid_rate_or_none(rate: object) -> float | None:
    """Return ``rate`` as a finite positive float, else None.

    A missing, zero, negative, NaN or non-numeric FX rate is never usable:
    callers must leave the holding unvalued rather than fabricate one.
    """
    if isinstance(rate, (bool, str, bytes)) or rate is None:
        return None
    try:
        numeric = float(rate)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def amount_in_gbp(
    amount: float,
    currency: str,
    gbpusd: float | None,
    gbp_valuation: GbpValuationService,
) -> float | None:
    """Value one native-currency holding amount in GBP, or None if it can't be.

    ``USD`` uses the supplied GBP/USD rate (units of USD per 1 GBP), LSE pence
    (``GBp``/``GBX``) is divided by 100, and any other non-GBP currency is
    valued through :class:`GbpValuationService`, which returns no amount when
    it has no same-day quote. No branch ever invents a rate.
    """
    if not math.isfinite(amount):
        return None
    unit = currency.strip()
    if unit in {"GBp", "GBX"}:
        return amount / 100
    if unit in {"GBP", ""}:
        return amount
    if unit == "USD":
        rate = valid_rate_or_none(gbpusd)
        return None if rate is None else amount / rate
    projection = gbp_valuation.value_in_gbp(
        Money(amount=Decimal(str(amount)), currency=unit)
    )
    return float(projection.gbp_amount) if projection.gbp_amount is not None else None


class SnapshotValuation(BaseModel):
    """The GBP valuation of one portfolio's open holdings at a point in time.

    ``market_value_gbp`` is None whenever ``status`` is ``incomplete`` or
    ``unavailable`` -- a snapshot writer must persist NULL there, never a
    partial total or a fabricated zero.
    """

    model_config = ConfigDict(frozen=True)

    market_value_gbp: float | None
    cost_gbp: float | None
    valued_positions: int
    unvalued_positions: int
    status: SnapshotStatus


def value_positions_gbp(
    positions: list[Position],
    gbpusd: float | None,
    gbp_valuation: GbpValuationService,
) -> SnapshotValuation:
    """Aggregate ``positions`` into a GBP :class:`SnapshotValuation`.

    An empty holdings list is a genuine ``0.0`` (a cash-only portfolio), not a
    gap. Otherwise the market value is reported only when every position has
    both a ``current_value`` and a usable conversion; cost is reported only
    when every position's cost converts.
    """
    if not positions:
        return SnapshotValuation(
            market_value_gbp=0.0,
            cost_gbp=0.0,
            valued_positions=0,
            unvalued_positions=0,
            status="empty",
        )

    values: list[float] = []
    costs: list[float] = []
    unvalued = 0
    cost_complete = True
    for position in positions:
        cost = amount_in_gbp(
            position.total_cost, position.price_currency, gbpusd, gbp_valuation
        )
        if cost is None:
            cost_complete = False
        else:
            costs.append(cost)
        value = (
            None
            if position.current_value is None
            else amount_in_gbp(
                position.current_value,
                position.price_currency,
                gbpusd,
                gbp_valuation,
            )
        )
        if value is None:
            unvalued += 1
        else:
            values.append(value)

    valued = len(values)
    if valued == 0:
        status: SnapshotStatus = "unavailable"
    elif unvalued:
        status = "incomplete"
    else:
        status = "valued"
    return SnapshotValuation(
        market_value_gbp=round(sum(values), 2) if status == "valued" else None,
        cost_gbp=round(sum(costs), 2) if cost_complete else None,
        valued_positions=valued,
        unvalued_positions=unvalued,
        status=status,
    )
