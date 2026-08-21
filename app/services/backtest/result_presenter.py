"""Pure presentation-only view-models for a completed Backtest Result
(Story 2.9).

Every helper here formats already-computed, already-persisted values from
Story 2.5's ``BacktestResultV1``/``snapshot_coverage`` -- it never
recomputes Metrics, variance, null reasons, fills, or provenance quality,
and it never rewrites a stored value. Rounding/signing happens only for
display in a local variable; the typed aggregate handed in is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import html

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestResultV1
from app.services.backtest.backtest_engine import (
    DividendAppliedEventV1,
    EntryFillEventV1,
    ExitFillEventV1,
    OpenPositionMarkEventV1,
    SkipReasonCode,
    SkippedSignalEventV1,
    SplitAppliedEventV1,
    TradeLogEvent,
)
from app.services.backtest.metrics import MetricUnavailableReason
from app.services.backtest.snapshot_profile import CoverageIntervalV1, CoverageSummaryV1
from app.services.backtest.trading_calendar import TradingCalendar

#: AC 2: the two null-Metric display strings, sourced only from
#: ``metric_availability`` -- never inferred/recomputed here.
_NO_CLOSED_TRADES_TEXT = "Not applicable — no closed trades"
_INSUFFICIENT_VARIATION_TEXT = "Not applicable — insufficient daily return variation"
_NOT_APPLICABLE_TEXT = "Not applicable"

_KIND_LABELS: dict[str, str] = {
    "entry_fill": "Buy",
    "exit_fill": "Sell",
    "skipped_signal": "Skipped",
    "split_applied": "Split",
    "dividend_applied": "Dividend",
    "open_position_mark": "Open mark",
}

_SKIP_REASON_TEXT: dict[SkipReasonCode, str] = {
    SkipReasonCode.DUPLICATE_SIGNAL: "Duplicate signal",
    SkipReasonCode.SIGNAL_SESSION_MISMATCH: "Signal/session mismatch",
    SkipReasonCode.INELIGIBLE_SECURITY: "Ineligible security",
    SkipReasonCode.POSITION_CONFLICT: "Position conflict",
    SkipReasonCode.INSUFFICIENT_CASH: "Insufficient cash",
    SkipReasonCode.POSITION_SIZE_ZERO: "Position size zero",
    SkipReasonCode.FILL_BEYOND_END: "Fill beyond run end",
}

_EXECUTED_FILL_KINDS = frozenset({"entry_fill", "exit_fill"})


@dataclass(frozen=True)
class MetricsViewV1:
    """One formatted rendering of AD-8's fixed four Metrics (AC 2)."""

    total_return: str
    sharpe_ratio: str
    win_rate: str
    max_drawdown: str


@dataclass(frozen=True)
class TradeLogRowV1:
    """One Trade Log row in a stable semantic column set (AC 4) -- a cell
    reads "—" when its column does not apply to this row's event kind."""

    sequence: int
    kind: str
    kind_label: str
    security_id: str
    date: str
    shares: str
    price: str
    pnl: str
    rule_id: str
    detail: str


@dataclass(frozen=True)
class TradeLogViewV1:
    """The complete ordered Trade Log plus the two empty-state flags AC 5
    needs ("no events" vs. "events but no executed fills")."""

    rows: tuple[TradeLogRowV1, ...]
    has_events: bool
    has_executed_fills: bool


@dataclass(frozen=True)
class ProvenanceEntryViewV1:
    """One provenance quality's coverage, clipped to the Result's own
    ``[start_month, end_month]`` window (AC 6) -- never the whole
    profile's coverage."""

    provenance_quality: str
    snapshot_count: int
    intervals: tuple[CoverageIntervalV1, ...]


@dataclass(frozen=True)
class ProvenanceViewV1:
    display_version: str
    entries: tuple[ProvenanceEntryViewV1, ...]


@dataclass(frozen=True)
class NoteViewV1:
    """Note text unescaped once for correct display/editing (never stored
    unescaped) -- see this module's docstring and ``deferred-work.md``'s
    double-escape hazard note."""

    text: str | None
    version: int


def _signed_percent(value: float, *, signed: bool) -> str:
    pct = round(value * 100, 1)
    if pct == 0:
        return "0.0%"
    sign = "+" if signed and pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def _unsigned_percent(value: float) -> str:
    return f"{round(value * 100, 1):.1f}%"


def _null_reason_text(reason: MetricUnavailableReason | None) -> str:
    if reason in (
        MetricUnavailableReason.INSUFFICIENT_DAILY_RETURNS,
        MetricUnavailableReason.ZERO_VARIANCE,
    ):
        return _INSUFFICIENT_VARIATION_TEXT
    return _NO_CLOSED_TRADES_TEXT


