"""Pure presentation-only view-models for a completed Backtest Result
(Story 2.9).

Every helper here formats already-computed, already-persisted values from
Story 2.5's ``BacktestResultV1``/``snapshot_coverage`` -- it never
recomputes Metrics, variance, null reasons, fills, or provenance quality,
and it never rewrites a stored value. Rounding/signing happens only for
display in a local variable; the typed aggregate handed in is read-only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
import html
import math

from app.repositories.backtest_repo import (
    BacktestCandidateAuditPageV1,
    BacktestIntegrityError,
    BacktestResultV1,
)
from app.services.backtest.backtest_engine import (
    CandidateAuditDisposition,
    DividendAppliedEventV1,
    EntryFillEventV1,
    ExitFillEventV1,
    OpenPositionMarkEventV1,
    SkipReasonCode,
    SkippedSignalEventV1,
    SplitAppliedEventV1,
    TerminalSettlementEventV1,
    TradeLogEvent,
)
from app.services.backtest.metrics import MetricUnavailableReason
from app.services.backtest.snapshot_profile import CoverageIntervalV1, CoverageSummaryV1
from app.services.backtest.strategy_protocol import EntrySelectionState
from app.services.backtest.strategy_explanation import format_decimal, format_reason
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
    "terminal_settlement": "Exit settled",
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
    SkipReasonCode.MAX_CONCURRENT_POSITIONS: "Position limit reached",
    SkipReasonCode.SECURITY_EXITED: "Security exited",
}

_EXECUTED_FILL_KINDS = frozenset({"entry_fill", "exit_fill"})

#: Primary Security-cell label when a row's ``security_id`` is not present
#: in the run's pinned reconstruction roster.
UNRESOLVED_SECURITY_LABEL = "Unknown security"

# Buy-and-Hold's persisted reason vocabulary is an audit contract, but its
# machine-facing codes do not belong in the Result UI.  Keep this mapping at
# the presentation boundary: it neither re-ranks nor derives a decision.
_INITIAL_BASKET_EXCLUSION_TEXT: dict[str, str] = {
    "entry_cutoff_not_reached": "The basket-selection date is before the configured entry date.",
    "invalid_entry_cutoff": "The configured entry date is invalid.",
    "history_unavailable": "Price history is unavailable for the 252-session return.",
    "insufficient_history": "Insufficient price history for the 252-session return.",
    "invalid_close_history": "Price history is invalid for the 252-session return.",
    "regime_filter_not_permitted": "Market conditions did not permit an initial entry.",
}

_INITIAL_BASKET_OUTCOME_TEXT: dict[EntrySelectionState, str] = {
    EntrySelectionState.SELECTED: "Selected",
    EntrySelectionState.ELIGIBLE_NOT_SELECTED: "Eligible, not selected",
    EntrySelectionState.EXCLUDED: "Excluded",
}


def resolve_security_label(
    security_id: str, identities: Mapping[str, tuple[str, str]]
) -> str:
    """Resolve ``security_id`` to a readable label via the run's pinned
    roster identity map (``security_id -> (provider_symbol, mic)``).

    Returns ``f"{symbol} ({mic})"`` when ``mic`` is non-empty, the bare
    ``symbol`` otherwise, and :data:`UNRESOLVED_SECURITY_LABEL` on a miss
    (key absent, or a blank/whitespace provider symbol). Never raises.
    """
    identity = identities.get(security_id)
    if identity is None:
        return UNRESOLVED_SECURITY_LABEL
    symbol, mic = identity
    symbol, mic = symbol.strip(), mic.strip()
    if not symbol:
        return UNRESOLVED_SECURITY_LABEL
    return f"{symbol} ({mic})" if mic else symbol


#: Universe-cell placeholder for a run with no persisted universe
#: selection (legacy ``selection_json`` is NULL) -- display-only.
NO_UNIVERSE_LABEL = "—"

#: ``UniverseViewV1.selection_mode`` values (gh-434). ``None`` means no
#: persisted selection, so the Universe composition panel is hidden.
WHOLE_UNIVERSE_MODE = "whole universe"
SELECTED_SECURITIES_MODE = "selected securities"


@dataclass(frozen=True)
class UniverseViewV1:
    """Display-only universe summary shared by the results list and the
    Result page's Run identity card (gh-434).

    ``label`` is the compact cell/row text ("Whole universe (N)", "N
    securities", a single resolved ticker, or :data:`NO_UNIVERSE_LABEL`);
    ``tickers`` are the selection's labels resolved via the run's pinned
    roster (sorted, with :data:`UNRESOLVED_SECURITY_LABEL` for misses);
    the composition numbers feed the Result page's Universe composition
    panel -- all ``None`` when there is no persisted selection."""

    label: str
    tickers: tuple[str, ...]
    roster_count: int | None = None
    runnable_count: int | None = None
    excluded_count: int | None = None
    selection_mode: str | None = None


def build_universe_view(
    security_ids: tuple[str, ...] | None,
    identities: Mapping[str, tuple[str, str]],
    runnable_ids: tuple[str, ...] | None = None,
) -> UniverseViewV1:
    """Build the shared universe view-model for one Backtest run (gh-434).

    ``security_ids`` is the run's canonical selection (``None`` for a
    legacy run without persisted ``selection_json``); ``identities`` is
    the pinned roster's ``security_id -> (provider_symbol, mic)`` map;
    ``runnable_ids`` are the ``valid_scan`` member IDs for the run's
    ``(profile_hash, start_month)`` snapshot -- ``None`` when the snapshot
    month is unavailable, which suppresses the whole-universe claim and
    the excluded count rather than guessing. The whole-universe claim
    requires the selection to equal the runnable set (not merely match
    its size) so a hand-ticked subset is never mislabelled. Never
    raises."""
    if not security_ids:
        return UniverseViewV1(label=NO_UNIVERSE_LABEL, tickers=())
    tickers = tuple(
        sorted(
            resolve_security_label(security_id, identities)
            for security_id in security_ids
        )
    )
    count = len(security_ids)
    roster_count = len(identities)
    runnable_count = None if runnable_ids is None else len(runnable_ids)
    excluded_count = (
        None
        if runnable_count is None or roster_count < runnable_count
        else roster_count - runnable_count
    )
    if runnable_ids is not None and set(security_ids) == set(runnable_ids):
        return UniverseViewV1(
            label=f"Whole universe ({count})",
            tickers=tickers,
            roster_count=roster_count,
            runnable_count=runnable_count,
            excluded_count=excluded_count,
            selection_mode=WHOLE_UNIVERSE_MODE,
        )
    if count == 1:
        label = tickers[0]
    else:
        label = f"{count} securities"
    return UniverseViewV1(
        label=label,
        tickers=tickers,
        roster_count=roster_count,
        runnable_count=runnable_count,
        excluded_count=excluded_count,
        selection_mode=SELECTED_SECURITIES_MODE,
    )


@dataclass(frozen=True)
class MetricsViewV1:
    """One formatted rendering of AD-8's fixed four Metrics (AC 2)."""

    total_return: str
    sharpe_ratio: str
    win_rate: str
    max_drawdown: str


