"""Deterministic Portfolio Risk Coach engine (GH-16).

``evaluate`` turns one portfolio's positions, stops, sectors and GBP cash
into a :class:`RiskReportV1` against a fixed :class:`RiskPolicyV1`. It is pure
over its inputs: no I/O of its own, no LLM, no sizing advice. Arithmetic is
``Decimal``, but every GBP conversion goes through the existing float-based
``amount_in_gbp`` (Decimal -> float -> Decimal at that boundary), which never
invents a rate: an unconvertible price makes the position unpriced, and an
unconvertible stop leaves it without an evidenced stop, rather than guessed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.schemas.portfolio_risk import (
    RiskFindingV1,
    RiskPolicyV1,
    RiskReportV1,
)
from app.schemas.trade import Position
from app.services.gbp_valuation_service import GbpValuationService
from app.services.snapshot_valuation import amount_in_gbp

STALE_PRICE_DAYS = 3
_HUNDRED = Decimal(100)
_ZERO = Decimal(0)
_ONE_DP = Decimal("0.1")
_NO_STOP_REASON = "neither a BUY-trade stop nor a scan stop exists"
_SEVERITY_RANK = {"high": 0, "medium": 1, "info": 2}


@dataclass(frozen=True)
class _Holding:
    """One priced position, valued in GBP."""

    ticker: str
    sector: str | None
    value: Decimal
    stop_value: Decimal | None
    stop_source: str | None
    price_text: str
    stop_text: str | None
    no_stop_reason: str = _NO_STOP_REASON


def evaluate(
    positions: Sequence[Position],
    *,
    sectors: Mapping[str, str | None],
    scan_stops: Mapping[str, tuple[float, str]],
    cash_gbp: float | None,
    gbpusd: float | None,
    prices_as_of: str | None,
    today: date,
    gbp_valuation: GbpValuationService,
    policy: RiskPolicyV1 | None = None,
) -> RiskReportV1:
    """Evaluate one portfolio against ``policy`` (default :class:`RiskPolicyV1`).

    ``sectors`` and ``scan_stops`` are keyed by canonical ``Position.ticker``;
    a scan stop carries its own quote currency. ``cash_gbp`` is the GBP
    provider snapshot (``None`` = never set). Findings are ordered high ->
    medium -> info, then by magnitude descending, then by ticker.
    """
    policy = policy or RiskPolicyV1()
    holdings: list[_Holding] = []
    ranked: list[tuple[Decimal, RiskFindingV1]] = []
    for pos in positions:
        holding = _value_holding(
            pos,
            sectors.get(pos.ticker),
            scan_stops.get(pos.ticker),
            gbpusd,
            gbp_valuation,
        )
        if isinstance(holding, RiskFindingV1):
            ranked.append((_ZERO, holding))
        else:
            holdings.append(holding)
    cash = _ZERO if cash_gbp is None else Decimal(str(cash_gbp))
    total = sum((h.value for h in holdings), cash)
    ranked += _stop_findings(holdings, total, policy)
    if total > 0:
        ranked += _position_findings(holdings, total, policy)
        ranked += _sector_findings(holdings, total, policy)
    ranked += _unknown_sector_findings(positions, sectors)
    ranked.append((_ZERO, _cash_finding(cash_gbp)))
    limitations = _limitations(ranked, cash_gbp, prices_as_of, today, positions)
    if positions and total <= 0:
        limitations += (
            "Portfolio value is not positive; concentration and "
            "capital-at-risk checks skipped.",
        )
    return RiskReportV1(
        policy=policy,
        findings=tuple(finding for _, finding in sorted(ranked, key=_order)),
        confidence="limited" if limitations else "complete",
        limitations=limitations,
        total_value_gbp=total,
    )


def _order(item: tuple[Decimal, RiskFindingV1]) -> tuple[int, Decimal, str, str]:
    magnitude, finding = item
    first_ticker = finding.tickers[0] if finding.tickers else ""
    return (_SEVERITY_RANK[finding.severity], -magnitude, first_ticker, finding.title)


def _to_gbp(
    amount: Decimal,
    currency: str,
    gbpusd: float | None,
    gbp_valuation: GbpValuationService,
) -> Decimal | None:
    converted = amount_in_gbp(float(amount), currency, gbpusd, gbp_valuation)
    return None if converted is None else Decimal(str(converted))


def _value_holding(
    pos: Position,
    sector: str | None,
    scan_stop: tuple[float, str] | None,
    gbpusd: float | None,
    gbp_valuation: GbpValuationService,
) -> _Holding | RiskFindingV1:
    """Value ``pos`` and its stop in GBP, or explain why it is unpriced.

    The BUY-trade stop (in ``cost_currency``) wins over the scan stop (in
    the scan record's quote currency); only a finite, positive stop counts.
    A stop with no GBP rate leaves the valued holding without an evidenced
    stop. Cost basis never stands in for a missing price or stop.
    """
    ticker = pos.display_symbol
    if not pos.shares > 0:
        return _unpriced(ticker, "it has a non-positive share count")
    if pos.current_price is None:
        return _unpriced(ticker, "no current price is available")
    shares = Decimal(str(pos.shares))
    value = _to_gbp(
        shares * Decimal(str(pos.current_price)),
        pos.price_currency,
        gbpusd,
        gbp_valuation,
    )
    if value is None:
        return _unpriced(ticker, f"its {pos.price_currency} price has no GBP rate")
    price_text = f"{pos.current_price:,.2f} {pos.price_currency}"
    stop: tuple[float, str, str] | None = None
    if pos.stop_loss is not None and _valid_stop(pos.stop_loss):
        stop = (pos.stop_loss, pos.cost_currency, "BUY trade")
    elif scan_stop and _valid_stop(scan_stop[0]):
        stop = (scan_stop[0], scan_stop[1], "scan analysis")
    if stop is None:
        return _Holding(ticker, sector, value, None, None, price_text, None)
    stop_text = f"{stop[0]:,.2f} {stop[1]}"
    stop_value = _to_gbp(shares * Decimal(str(stop[0])), stop[1], gbpusd, gbp_valuation)
    if stop_value is None:
        reason = f"its {stop[2]} stop ({stop_text}) could not be converted to GBP"
        return _Holding(ticker, sector, value, None, None, price_text, None, reason)
    return _Holding(ticker, sector, value, stop_value, stop[2], price_text, stop_text)


def _valid_stop(stop: float) -> bool:
    """Only a finite, positive stop is evidence; 0, negative or NaN is none."""
    return math.isfinite(stop) and stop > 0


def _unpriced(ticker: str, reason: str) -> RiskFindingV1:
    return RiskFindingV1(
        kind="unpriced",
        severity="medium",
        title=f"{ticker} is unpriced",
        detail=(f"{ticker} is excluded from every percentage because {reason}."),
        tickers=(ticker,),
        inputs=(("Reason", reason),),
        action="Review the price feed for this holding before relying on totals.",
    )


def _position_findings(
    holdings: list[_Holding], total: Decimal, policy: RiskPolicyV1
) -> list[tuple[Decimal, RiskFindingV1]]:
    findings: list[tuple[Decimal, RiskFindingV1]] = []
    for h in holdings:
        weight = h.value / total * _HUNDRED
        if _shown(weight) <= policy.max_position_pct:
            continue
        findings.append(
            (
                weight,
                RiskFindingV1(
                    kind="position_concentration",
                    severity="high",
                    title=f"{h.ticker} is {_pct(weight)} of the portfolio",
                    detail=(
                        f"{h.ticker} exceeds the {policy.max_position_pct}% "
                        "single-position limit."
                    ),
                    tickers=(h.ticker,),
                    inputs=(
                        ("Position value", _gbp(h.value)),
                        ("Portfolio value", _gbp(total)),
                        ("Weight", _pct(weight)),
                        ("Limit", f"{policy.max_position_pct}%"),
                    ),
                    action="Review whether this concentration is intended.",
                ),
            )
        )
    return findings


def _sector_findings(
    holdings: list[_Holding], total: Decimal, policy: RiskPolicyV1
) -> list[tuple[Decimal, RiskFindingV1]]:
    """Flag each known sector whose priced weight exceeds the sector limit."""
    grouped: dict[str, list[_Holding]] = {}
    for h in holdings:
        if h.sector:
            grouped.setdefault(h.sector, []).append(h)
    findings: list[tuple[Decimal, RiskFindingV1]] = []
    for sector, members in grouped.items():
        value = sum((h.value for h in members), _ZERO)
        weight = value / total * _HUNDRED
        if _shown(weight) <= policy.max_sector_pct:
            continue
        tickers = tuple(sorted(h.ticker for h in members))
        findings.append(
            (
                weight,
                RiskFindingV1(
                    kind="sector_concentration",
                    severity="high",
                    title=f"{sector} is {_pct(weight)} of the portfolio",
                    detail=(
                        f"{sector} exceeds the {policy.max_sector_pct}% sector limit."
                    ),
                    tickers=tickers,
                    inputs=(
                        ("Sector value", _gbp(value)),
                        ("Portfolio value", _gbp(total)),
                        ("Weight", _pct(weight)),
                        ("Limit", f"{policy.max_sector_pct}%"),
                        ("Holdings", ", ".join(tickers)),
                    ),
                    action="Review whether this sector exposure is intended.",
                ),
            )
        )
    return findings


def _unknown_sector_findings(
    positions: Sequence[Position], sectors: Mapping[str, str | None]
) -> list[tuple[Decimal, RiskFindingV1]]:
    tickers = tuple(
        sorted(p.display_symbol for p in positions if not sectors.get(p.ticker))
    )
    if not tickers:
        return []
    finding = RiskFindingV1(
        kind="unknown_sector",
        severity="info",
        title="Unknown sector",
        detail="These holdings have no sector in the latest analysis and are "
        "left out of the sector check.",
        tickers=tickers,
        inputs=(("Holdings", ", ".join(tickers)),),
        action="Review these holdings' sector exposure manually.",
    )
    return [(_ZERO, finding)]


def _stop_findings(
    holdings: list[_Holding], total: Decimal, policy: RiskPolicyV1
) -> list[tuple[Decimal, RiskFindingV1]]:
    """Per-position stop findings plus the aggregate capital-at-risk finding.

    A position at or below its stop contributes 0 to the aggregate; one with
    no evidenced stop is excluded from it.
    """
    findings: list[tuple[Decimal, RiskFindingV1]] = []
    at_risk: list[tuple[_Holding, Decimal]] = []
    for h in holdings:
        weight = h.value / total * _HUNDRED if total > 0 else _ZERO
        if h.stop_value is None:
            findings.append((weight, _no_stop(h)))
        elif h.value <= h.stop_value:
            findings.append((weight, _below_stop(h)))
            at_risk.append((h, _ZERO))
        else:
            at_risk.append((h, h.value - h.stop_value))
    if at_risk and total > 0:
        findings.append(_capital_at_risk(at_risk, total, policy))
    return findings


def _no_stop(h: _Holding) -> RiskFindingV1:
    return RiskFindingV1(
        kind="no_stop",
        severity="medium",
        title=f"{h.ticker} has no evidenced stop",
        detail=(
            f"{h.ticker} is left out of capital at risk because {h.no_stop_reason}."
        ),
        tickers=(h.ticker,),
        inputs=(
            ("Position value", _gbp(h.value)),
            ("Price", h.price_text),
            ("Reason", h.no_stop_reason),
        ),
        action="Review and record a stop for this holding.",
    )


def _below_stop(h: _Holding) -> RiskFindingV1:
    return RiskFindingV1(
        kind="below_stop",
        severity="high",
        title=f"{h.ticker} is at or below its stop",
        detail=f"{h.ticker}'s price is at or below its {h.stop_source} stop.",
        tickers=(h.ticker,),
        inputs=(
            ("Price", h.price_text),
            ("Stop", h.stop_text or ""),
            ("Stop source", h.stop_source or ""),
            ("Value at price", _gbp(h.value)),
            ("Value at stop", _gbp(h.stop_value or _ZERO)),
        ),
        action="Review this position against your exit plan.",
    )


def _capital_at_risk(
    at_risk: list[tuple[_Holding, Decimal]], total: Decimal, policy: RiskPolicyV1
) -> tuple[Decimal, RiskFindingV1]:
    risk = sum((amount for _, amount in at_risk), _ZERO)
    pct = risk / total * _HUNDRED
    over = _shown(pct) > policy.max_capital_at_risk_pct
    at_stop = sorted(h.ticker for h, amount in at_risk if amount == 0)
    per_position = tuple(
        (
            f"{h.ticker} ({h.stop_source} stop {h.stop_text})",
            f"{_gbp(amount)} ({_pct(amount / total * _HUNDRED)})",
        )
        for h, amount in sorted(at_risk, key=lambda item: (-item[1], item[0].ticker))
    )
    finding = RiskFindingV1(
        kind="capital_at_risk",
        severity="high" if over else "info",
        title=f"Capital at risk to stops is {_pct(pct)}",
        detail=(
            f"Falling to every evidenced stop would cost {_gbp(risk)}, "
            f"{'above' if over else 'within'} the "
            f"{policy.max_capital_at_risk_pct}% limit."
            + (
                f" {len(at_stop)} holding(s) at or below their stop "
                f"({', '.join(at_stop)}) count as £0.00 in this total."
                if at_stop
                else ""
            )
        ),
        tickers=tuple(sorted(h.ticker for h, _ in at_risk)),
        inputs=(
            *per_position,
            ("Total at risk", _gbp(risk)),
            ("Portfolio value", _gbp(total)),
            ("Limit", f"{policy.max_capital_at_risk_pct}%"),
        ),
        action="Review your stops and exposure." if over else "",
    )
    return pct, finding


def _cash_finding(cash_gbp: float | None) -> RiskFindingV1:
    shown = "unknown" if cash_gbp is None else _gbp(Decimal(str(cash_gbp)))
    return RiskFindingV1(
        kind="cash",
        severity="info",
        title=f"Cash: {shown}",
        detail=(
            "No GBP cash snapshot is recorded; portfolio value excludes cash."
            if cash_gbp is None
            else "GBP cash from the provider snapshot is included in portfolio value."
        ),
        inputs=(("GBP cash", shown),),
    )


def _limitations(
    ranked: list[tuple[Decimal, RiskFindingV1]],
    cash_gbp: float | None,
    prices_as_of: str | None,
    today: date,
    positions: Sequence[Position],
) -> tuple[str, ...]:
    """Explain every gap that makes the report's confidence ``limited``."""
    kinds = [finding.kind for _, finding in ranked]
    limitations: list[str] = []
    if unpriced := kinds.count("unpriced"):
        limitations.append(
            f"{unpriced} holding(s) unpriced and excluded from percentages."
        )
    if no_stop := kinds.count("no_stop"):
        limitations.append(
            f"{no_stop} holding(s) without an evidenced stop are excluded "
            "from capital at risk."
        )
    if cash_gbp is None:
        limitations.append("Cash balance unknown; portfolio value excludes cash.")
    if positions:
        stale = _stale_prices(prices_as_of, today)
        if stale:
            limitations.append(stale)
    return tuple(limitations)


def _stale_prices(prices_as_of: str | None, today: date) -> str | None:
    """Return a limitation when prices are undated or older than 3 days."""
    try:
        as_of = date.fromisoformat((prices_as_of or "")[:10])
    except ValueError:
        return "Price date unknown; prices may be stale."
    if (today - as_of).days > STALE_PRICE_DAYS:
        return f"Prices as of {prices_as_of} are older than {STALE_PRICE_DAYS} days."
    return None


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}"


def _shown(percent: Decimal) -> Decimal:
    """Round a percentage exactly as it is displayed, for limit comparisons."""
    return percent.quantize(_ONE_DP)


def _pct(value: Decimal) -> str:
    return f"{_shown(value)}%"