def metrics_view(result: BacktestResultV1) -> MetricsViewV1:
    """Format the four persisted Metrics exactly per AC 2's typography
    rules. Total Return/Max Drawdown are never ``None`` on a genuine
    complete Result; the ``None`` branch below is a defensive fallback
    only, never a recomputation."""
    metrics = result.metrics
    availability = result.metric_availability
    total_return = (
        _NOT_APPLICABLE_TEXT
        if metrics.total_return is None
        else _signed_percent(metrics.total_return, signed=True)
    )
    max_drawdown = (
        _NOT_APPLICABLE_TEXT
        if metrics.max_drawdown is None
        else _signed_percent(metrics.max_drawdown, signed=False)
    )
    sharpe = (
        _null_reason_text(availability.sharpe_unavailable)
        if metrics.sharpe_ratio is None
        else f"{metrics.sharpe_ratio:.2f}"
    )
    win_rate = (
        _null_reason_text(availability.win_rate_unavailable)
        if metrics.win_rate is None
        else _unsigned_percent(metrics.win_rate)
    )
    return MetricsViewV1(
        total_return=total_return,
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
    )


def equity_curve_payload(result: BacktestResultV1) -> tuple[dict[str, object], ...]:
    """Return one ordered ``{date, equity, equity_display}`` series (AC 3)
    -- the single server-produced payload the chart and its data-table
    disclosure both render from, via one ``tojson`` blob and one Jinja
    loop over the same list. Equity is rounded to 2dp for display only;
    ``result.equity_curve`` itself is never touched."""
    payload: list[dict[str, object]] = []
    for point in result.equity_curve:
        equity = float(point.total_equity_base.quantize(Decimal("0.01")))
        payload.append(
            {
                "date": point.session.isoformat(),
                "equity": equity,
                "equity_display": f"{equity:,.2f}",
            }
        )
    return tuple(payload)


def comparison_equity_payload(
    left: BacktestResultV1, right: BacktestResultV1
) -> tuple[dict[str, object], ...]:
    """Return one ordered, shared-timeline ``{date, equity_a,
    equity_a_display, equity_b, equity_b_display}`` series for Story 3.3's
    Comparison view.

    The two Results' Equity Curves are zipped by index -- never merged or
    reindexed -- after verifying their session dates match exactly. AD-20's
    determinism guarantee plus Story 3.1's period/profile-hash equality
    make a divergent sequence a genuine data-corruption signal for an
    already-eligible pair, so a mismatch always raises
    :class:`BacktestIntegrityError` rather than being reconciled. Equity is
    rounded to 2dp for display only, via the same convention
    ``equity_curve_payload`` uses; neither Result's ``equity_curve`` is
    ever touched.
    """
    left_sessions = tuple(point.session for point in left.equity_curve)
    right_sessions = tuple(point.session for point in right.equity_curve)
    if left_sessions != right_sessions:
        raise BacktestIntegrityError(
            "comparison equity curves diverge: session dates do not match "
            "between the two Results"
        )
    payload: list[dict[str, object]] = []
    for left_point, right_point in zip(left.equity_curve, right.equity_curve):
        equity_a = float(left_point.total_equity_base.quantize(Decimal("0.01")))
        equity_b = float(right_point.total_equity_base.quantize(Decimal("0.01")))
        payload.append(
            {
                "date": left_point.session.isoformat(),
                "equity_a": equity_a,
                "equity_a_display": f"{equity_a:,.2f}",
                "equity_b": equity_b,
                "equity_b_display": f"{equity_b:,.2f}",
            }
        )
    return tuple(payload)