@dataclass(frozen=True)
class MetricDisplayV1:
    """A formatted metric and its optional P&L-like colour class."""

    value: str
    css_class: str = ""


@dataclass(frozen=True)
class BacktestMetricsDisplayV1:
    """Shared list/Result metric presentation without changing Metrics."""

    total_return: MetricDisplayV1
    sharpe_ratio: MetricDisplayV1
    win_rate: MetricDisplayV1
    max_drawdown: MetricDisplayV1


@dataclass(frozen=True)
class ResultFinancialsViewV1:
    """Display-only values derived from an immutable completed Result."""

    starting_capital: MetricDisplayV1
    pnl: MetricDisplayV1


@dataclass(frozen=True)
class PerformanceSummaryViewV1:
    """Additional display-only context derived from verified fills and
    daily valuations. These values supplement, and never replace, the
    four persisted Backtest Metrics."""

    cagr: MetricDisplayV1
    mean_invested_exposure: MetricDisplayV1
    time_invested: MetricDisplayV1
    turnover: MetricDisplayV1
    exit_count: MetricDisplayV1


@dataclass(frozen=True)
class TradeLogRowV1:
    """One Trade Log row in a stable semantic column set (AC 4) -- a cell
    reads "—" when its column does not apply to this row's event kind."""

    sequence: int
    kind: str
    kind_label: str
    security_id: str
    security_label: str
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
class CandidateAuditRowViewV1:
    candidate_sequence: int
    security_id: str
    security_label: str
    side: str
    signal_session: str
    intended_fill_session: str
    outcome_session: str
    disposition: str
    priority: str
    host_cohort_position: str
    allocator_position: str
    slot_summary: str
    available_slots_before_cohort: str
    held_positions: tuple[str, ...]
    pending_buy_reservations: tuple[str, ...]
    prior_cohort_admissions: tuple[str, ...]
    sell_releases: tuple[str, ...]
    reservation_summary: str
    reason_code: str
    explanation: tuple[str, ...]
    event_sequence: int


@dataclass(frozen=True)
class CandidateAuditViewV1:
    recorded: bool
    candidate_count: int
    priority_recorded: int
    priority_missing: int
    explanation_recorded: int
    explanation_missing: int
    preflight_rejected: int
    full_book_rejected: int
    competition_rejected: int
    filled: int
    fill_rejected: int
    page: int
    total_pages: int
    rows: tuple[CandidateAuditRowViewV1, ...]


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


@dataclass(frozen=True)
class InitialBasketRowV1:
    """One immutable, persisted initial-selection decision for display."""

    rank: int
    security_id: str
    security_label: str
    trailing_return: str
    outcome: str
    exclusion: str | None


@dataclass(frozen=True)
class InitialBasketViewV1:
    """The Result-page projection of optional V2 selection evidence.

    ``recorded`` deliberately distinguishes an old Result (no evidence was
    persisted) from a recorded all-excluded selection (valid evidence with no
    selected members).
    """

    recorded: bool
    selection_session: str | None
    metric_id: str | None
    metric_version: str | None
    rows: tuple[InitialBasketRowV1, ...]
    has_selected: bool


def _unsigned_percent(value: float) -> str:
    return f"{round(value * 100, 1):.1f}%"


def _metric_percent(
    value: float, *, signed: bool, loss_classed: bool = False
) -> MetricDisplayV1:
    """Present a percentage using the shared one-decimal P&L convention."""
    pct = round(value * 100, 1)
    if pct == 0:
        return MetricDisplayV1("0.0%")
    css_class = "pos" if pct > 0 else "neg"
    if loss_classed:
        css_class = "neg" if pct < 0 else ""
    sign = "+" if signed and pct > 0 else ""
    return MetricDisplayV1(f"{sign}{pct:.1f}%", css_class)


def _currency(
    value: Decimal, currency: str, *, signed: bool = False
) -> MetricDisplayV1:
    """Format money in the run currency; no persistence/calculation occurs."""
    amount = value.quantize(Decimal("0.01"))
    if amount == 0:
        amount = Decimal(0)
    sign = "+" if signed and amount > 0 else ""
    body = f"{abs(amount):,.2f}"
    if amount < 0:
        sign = "-"
    display = f"{sign}£{body}" if currency == "GBP" else f"{sign}{body} {currency}"
    css_class = "pos" if amount > 0 else "neg" if amount < 0 else ""
    return MetricDisplayV1(display, css_class)


def _one_decimal_percent(value: Decimal) -> str:
    """Format a persisted Decimal score without changing the stored value."""
    with localcontext() as context:
        context.prec = max(
            28, len(value.as_tuple().digits) + max(value.adjusted(), 0) + 3
        )
        rounded = (value * Decimal("100")).quantize(Decimal("0.1"))
    if rounded == 0:
        rounded = Decimal(0)
    return f"{rounded:.1f}%"