def _money(value: Decimal, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def _trade_log_row(event: TradeLogEvent, base_currency: str) -> TradeLogRowV1:
    kind_label = _KIND_LABELS[event.kind]
    if isinstance(event, SkippedSignalEventV1):
        reason_text = _SKIP_REASON_TEXT.get(event.reason, event.reason.value)
        return TradeLogRowV1(
            sequence=event.sequence,
            kind=event.kind,
            kind_label=kind_label,
            security_id=event.security_id,
            date=event.signal_session.isoformat(),
            shares="—",
            price="—",
            pnl="—",
            rule_id=event.rule_id,
            detail=f"{reason_text}: {event.detail}",
        )
    if isinstance(event, (EntryFillEventV1, ExitFillEventV1)):
        detail = "—"
        if event.fx_rate is not None and event.fx_session is not None:
            detail = f"FX {event.fx_rate} on {event.fx_session}"
        pnl = (
            _money(event.realized_pnl_base, base_currency)
            if isinstance(event, ExitFillEventV1)
            else "—"
        )
        return TradeLogRowV1(
            sequence=event.sequence,
            kind=event.kind,
            kind_label=kind_label,
            security_id=event.security_id,
            date=event.fill_session.isoformat(),
            shares=str(event.shares),
            price=_money(event.fill_price_native, event.fill_currency),
            pnl=pnl,
            rule_id=event.rule_id,
            detail=detail,
        )
    if isinstance(event, SplitAppliedEventV1):
        return TradeLogRowV1(
            sequence=event.sequence,
            kind=event.kind,
            kind_label=kind_label,
            security_id=event.security_id,
            date=event.session.isoformat(),
            shares="—",
            price="—",
            pnl="—",
            rule_id="—",
            detail=(
                f"Ratio {event.ratio}: {event.shares_before} → "
                f"{event.shares_after} shares"
            ),
        )
    if isinstance(event, DividendAppliedEventV1):
        return TradeLogRowV1(
            sequence=event.sequence,
            kind=event.kind,
            kind_label=kind_label,
            security_id=event.security_id,
            date=event.session.isoformat(),
            shares="—",
            price=_money(event.per_share_amount, event.currency),
            pnl="—",
            rule_id="—",
            detail=(
                f"{event.shares_carried} shares credited "
                f"{_money(event.cash_credit_base, base_currency)}"
            ),
        )
    # OpenPositionMarkEventV1: an open final mark, never a fabricated exit
    # and never counted as a closed trade (AC 5).
    assert isinstance(event, OpenPositionMarkEventV1)
    return TradeLogRowV1(
        sequence=event.sequence,
        kind=event.kind,
        kind_label=kind_label,
        security_id=event.security_id,
        date=event.session.isoformat(),
        shares=str(event.shares),
        price=f"{event.mark_price_native:,.2f} (native)",
        pnl=_money(event.unrealized_pnl_base, f"{base_currency} unrealized"),
        rule_id="—",
        detail=(
            f"Market value {_money(event.market_value_base, base_currency)}, "
            f"cost basis {_money(event.cost_basis_base, base_currency)} "
            "— open at run end, not a fabricated exit."
        ),
    )


def trade_log_view(result: BacktestResultV1) -> TradeLogViewV1:
    """Map every persisted event to one stable column set in ``sequence``
    order (AC 4) -- no event kind is dropped or forced into a buy/sell
    shape, and the two AC 5 empty states are computed here, not inferred
    from Metrics."""
    rows = tuple(_trade_log_row(event, result.base_currency) for event in result.events)
    has_executed_fills = any(row.kind in _EXECUTED_FILL_KINDS for row in rows)
    return TradeLogViewV1(
        rows=rows, has_events=bool(rows), has_executed_fills=has_executed_fills
    )


def _clip_interval(
    interval: CoverageIntervalV1, start_month: str, end_month: str
) -> CoverageIntervalV1 | None:
    clipped_start = max(interval.start_month, start_month)
    clipped_end = min(interval.end_month, end_month)
    if clipped_start > clipped_end:
        return None
    return CoverageIntervalV1(start_month=clipped_start, end_month=clipped_end)


def provenance_view(
    result: BacktestResultV1, coverage: CoverageSummaryV1
) -> ProvenanceViewV1:
    """Filter ``coverage.provenance`` (the *whole* pinned profile's
    coverage) down to the Result's own ``[start_month, end_month]``
    window (Design Notes) -- never current active coverage, and never one
    blended reconstructed/observed claim when the window spans both."""
    entries: list[ProvenanceEntryViewV1] = []
    for item in coverage.provenance:
        clipped_intervals = tuple(
            clipped
            for interval in item.intervals
            if (
                clipped := _clip_interval(
                    interval, result.start_month, result.end_month
                )
            )
            is not None
        )
        if not clipped_intervals:
            continue
        # Reuse the source's own count untouched when clipping was a
        # no-op (the Result's window fully contains this quality's
        # coverage); only derive a new count -- via the same month-range
        # convention the source itself groups intervals by -- when the
        # window actually narrowed it. This never recomputes provenance
        # quality itself, only a display count for a narrower sub-range
        # the source has no pre-aggregated count for.
        if clipped_intervals == item.intervals:
            snapshot_count = item.snapshot_count
        else:
            snapshot_count = sum(
                len(TradingCalendar.months_inclusive(iv.start_month, iv.end_month))
                for iv in clipped_intervals
            )
        entries.append(
            ProvenanceEntryViewV1(
                provenance_quality=item.provenance_quality,
                snapshot_count=snapshot_count,
                intervals=clipped_intervals,
            )
        )
    return ProvenanceViewV1(
        display_version=coverage.display_version, entries=tuple(entries)
    )


def note_view(result: BacktestResultV1) -> NoteViewV1:
    """Unescape the stored (already-``html.escape``d) note exactly once
    for correct display/editing -- the presenter's read side of the
    double-escape hazard documented in ``deferred-work.md``. Never marked
    ``|safe``; Jinja's normal autoescaping re-escapes it exactly once for
    HTML output."""
    text = None if result.note is None else html.unescape(result.note)
    return NoteViewV1(text=text, version=result.note_version)


__all__ = [
    "MetricsViewV1",
    "TradeLogRowV1",
    "TradeLogViewV1",
    "ProvenanceEntryViewV1",
    "ProvenanceViewV1",
    "NoteViewV1",
    "metrics_view",
    "equity_curve_payload",
    "comparison_equity_payload",
    "trade_log_view",
    "provenance_view",
    "note_view",
]