def initial_basket_view(
    result: BacktestResultV1, identities: Mapping[str, tuple[str, str]]
) -> InitialBasketViewV1:
    """Project stored initial-entry evidence for the Result page only.

    This function intentionally does no price lookup, score calculation, or
    identity repair.  Its input is optional to preserve historical Results.
    """
    selection = result.initial_entry_selection
    if selection is None:
        return InitialBasketViewV1(
            recorded=False,
            selection_session=None,
            metric_id=None,
            metric_version=None,
            rows=(),
            has_selected=False,
        )
    rows = tuple(
        InitialBasketRowV1(
            rank=decision.rank,
            security_id=decision.security_id,
            security_label=resolve_security_label(decision.security_id, identities),
            trailing_return=(
                _one_decimal_percent(decision.score)
                if decision.score is not None
                else "—"
            ),
            outcome=_INITIAL_BASKET_OUTCOME_TEXT[decision.state],
            exclusion=(
                _INITIAL_BASKET_EXCLUSION_TEXT.get(
                    decision.reason_code or "", "Not eligible for the initial basket."
                )
                if decision.state is EntrySelectionState.EXCLUDED
                else None
            ),
        )
        for decision in sorted(selection.decisions, key=lambda item: item.rank)
    )
    return InitialBasketViewV1(
        recorded=True,
        selection_session=selection.session.isoformat(),
        metric_id=selection.metric_id,
        metric_version=selection.metric_version,
        rows=rows,
        has_selected=any(
            decision.state is EntrySelectionState.SELECTED
            for decision in selection.decisions
        ),
    )


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
    display = backtest_metrics_view(result.metrics, result.metric_availability)
    return MetricsViewV1(
        total_return=display.total_return.value,
        sharpe_ratio=display.sharpe_ratio.value,
        win_rate=display.win_rate.value,
        max_drawdown=display.max_drawdown.value,
    )


def backtest_metrics_view(
    metrics: object, availability: object
) -> BacktestMetricsDisplayV1:
    """Format persisted metric values for either Result or results-list views."""
    total_return = getattr(metrics, "total_return")
    max_drawdown = getattr(metrics, "max_drawdown", None)
    sharpe_ratio = getattr(metrics, "sharpe_ratio", None)
    win_rate = getattr(metrics, "win_rate", None)
    return BacktestMetricsDisplayV1(
        total_return=(
            MetricDisplayV1(_NOT_APPLICABLE_TEXT)
            if total_return is None
            else _metric_percent(total_return, signed=True)
        ),
        sharpe_ratio=(
            MetricDisplayV1(
                _null_reason_text(getattr(availability, "sharpe_unavailable", None))
            )
            if sharpe_ratio is None
            else MetricDisplayV1(f"{sharpe_ratio:.2f}")
        ),
        win_rate=(
            MetricDisplayV1(
                _null_reason_text(getattr(availability, "win_rate_unavailable", None))
            )
            if win_rate is None
            else MetricDisplayV1(_unsigned_percent(win_rate))
        ),
        max_drawdown=(
            MetricDisplayV1(_NOT_APPLICABLE_TEXT)
            if max_drawdown is None
            else _metric_percent(max_drawdown, signed=False, loss_classed=True)
        ),
    )


def result_financials_view(result: BacktestResultV1) -> ResultFinancialsViewV1:
    """Derive Result-only P&L from final persisted equity, for display only."""
    starting_capital = _currency(result.starting_capital, result.base_currency)
    if not result.equity_curve:
        return ResultFinancialsViewV1(
            starting_capital=starting_capital,
            pnl=MetricDisplayV1(_NOT_APPLICABLE_TEXT),
        )
    pnl = result.equity_curve[-1].total_equity_base - result.starting_capital
    return ResultFinancialsViewV1(
        starting_capital=starting_capital,
        pnl=_currency(pnl, result.base_currency, signed=True),
    )


def performance_summary_view(result: BacktestResultV1) -> PerformanceSummaryViewV1:
    """Format useful context that is not part of the fixed persisted
    Metrics contract. All inputs come from the verified equity curve and
    executed fills on ``result``; this does not run or alter a backtest."""
    curve = result.equity_curve
    not_applicable = MetricDisplayV1(_NOT_APPLICABLE_TEXT)
    cagr = not_applicable
    if curve and result.starting_capital > 0 and curve[-1].total_equity_base > 0:
        elapsed_days = max((curve[-1].session - curve[0].session).days, 1)
        try:
            rate = (
                math.pow(
                    float(curve[-1].total_equity_base / result.starting_capital),
                    365.2425 / elapsed_days,
                )
                - 1
            )
        except (OverflowError, ValueError):
            rate = math.nan
        if math.isfinite(rate):
            cagr = _metric_percent(rate, signed=True)

    invested_fractions = [
        point.positions_value_base / point.total_equity_base
        for point in curve
        if point.total_equity_base > 0
    ]
    if invested_fractions:
        mean_exposure = MetricDisplayV1(
            f"{float(sum(invested_fractions, Decimal(0)) / len(invested_fractions)) * 100:.2f}%"
        )
        time_invested = MetricDisplayV1(
            f"{sum(value > 0 for value in invested_fractions) * 100 / len(invested_fractions):.2f}%"
        )
    else:
        mean_exposure = not_applicable
        time_invested = not_applicable

    buys = sum(
        (
            event.cost_base
            for event in result.events
            if isinstance(event, EntryFillEventV1)
        ),
        Decimal(0),
    )
    sells = sum(
        (
            event.proceeds_base
            for event in result.events
            if isinstance(event, ExitFillEventV1)
        ),
        Decimal(0),
    )
    turnover = (
        MetricDisplayV1(f"{(buys + sells) / result.starting_capital:.2f}×")
        if result.starting_capital > 0
        else not_applicable
    )
    exits = sum(isinstance(event, ExitFillEventV1) for event in result.events)
    return PerformanceSummaryViewV1(
        cagr=cagr,
        mean_invested_exposure=mean_exposure,
        time_invested=time_invested,
        turnover=turnover,
        exit_count=MetricDisplayV1(f"{exits:,}"),
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


def multi_comparison_equity_payload(
    results: Sequence[BacktestResultV1],
) -> dict[str, object]:
    """Build indexed equity and drawdown paths for a small compatible
    result group. Every curve must share the exact ordered session axis;
    no missing date is interpolated or silently reindexed. Equity is
    indexed to 100 on each run's first session so different starting
    capital does not distort the comparison."""
    if len(results) < 2:
        raise ValueError("a multi-result comparison needs at least two Results")
    sessions = tuple(point.session for point in results[0].equity_curve)
    if not sessions:
        raise BacktestIntegrityError("comparison equity curve is empty")

    series: list[dict[str, object]] = []
    display_columns: list[tuple[str, ...]] = []
    equity_columns: list[tuple[str, ...]] = []
    drawdown_columns: list[tuple[str, ...]] = []
    for result in results:
        result_sessions = tuple(point.session for point in result.equity_curve)
        if result_sessions != sessions:
            raise BacktestIntegrityError(
                "comparison equity curves diverge: session dates do not match "
                "between the selected Results"
            )
        initial_equity = result.equity_curve[0].total_equity_base
        if initial_equity <= 0:
            raise BacktestIntegrityError(
                "comparison equity curve has a non-positive starting value"
            )

        values: list[float] = []
        equity_values: list[float] = []
        drawdowns: list[float] = []
        value_displays: list[str] = []
        equity_displays: list[str] = []
        drawdown_displays: list[str] = []
        peak = initial_equity
        for point in result.equity_curve:
            try:
                indexed = (point.total_equity_base / initial_equity * 100).quantize(
                    Decimal("0.01")
                )
                peak = max(peak, point.total_equity_base)
                drawdown = ((point.total_equity_base / peak - 1) * 100).quantize(
                    Decimal("0.01")
                )
                equity = point.total_equity_base.quantize(Decimal("0.01"))
                indexed_value, equity_value, drawdown_value = map(
                    float, (indexed, equity, drawdown)
                )
            except (DecimalException, OverflowError) as exc:
                raise BacktestIntegrityError(
                    "comparison equity values cannot be represented for display"
                ) from exc
            if not all(
                math.isfinite(value)
                for value in (indexed_value, equity_value, drawdown_value)
            ):
                raise BacktestIntegrityError(
                    "comparison equity values exceed the display range"
                )
            values.append(indexed_value)
            equity_values.append(equity_value)
            drawdowns.append(drawdown_value)
            value_displays.append(f"{indexed:,.2f}")
            equity_displays.append(f"{equity:,.2f}")
            drawdown_displays.append(f"{drawdown:,.2f}%")

        series.append(
            {
                "run_id": result.run_id,
                "label": (
                    f"{result.strategy_id} v{result.strategy_api_version} "
                    f"({result.run_id[:8]})"
                ),
                "values": tuple(values),
                "equity_values": tuple(equity_values),
                "drawdowns": tuple(drawdowns),
            }
        )
        display_columns.append(tuple(value_displays))
        drawdown_columns.append(tuple(drawdown_displays))
        equity_columns.append(tuple(equity_displays))

    table_rows = tuple(
        {
            "date": session.isoformat(),
            "values": tuple(column[index] for column in display_columns),
            "equities": tuple(column[index] for column in equity_columns),
            "drawdowns": tuple(column[index] for column in drawdown_columns),
        }
        for index, session in enumerate(sessions)
    )
    return {
        "dates": tuple(session.isoformat() for session in sessions),
        "series": tuple(series),
        "table_rows": table_rows,
    }


def _money(value: Decimal, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def _trade_log_row(
    event: TradeLogEvent,
    base_currency: str,
    identities: Mapping[str, tuple[str, str]],
) -> TradeLogRowV1:
    kind_label = _KIND_LABELS[event.kind]
    if isinstance(event, SkippedSignalEventV1):
        reason_text = _SKIP_REASON_TEXT.get(event.reason, event.reason.value)
        return TradeLogRowV1(
            sequence=event.sequence,
            kind=event.kind,
            kind_label=kind_label,
            security_id=event.security_id,
            security_label=resolve_security_label(event.security_id, identities),
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
            security_label=resolve_security_label(event.security_id, identities),
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
            security_label=resolve_security_label(event.security_id, identities),
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
            security_label=resolve_security_label(event.security_id, identities),
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
    if isinstance(event, TerminalSettlementEventV1):
        price = _money(event.settlement_price_native, event.currency)
        basis = event.price_basis.replace("_", " ")
        return TradeLogRowV1(
            sequence=event.sequence,
            kind=event.kind,
            kind_label=kind_label,
            security_id=event.security_id,
            security_label=resolve_security_label(event.security_id, identities),
            date=event.session.isoformat(),
            shares=str(event.shares),
            price=price,
            pnl=_money(event.realized_pnl_base, base_currency),
            rule_id="—",
            detail=(
                f"Exit settled at {price} ({basis}) — {event.exit_type} "
                f"on {event.exit_session.isoformat()}"
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
        security_label=resolve_security_label(event.security_id, identities),
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


def trade_log_view(
    result: BacktestResultV1, identities: Mapping[str, tuple[str, str]]
) -> TradeLogViewV1:
    """Map every persisted event to one stable column set in ``sequence``
    order (AC 4) -- no event kind is dropped or forced into a buy/sell
    shape, and the two AC 5 empty states are computed here, not inferred
    from Metrics."""
    rows = tuple(
        _trade_log_row(event, result.base_currency, identities)
        for event in result.events
    )
    has_executed_fills = any(row.kind in _EXECUTED_FILL_KINDS for row in rows)
    return TradeLogViewV1(
        rows=rows, has_events=bool(rows), has_executed_fills=has_executed_fills
    )


_CANDIDATE_AUDIT_DISPOSITION_TEXT: dict[CandidateAuditDisposition, str] = {
    CandidateAuditDisposition.PREFLIGHT_REJECTED: "Rejected before slot consideration",
    CandidateAuditDisposition.FULL_BOOK_REJECTED: "No usable slot",
    CandidateAuditDisposition.COMPETITION_REJECTED: "Lost an available slot to the cohort",
    CandidateAuditDisposition.FILLED: "Filled",
    CandidateAuditDisposition.FILL_REJECTED: "Admitted, then rejected at fill",
}


def candidate_audit_view(
    page: BacktestCandidateAuditPageV1,
    identities: Mapping[str, tuple[str, str]],
    base_currency: str,
) -> CandidateAuditViewV1:
    """Format one persisted candidate page without running Skills or prices."""
    summary = page.summary
    rows: list[CandidateAuditRowViewV1] = []
    for record in page.records:
        held_positions = tuple(
            f"{resolve_security_label(security_id, identities)} [{security_id}]"
            for security_id in record.held_security_ids
        )
        pending_buy_reservations = tuple(
            f"{resolve_security_label(order.security_id, identities)} "
            f"[{order.security_id}], signalled {order.signal_session.isoformat()}, "
            f"fills {order.fill_session.isoformat()}, reserves "
            f"{format_decimal(order.reserved_base or Decimal(0))} {base_currency}"
            for order in record.pending_buy_orders
        )
        prior_cohort_admissions = tuple(
            f"{resolve_security_label(security_id, identities)} [{security_id}]"
            for security_id in record.prior_cohort_admitted_security_ids
        )
        sell_releases = tuple(
            f"{resolve_security_label(order.security_id, identities)} "
            f"[{order.security_id}], signalled {order.signal_session.isoformat()}, "
            f"releases {order.fill_session.isoformat()} "
            f"({'by candidate fill' if record.intended_fill_session is not None and order.fill_session <= record.intended_fill_session else 'after candidate fill'})"
            for order in record.pending_sell_releases
        )
        qualifying_sell_releases = sum(
            record.intended_fill_session is not None
            and order.fill_session <= record.intended_fill_session
            for order in record.pending_sell_releases
        )
        if record.position_cap is None:
            slot_summary = "No position cap was configured."
        elif record.host_cohort_position is None:
            slot_summary = (
                "Preflight rejected this candidate before slot consideration."
            )
        else:
            slot_summary = (
                f"{record.available_slots_before_candidate or 0} slot(s) available "
                f"when considered; {record.occupied_slots} held or pending BUY; "
                f"{qualifying_sell_releases} of {len(record.pending_sell_releases)} "
                "scheduled SELL(s) release by fill date."
            )
        pending_base = format_decimal(record.pending_buy_reservation_base)
        allocation = (
            "not allocated"
            if record.allocation_target_base is None
            else format_decimal(record.allocation_target_base)
        )
        explanation: list[str] = []
        if record.explanation is not None:
            for reason in record.explanation.reasons:
                summary_text, facts = format_reason(reason)
                explanation.append(f"{reason.code}: {summary_text}")
                explanation.extend(facts)
        rows.append(
            CandidateAuditRowViewV1(
                candidate_sequence=record.candidate_sequence,
                security_id=record.security_id,
                security_label=resolve_security_label(record.security_id, identities),
                side=record.side.value,
                signal_session=record.signal_session.isoformat(),
                intended_fill_session=(
                    "—"
                    if record.intended_fill_session is None
                    else record.intended_fill_session.isoformat()
                ),
                outcome_session=record.outcome_session.isoformat(),
                disposition=_CANDIDATE_AUDIT_DISPOSITION_TEXT[record.disposition],
                priority=(
                    "Not supplied"
                    if record.priority is None
                    else format_decimal(record.priority)
                ),
                host_cohort_position=(
                    "—"
                    if record.host_cohort_position is None
                    else str(record.host_cohort_position)
                ),
                allocator_position=(
                    "—"
                    if record.allocator_position is None
                    else str(record.allocator_position)
                ),
                slot_summary=slot_summary,
                available_slots_before_cohort=(
                    "—"
                    if record.available_slots_before_cohort is None
                    else str(record.available_slots_before_cohort)
                ),
                held_positions=held_positions,
                pending_buy_reservations=pending_buy_reservations,
                prior_cohort_admissions=prior_cohort_admissions,
                sell_releases=sell_releases,
                reservation_summary=(
                    f"Earlier pending BUY reservations: {pending_base} {base_currency}; "
                    f"this candidate: {allocation} {base_currency}."
                ),
                reason_code=record.reason_code or "—",
                explanation=tuple(explanation),
                event_sequence=record.event_sequence,
            )
        )
    return CandidateAuditViewV1(
        recorded=summary.recorded,
        candidate_count=summary.candidate_count,
        priority_recorded=summary.priority_recorded,
        priority_missing=summary.priority_missing,
        explanation_recorded=summary.explanation_recorded,
        explanation_missing=summary.explanation_missing,
        preflight_rejected=summary.preflight_rejected,
        full_book_rejected=summary.full_book_rejected,
        competition_rejected=summary.competition_rejected,
        filled=summary.filled,
        fill_rejected=summary.fill_rejected,
        page=page.page,
        total_pages=page.total_pages,
        rows=tuple(rows),
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
    "UNRESOLVED_SECURITY_LABEL",
    "resolve_security_label",
    "MetricsViewV1",
    "PerformanceSummaryViewV1",
    "TradeLogRowV1",
    "TradeLogViewV1",
    "ProvenanceEntryViewV1",
    "ProvenanceViewV1",
    "NoteViewV1",
    "InitialBasketRowV1",
    "InitialBasketViewV1",
    "metrics_view",
    "equity_curve_payload",
    "comparison_equity_payload",
    "multi_comparison_equity_payload",
    "performance_summary_view",
    "trade_log_view",
    "provenance_view",
    "note_view",
    "initial_basket_view",
]
