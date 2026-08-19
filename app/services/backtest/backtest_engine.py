"""Deterministic Backtest simulation engine (Story 2.4, AD-3/AD-6/AD-10/
AD-12/AD-13/AD-20).

``run_simulation`` replays one pinned :class:`RunInputManifestV1` through a
simulated, isolated portfolio: it iterates the manifest's canonical session
timeline, calls a resolved ``StrategyProtocolV1`` implementation through
bounded ``MarketViewV1``/``PortfolioView`` objects, applies pinned corporate
actions and FX exactly once per session via the existing
``corporate_actions``/``currency`` policy helpers, executes SELL-before-BUY
next-session-open fills with integer-floor sizing, and emits one
deterministic :class:`SimulationOutputV1` (Trade Log events + Equity Curve +
final open-position marks).

This module is deliberately pure and in-memory. It never imports the live
trading agent (``app.agents.trader.trader_agent``), any other module under
``app.agents``, ``trades_repo``, ``cash_balances_repo``,
``position_state_repo``, any broker/order builder, ``BacktestRepository``,
or ``HistoricalPriceRepository`` -- it consumes only a
:class:`RunInputManifestV1`, a caller-supplied ``MarketView`` factory, a
resolved ``StrategyProtocolV1`` implementation, plain pre-resolved evidence
(:class:`SecurityMarketDataV1`/``StoredHistoricalEvidence``), and immutable
policy constants. Callers (a future Story 2.5/2.6 worker, or a test) own
resolving evidence through the real repositories and handing this module
plain data -- exactly the same seam ``MarketView`` itself is constructed
through.

Design notes
------------
``MarketViewV1.price_history`` intentionally exposes only the bounded
*split-continuous* plane (Story 2.3, AD-6) -- the one plane a Strategy may
see. Fills and valuation must never use that plane or any provider-native/
adjusted close; they use only the exact *as-traded* plane
(``HistoricalMarketPlanes.as_traded()``). Because the as-traded plane is not
reachable through ``MarketViewV1``, this engine builds its own
``HistoricalMarketPlanes`` directly from the same pinned evidence a caller
already resolved for ``MarketView`` (via :class:`SecurityMarketDataV1`),
entirely separate from the Strategy-facing view.

A security's MIC trading calendar is derived from its pinned evidence's own
``exchange_timezone`` (``America/New_York`` -> XNYS, ``Europe/London`` ->
XLON) -- the same mapping already used by
``historical_initialization_engine.py``/``bau_capture_coordinator.py`` --
rather than inventing a new manifest field for it.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.corporate_actions import (
    PositionState,
    apply_dividend_cash,
    apply_split,
)
from app.services.backtest.currency import CurrencyPolicyError, convert_to_base
from app.services.backtest.market_planes import (
    AsTradedRow,
    CorporateAction,
    HistoricalMarketPlanes,
    MarketDataPolicyError,
    deterministic_decimal_context,
    quantize_eight,
)
from app.services.backtest.run_input_manifest import RunInputManifestV1
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    PositionSummaryV1,
    Signal,
    SignalSide,
    StrategyParameters,
    StrategyProtocolError,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
)
from app.services.backtest.trading_calendar import TradingCalendar

# ---------------------------------------------------------------------------
# Fatal errors
# ---------------------------------------------------------------------------


class SimulationErrorCode(StrEnum):
    """Engine-specific stable fatal-failure codes.

    Kept deliberately small: wherever an existing policy module already
    owns a stable code for a failure category (``MarketDataPolicyError``/
    ``CurrencyPolicyError``'s ``"integrity_error"``/``"unsupported_
    corporate_action"``/``"unsupported_quote_unit"``/``"fx_missing"``/
    ``"fx_ambiguous"``/``"fx_stale"``/``"unsupported_currency"``, or
    ``StrategyProtocolErrorCode``'s protocol-container codes),
    :class:`SimulationError` reuses that exact code string unmodified
    rather than remapping it -- there is exactly one stable vocabulary per
    failure origin, never a second translation layer.
    """

    MISSING_PINNED_EVIDENCE = "missing_pinned_evidence"
    MISSING_REQUIRED_OPEN = "missing_required_open"
    MISSING_REQUIRED_CLOSE = "missing_required_close"
    UNSUPPORTED_EXCHANGE_TIMEZONE = "unsupported_exchange_timezone"
    INVALID_STRATEGY_IMPLEMENTATION = "invalid_strategy_implementation"
    INVARIANT_VIOLATION = "invariant_violation"


class SimulationError(Exception):
    """One typed, inspectable fatal simulation failure with session/month
    context (AC 6).

    Raised for an unsupported corporate action, ambiguous/missing/stale
    required FX, tampered/missing pinned evidence, arithmetic failure, or
    an impossible portfolio invariant. Never raised for an ordinary
    business-rule rejection of one signal/fill -- those are recorded as a
    :class:`SkippedSignalEventV1` and processing continues.
    """

    def __init__(self, *, code: str, session: date, message: str) -> None:
        self.code = code
        self.session = session
        self.month = f"{session.year:04d}-{session.month:02d}"
        super().__init__(message)


def _fatal(code: str, session: date, message: str) -> SimulationError:
    return SimulationError(code=code, session=session, message=message)


# ---------------------------------------------------------------------------
# Skipped-event reason codes
# ---------------------------------------------------------------------------


class SkipReasonCode(StrEnum):
    """Stable, machine-readable reasons a signal/fill was skipped rather
    than executed -- business-rule rejections, never fatal (AC 2/3/4)."""

    DUPLICATE_SIGNAL = "duplicate_signal"
    SIGNAL_SESSION_MISMATCH = "signal_session_mismatch"
    INELIGIBLE_SECURITY = "ineligible_security"
    POSITION_CONFLICT = "position_conflict"
    INSUFFICIENT_CASH = "insufficient_cash"
    POSITION_SIZE_ZERO = "position_size_zero"
    FILL_BEYOND_END = "fill_beyond_end"


# ---------------------------------------------------------------------------
# Trade Log event / Equity Curve / output models
# ---------------------------------------------------------------------------


class _EngineModel(BaseModel):
    """Frozen, strict, extra-forbidding base, matching ``_StrategyModel``/
    ``_RunInputModel``'s established immutability convention."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class SkippedSignalEventV1(_EngineModel):
    """One signal that was validated but not scheduled/executed."""

    kind: Literal["skipped_signal"] = "skipped_signal"
    security_id: str = Field(min_length=1)
    side: SignalSide
    signal_session: date
    rule_id: str = Field(min_length=1)
    reason: SkipReasonCode
    detail: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class EntryFillEventV1(_EngineModel):
    """One executed BUY fill opening a position."""

    kind: Literal["entry_fill"] = "entry_fill"
    security_id: str = Field(min_length=1)
    signal_session: date
    fill_session: date
    rule_id: str = Field(min_length=1)
    shares: int = Field(gt=0)
    fill_price_native: Decimal
    fill_currency: str = Field(pattern=r"^[A-Z]{3}$")
    fill_quote_unit: str = Field(min_length=1)
    cost_base: Decimal
    fx_rate: Decimal | None = None
    fx_session: date | None = None
    fx_revision: str | None = None
    sequence: int = Field(ge=1)


class ExitFillEventV1(_EngineModel):
    """One executed SELL fill fully closing a position (V1's full-exit
    model -- never a partial close)."""

    kind: Literal["exit_fill"] = "exit_fill"
    security_id: str = Field(min_length=1)
    signal_session: date
    fill_session: date
    rule_id: str = Field(min_length=1)
    shares: int = Field(gt=0)
    fill_price_native: Decimal
    fill_currency: str = Field(pattern=r"^[A-Z]{3}$")
    fill_quote_unit: str = Field(min_length=1)
    proceeds_base: Decimal
    cost_basis_base: Decimal
    realized_pnl_base: Decimal
    fx_rate: Decimal | None = None
    fx_session: date | None = None
    fx_revision: str | None = None
    sequence: int = Field(ge=1)


class SplitAppliedEventV1(_EngineModel):
    """One pinned split applied exactly once to a position carried into
    its effective session, before that session's signals/fills."""

    kind: Literal["split_applied"] = "split_applied"
    security_id: str = Field(min_length=1)
    session: date
    ratio: Decimal
    shares_before: Decimal
    shares_after: Decimal
    evidence_revision: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class DividendAppliedEventV1(_EngineModel):
    """One pinned dividend credited exactly once to a position carried
    into its effective session, before that session's signals/fills."""

    kind: Literal["dividend_applied"] = "dividend_applied"
    security_id: str = Field(min_length=1)
    session: date
    per_share_amount: Decimal
    shares_carried: Decimal
    cash_credit_native: Decimal
    cash_credit_base: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    quote_unit: str = Field(min_length=1)
    evidence_revision: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class OpenPositionMarkEventV1(_EngineModel):
    """One open position marked at the normalized final session's exact
    as-traded close and bounded FX -- never a fabricated exit, never
    counted in Win Rate, but feeding the final Equity Curve/Total
    Return/Drawdown point (AC 7)."""

    kind: Literal["open_position_mark"] = "open_position_mark"
    security_id: str = Field(min_length=1)
    session: date
    shares: int = Field(gt=0)
    mark_price_native: Decimal
    market_value_base: Decimal
    cost_basis_base: Decimal
    unrealized_pnl_base: Decimal
    sequence: int = Field(ge=1)


#: The full closed set of Trade Log work events.
TradeLogEvent = (
    EntryFillEventV1
    | ExitFillEventV1
    | SkippedSignalEventV1
    | SplitAppliedEventV1
    | DividendAppliedEventV1
    | OpenPositionMarkEventV1
)


class EquityCurvePointV1(_EngineModel):
    """One canonical in-range union-calendar valuation date's total
    simulated equity, in base currency, no presentation rounding."""

    session: date
    cash_base: Decimal
    positions_value_base: Decimal
    total_equity_base: Decimal
    sequence: int = Field(ge=1)


class SimulationOutputV1(_EngineModel):
    """One deterministic, complete replay's Trade Log + Equity Curve +
    final open-position marks -- never emitted partially (AC 6)."""

    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    events: tuple[TradeLogEvent, ...]
    equity_curve: tuple[EquityCurvePointV1, ...]
    final_cash_base: Decimal
    final_open_positions: tuple[OpenPositionMarkEventV1, ...]


# ---------------------------------------------------------------------------
# Internal (non-output) engine value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingOrderV1:
    """One signal's pinned request, scheduled for a future fill.

    At most one pending order may exist per security at a time (V1's
    one-long-position model) -- a second signal for the same security
    while one is already pending is rejected as ``position_conflict``.
    """

    security_id: str
    side: SignalSide
    signal_session: date
    fill_session: date
    rule_id: str
    requested_shares: int


@dataclass(frozen=True)
class SecurityMarketDataV1:
    """One security's exact pinned price/action evidence for the whole Run.

    Deliberately plain data: the caller (never this engine) resolves this
    once via ``HistoricalPriceRepository.get(...)`` for each of the
    manifest's pinned ``PinnedSecurityEvidenceV1.price_revision`` entries.
    Carrying only the already-resolved evidence keeps the engine free of
    any repository import while still letting it build the same
    ``HistoricalMarketPlanes`` ``MarketView`` builds internally -- this
    engine reads only the as-traded plane from it, never the
    split-continuous plane exposed to a Strategy.
    """

    security_id: str
    price_evidence: StoredHistoricalEvidence


#: A caller-supplied, session-bound ``MarketViewV1`` constructor -- the
#: same "MarketView factory" the Code Map/Design Notes describe. The
#: caller (test or a future Story 2.6 worker) closes over whatever
#: repositories it needs; this engine only ever calls it with a session
#: date and receives back a bounded, no-look-ahead view.
MarketViewFactory = Callable[[date], MarketViewV1]


class SessionBatchSink(Protocol):
    """Receive one session's complete, already-committed work output.

    Called exactly once per successfully processed session, only after
    that session's actions/fills/valuation/signals have all committed --
    never for a session that raised :class:`SimulationError` partway
    through, so an implementation never observes a partial batch. Story
    2.5 supplies a SQLite staging sink; this module ships only the default
    in-memory implementation below.
    """

    def publish_session(
        self,
        *,
        session: date,
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
    ) -> None: ...


@dataclass
class InMemorySessionBatchSink:
    """Default in-memory :class:`SessionBatchSink` -- accumulates every
    published session, for tests and Story 2.5's first integration."""

    events: list[TradeLogEvent] = field(default_factory=list)
    equity_curve: list[EquityCurvePointV1] = field(default_factory=list)

    def publish_session(
        self,
        *,
        session: date,
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
    ) -> None:
        del session  # already carried by equity_point.session
        self.events.extend(events)
        self.equity_curve.append(equity_point)


class MonthBoundaryObserver(Protocol):
    """A hook invoked once per new calendar month the session loop
    enters. Story 2.6 supplies a real cancellation-checking observer here
    by swapping this in -- never by subclassing the engine."""

    def on_month_boundary(self, *, month: str) -> None: ...


class NoOpMonthBoundaryObserver:
    """The default :class:`MonthBoundaryObserver` -- does nothing."""

    def on_month_boundary(self, *, month: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------

#: SELL executes before BUY for the same session/security, matching
#: ``strategy_protocol.Signal``'s own execution convention -- kept as a
#: private copy here because the engine's cross-signal *combination*
#: order (side rank ahead of security ID) is deliberately different from
#: ``Signal.sort_key``'s own per-call field order (security ID ahead of
#: side rank), see ``_engine_signal_sort_key`` below.
_SIDE_RANK: dict[SignalSide, int] = {SignalSide.SELL: 0, SignalSide.BUY: 1}

#: Maps a pinned security's evidence ``exchange_timezone`` to the MIC
#: whose ``TradingCalendar`` session table governs its fills/marks --
#: mirrors ``historical_initialization_engine.py``'s/
#: ``bau_capture_coordinator.py``'s existing MIC-to-timezone mapping,
#: inverted. XNAS and XNYS already share one XNYS calendar table, so no
#: distinct XNAS entry is needed.
_EXCHANGE_TIMEZONE_TO_MIC: dict[str, str] = {
    "America/New_York": "XNYS",
    "Europe/London": "XLON",
}


def _mic_for_exchange_timezone(exchange_timezone: str, *, session: date) -> str:
    """Resolve a MIC during Run initialization -- ``session`` is the Run's
    own start date, not a simulated session, since MIC resolution happens
    once per pinned security before the session loop begins."""
    try:
        return _EXCHANGE_TIMEZONE_TO_MIC[exchange_timezone]
    except KeyError as exc:
        raise _fatal(
            SimulationErrorCode.UNSUPPORTED_EXCHANGE_TIMEZONE,
            session,
            f"Unsupported exchange timezone at Run initialization: "
            f"{exchange_timezone!r}",
        ) from exc


def _month_range_dates(start_month: str, end_month: str) -> tuple[date, date]:
    """Return ``[start_month, end_month]``'s inclusive/exclusive calendar
    date bounds -- ``(first day of start_month, first day after
    end_month)`` -- independent of any one MIC's own session calendar."""
    start_year, start_number = (int(part) for part in start_month.split("-"))
    end_year, end_number = (int(part) for part in end_month.split("-"))
    start_date = date(start_year, start_number, 1)
    if end_number == 12:
        end_exclusive = date(end_year + 1, 1, 1)
    else:
        end_exclusive = date(end_year, end_number + 1, 1)
    return start_date, end_exclusive


def _engine_signal_sort_key(signal: Signal) -> tuple[date, int, str, str]:
    """The engine's own cross-security combination order: signal session,
    then SELL before BUY, then security ID, then rule ID.

    Deliberately distinct from ``Signal.sort_key`` (which orders security
    ID ahead of side) -- combining a same-session SELL for security "ZZZ"
    with a BUY for security "AAA" using ``Signal.sort_key`` alone would
    process the BUY first purely because "AAA" < "ZZZ", silently
    violating the SELL-before-BUY cash-reuse guarantee. This key restores
    side rank as the dominant tiebreak after session, matching the
    Determinism Guardrails' explicit ``(signal_session, side_rank,
    security_id, deterministic_sort_key)`` materialization order.
    """
    return (signal.session, _SIDE_RANK[signal.side], signal.security_id, signal.rule_id)


def _fill_sort_key(order: PendingOrderV1) -> tuple[date, int, str, date, str]:
    """Fills materialize in ``(fill_session, side_rank, security_id,
    signal_session, deterministic_sort_key)`` order per the Determinism
    Guardrails -- distinct from signal-scheduling order above."""
    return (
        order.fill_session,
        _SIDE_RANK[order.side],
        order.security_id,
        order.signal_session,
        order.rule_id,
    )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class _Engine:
    """Mutable, single-use replay engine. Construct and call ``.run()``
    exactly once via :func:`run_simulation`."""

    def __init__(
        self,
        *,
        manifest: RunInputManifestV1,
        strategy: StrategyProtocolV1,
        market_view_factory: MarketViewFactory,
        security_market_data: tuple[SecurityMarketDataV1, ...],
        fx_evidence: StoredHistoricalEvidence | None,
        sink: SessionBatchSink,
        month_observer: MonthBoundaryObserver,
    ) -> None:
        self.manifest = manifest
        self.strategy = strategy
        self.market_view_factory = market_view_factory
        self.fx_evidence = fx_evidence
        self.sink = sink
        self.month_observer = month_observer
        self.calendar = TradingCalendar()
        self.manifest_parameters = cast(StrategyParameters, manifest.parameters)

        self.start_date, self.end_exclusive = _month_range_dates(
            manifest.start_month, manifest.end_month
        )

        if not isinstance(strategy, StrategyProtocolV1):
            raise _fatal(
                SimulationErrorCode.INVALID_STRATEGY_IMPLEMENTATION,
                self.start_date,
                "strategy does not satisfy StrategyProtocolV1",
            )

        if len({item.security_id for item in security_market_data}) != len(
            security_market_data
        ):
            raise _fatal(
                SimulationErrorCode.MISSING_PINNED_EVIDENCE,
                self.start_date,
                "security_market_data must not repeat a security_id",
            )
        pinned = {item.security_id: item for item in manifest.securities}
        supplied = {item.security_id: item for item in security_market_data}
        if set(pinned) != set(supplied):
            raise _fatal(
                SimulationErrorCode.MISSING_PINNED_EVIDENCE,
                self.start_date,
                "security_market_data does not match the manifest's pinned securities",
            )
        for security_id, item in supplied.items():
            if item.price_evidence.data_revision != pinned[security_id].price_revision:
                raise _fatal(
                    SimulationErrorCode.MISSING_PINNED_EVIDENCE,
                    self.start_date,
                    f"{security_id!r} price evidence does not match its pinned revision",
                )
            pinned_fx_revision = pinned[security_id].fx_revision
            if pinned_fx_revision is not None:
                if fx_evidence is None:
                    raise _fatal(
                        SimulationErrorCode.MISSING_PINNED_EVIDENCE,
                        self.start_date,
                        f"{security_id!r} pins an fx_revision but no fx_evidence "
                        "was supplied",
                    )
                if fx_evidence.data_revision != pinned_fx_revision:
                    raise _fatal(
                        SimulationErrorCode.MISSING_PINNED_EVIDENCE,
                        self.start_date,
                        f"{security_id!r} fx evidence does not match its pinned "
                        "fx_revision",
                    )

        self.planes: dict[str, HistoricalMarketPlanes] = {}
        self.as_traded_by_session: dict[str, dict[date, AsTradedRow]] = {}
        self.session_index: dict[str, tuple[date, ...]] = {}
        self.mic_by_security: dict[str, str] = {}
        self.actions_by_security: dict[
            str, dict[date, tuple[CorporateAction, ...]]
        ] = {}
        try:
            for security_id, item in supplied.items():
                plane = HistoricalMarketPlanes.from_evidence(item.price_evidence)
                self.planes[security_id] = plane
                rows = plane.as_traded()
                by_session = {row.session: row for row in rows}
                self.as_traded_by_session[security_id] = by_session
                self.session_index[security_id] = tuple(sorted(by_session))
                self.mic_by_security[security_id] = _mic_for_exchange_timezone(
                    plane.exchange_timezone, session=self.start_date
                )
                actions = plane.actions_as_of(plane.end - timedelta(days=1))
                bucketed: dict[date, list[CorporateAction]] = {}
                for action in actions:
                    bucketed.setdefault(action.session, []).append(action)
                self.actions_by_security[security_id] = {
                    session: tuple(sorted(items, key=lambda a: a.action_type))
                    for session, items in bucketed.items()
                }
        except MarketDataPolicyError as exc:
            raise _fatal(exc.code, self.start_date, exc.detail) from exc

        mics = sorted(set(self.mic_by_security.values()))
        union: set[date] = set()
        for mic in mics:
            union.update(
                self.calendar.sessions_in_range(
                    mic, self.start_date, self.end_exclusive
                )
            )
        self.union_sessions: tuple[date, ...] = tuple(sorted(union))
        if not self.union_sessions:
            raise _fatal(
                SimulationErrorCode.INVARIANT_VIOLATION,
                self.start_date,
                "normalized range contains no trading sessions",
            )

        self.cash = quantize_eight(manifest.starting_capital)
        self.positions: dict[str, PositionState] = {}
        self.pending: dict[str, PendingOrderV1] = {}
        self.applied_action_keys: set[str] = set()
        self._sequence = 0
        self.output_events: list[TradeLogEvent] = []
        self.equity_curve: list[EquityCurvePointV1] = []
        self.final_open_positions: tuple[OpenPositionMarkEventV1, ...] = ()

    # -- sequencing -----------------------------------------------------

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence

    # -- calendar/price lookups ------------------------------------------

    def _next_mic_session(self, security_id: str, signal_session: date) -> date | None:
        mic = self.mic_by_security[security_id]
        next_day = signal_session + timedelta(days=1)
        if next_day >= self.end_exclusive:
            return None
        candidates = self.calendar.sessions_in_range(mic, next_day, self.end_exclusive)
        return candidates[0] if candidates else None

    def _latest_row_on_or_before(
        self, security_id: str, as_of: date
    ) -> AsTradedRow | None:
        sessions = self.session_index[security_id]
        index = bisect_right(sessions, as_of) - 1
        if index < 0:
            return None
        return self.as_traded_by_session[security_id][sessions[index]]

    def _convert(
        self,
        native_value: Decimal,
        plane: HistoricalMarketPlanes,
        *,
        valuation_session: date,
    ):
        try:
            return convert_to_base(
                value=native_value,
                quote_currency=plane.currency,
                quote_unit=plane.quote_unit,
                base_currency=self.manifest.base_currency,
                valuation_session=valuation_session,
                completed_fx_through=valuation_session,
                fx_evidence=self.fx_evidence,
            )
        except CurrencyPolicyError as exc:
            raise _fatal(exc.code, valuation_session, exc.detail) from exc

    # -- portfolio view construction -------------------------------------

    def _portfolio_view(self, as_of_session: date) -> PortfolioView:
        positions = tuple(
            PositionSummaryV1(
                security_id=security_id,
                quantity=position.shares,
                average_cost=position.per_share_basis,
            )
            for security_id, position in sorted(self.positions.items())
        )
        return PortfolioView(
            as_of_session=as_of_session,
            base_currency=self.manifest.base_currency,
            cash=self.cash,
            positions=positions,
            volatility_observations=(),
        )

    # -- corporate actions ------------------------------------------------

    def _apply_actions(
        self, session: date, session_events: list[TradeLogEvent]
    ) -> None:
        for security_id in sorted(self.positions):
            actions = self.actions_by_security.get(security_id, {}).get(session, ())
            for action in actions:
                action_key = (
                    f"{security_id}:{action.evidence_revision}:"
                    f"{action.session.isoformat()}:{action.action_type}"
                )
                if action_key in self.applied_action_keys:
                    continue
                self.applied_action_keys.add(action_key)
                self._apply_one_action(security_id, action, session, session_events)

    def _apply_one_action(
        self,
        security_id: str,
        action: CorporateAction,
        session: date,
        session_events: list[TradeLogEvent],
    ) -> None:
        plane = self.planes[security_id]
        position = self.positions[security_id]
        try:
            if action.action_type == "split":
                result = apply_split(
                    position,
                    action,
                    quote_currency=plane.currency,
                    quote_unit=plane.quote_unit,
                )
                self.positions[security_id] = result.position
                session_events.append(
                    SplitAppliedEventV1(
                        security_id=security_id,
                        session=session,
                        ratio=action.value,
                        shares_before=position.shares,
                        shares_after=result.position.shares,
                        evidence_revision=action.evidence_revision,
                        policy_version=result.event.policy_version,
                        sequence=self._next_seq(),
                    )
                )
            else:
                credit = apply_dividend_cash(
                    shares_carried_into_open=position.shares,
                    action=action,
                    quote_currency=plane.currency,
                    quote_unit=plane.quote_unit,
                )
                conversion = self._convert(
                    credit.cash_credit, plane, valuation_session=session
                )
                self.cash = quantize_eight(self.cash + conversion.base_amount)
                session_events.append(
                    DividendAppliedEventV1(
                        security_id=security_id,
                        session=session,
                        per_share_amount=action.value,
                        shares_carried=position.shares,
                        cash_credit_native=credit.cash_credit,
                        cash_credit_base=conversion.base_amount,
                        currency=plane.currency,
                        quote_unit=plane.quote_unit,
                        evidence_revision=action.evidence_revision,
                        policy_version=credit.event.policy_version,
                        sequence=self._next_seq(),
                    )
                )
        except MarketDataPolicyError as exc:
            raise _fatal(exc.code, session, exc.detail) from exc

    # -- fills --------------------------------------------------------------

    def _execute_fills(
        self, session: date, session_events: list[TradeLogEvent]
    ) -> None:
        due = [
            order for order in self.pending.values() if order.fill_session == session
        ]
        due.sort(key=_fill_sort_key)
        for order in due:
            del self.pending[order.security_id]
            session_events.append(self._execute_fill(order, session))

    def _execute_fill(self, order: PendingOrderV1, session: date) -> TradeLogEvent:
        row = self.as_traded_by_session[order.security_id].get(session)
        if row is None:
            raise _fatal(
                SimulationErrorCode.MISSING_REQUIRED_OPEN,
                session,
                f"{order.security_id!r} has no as-traded open on {session.isoformat()}",
            )
        if row.open <= 0:
            raise _fatal(
                SimulationErrorCode.INVARIANT_VIOLATION,
                session,
                f"{order.security_id!r} as-traded open on {session.isoformat()} "
                "is not a positive price",
            )
        plane = self.planes[order.security_id]
        if order.side is SignalSide.BUY:
            return self._execute_buy(order, plane, row.open, session)
        return self._execute_sell(order, plane, row.open, session)

    def _execute_buy(
        self,
        order: PendingOrderV1,
        plane: HistoricalMarketPlanes,
        price_native: Decimal,
        session: date,
    ) -> TradeLogEvent:
        if order.security_id in self.positions:
            return self._skip_order(
                order,
                session,
                SkipReasonCode.POSITION_CONFLICT,
                "position already open at fill time",
            )
        try:
            with deterministic_decimal_context():
                native_cost = price_native * Decimal(order.requested_shares)
        except DecimalException as exc:
            raise _fatal(
                "integrity_error", session, "fill cost arithmetic failed"
            ) from exc
        conversion = self._convert(native_cost, plane, valuation_session=session)
        cost_base = conversion.base_amount
        if cost_base > self.cash:
            return self._skip_order(
                order,
                session,
                SkipReasonCode.INSUFFICIENT_CASH,
                "insufficient simulated cash at fill time",
            )
        self.cash = quantize_eight(self.cash - cost_base)
        if self.cash < 0:
            raise _fatal(
                SimulationErrorCode.INVARIANT_VIOLATION,
                session,
                "cash balance went negative",
            )
        try:
            with deterministic_decimal_context():
                per_share_basis = quantize_eight(
                    cost_base / Decimal(order.requested_shares)
                )
        except DecimalException as exc:
            raise _fatal(
                "integrity_error", session, "cost-basis arithmetic failed"
            ) from exc
        self.positions[order.security_id] = PositionState(
            shares=Decimal(order.requested_shares), per_share_basis=per_share_basis
        )
        return EntryFillEventV1(
            security_id=order.security_id,
            signal_session=order.signal_session,
            fill_session=session,
            rule_id=order.rule_id,
            shares=order.requested_shares,
            fill_price_native=price_native,
            fill_currency=plane.currency,
            fill_quote_unit=plane.quote_unit,
            cost_base=cost_base,
            fx_rate=conversion.fx_rate,
            fx_session=conversion.fx_session,
            fx_revision=conversion.fx_revision,
            sequence=self._next_seq(),
        )

    def _execute_sell(
        self,
        order: PendingOrderV1,
        plane: HistoricalMarketPlanes,
        price_native: Decimal,
        session: date,
    ) -> TradeLogEvent:
        position = self.positions.get(order.security_id)
        if position is None:
            return self._skip_order(
                order,
                session,
                SkipReasonCode.POSITION_CONFLICT,
                "no open position to sell at fill time",
            )
        # V1 is full-exit-only: sell whatever is currently held, not the
        # (possibly stale) quantity snapshotted when the signal was
        # scheduled -- a split effective on this fill session already
        # adjusted ``position.shares`` in ``_apply_actions`` above, and the
        # order's original ``requested_shares`` is not re-derived for it.
        fill_shares = int(position.shares)
        try:
            with deterministic_decimal_context():
                native_proceeds = price_native * Decimal(fill_shares)
        except DecimalException as exc:
            raise _fatal(
                "integrity_error", session, "fill proceeds arithmetic failed"
            ) from exc
        conversion = self._convert(native_proceeds, plane, valuation_session=session)
        proceeds_base = conversion.base_amount
        try:
            with deterministic_decimal_context():
                cost_basis_base = quantize_eight(
                    position.per_share_basis * position.shares
                )
                realized_pnl_base = quantize_eight(proceeds_base - cost_basis_base)
        except DecimalException as exc:
            raise _fatal(
                "integrity_error", session, "realized P&L arithmetic failed"
            ) from exc
        self.cash = quantize_eight(self.cash + proceeds_base)
        del self.positions[order.security_id]
        return ExitFillEventV1(
            security_id=order.security_id,
            signal_session=order.signal_session,
            fill_session=session,
            rule_id=order.rule_id,
            shares=fill_shares,
            fill_price_native=price_native,
            fill_currency=plane.currency,
            fill_quote_unit=plane.quote_unit,
            proceeds_base=proceeds_base,
            cost_basis_base=cost_basis_base,
            realized_pnl_base=realized_pnl_base,
            fx_rate=conversion.fx_rate,
            fx_session=conversion.fx_session,
            fx_revision=conversion.fx_revision,
            sequence=self._next_seq(),
        )

    def _skip_order(
        self, order: PendingOrderV1, session: date, reason: SkipReasonCode, detail: str
    ) -> SkippedSignalEventV1:
        return SkippedSignalEventV1(
            security_id=order.security_id,
            side=order.side,
            signal_session=order.signal_session,
            rule_id=order.rule_id,
            reason=reason,
            detail=detail,
            sequence=self._next_seq(),
        )

    # -- valuation ------------------------------------------------------

    def _value_state(self, session: date) -> EquityCurvePointV1:
        positions_value = Decimal(0)
        for security_id, position in sorted(self.positions.items()):
            positions_value = quantize_eight(
                positions_value
                + self._mark_position_value(security_id, position, session)
            )
        total = quantize_eight(self.cash + positions_value)
        return EquityCurvePointV1(
            session=session,
            cash_base=self.cash,
            positions_value_base=positions_value,
            total_equity_base=total,
            sequence=self._next_seq(),
        )

    def _mark_position_value(
        self, security_id: str, position: PositionState, session: date
    ) -> Decimal:
        row = self._latest_row_on_or_before(security_id, session)
        if row is None:
            raise _fatal(
                SimulationErrorCode.MISSING_REQUIRED_CLOSE,
                session,
                f"no as-traded close is available to value {security_id!r} "
                f"on or before {session.isoformat()}",
            )
        plane = self.planes[security_id]
        try:
            with deterministic_decimal_context():
                native_value = row.close * position.shares
        except DecimalException as exc:
            raise _fatal(
                "integrity_error", session, "position valuation arithmetic failed"
            ) from exc
        conversion = self._convert(native_value, plane, valuation_session=session)
        return conversion.base_amount

    # -- signal processing ------------------------------------------------

    def _process_signals(
        self, session: date, session_events: list[TradeLogEvent]
    ) -> None:
        view = self.market_view_factory(session)
        if view.as_of_session != session:
            raise _fatal(
                SimulationErrorCode.INVARIANT_VIOLATION,
                session,
                "market view factory returned a view bound to the wrong session",
            )
        portfolio = self._portfolio_view(session)
        try:
            exits = validate_exit_signals(
                self.strategy.exit_signals(view, portfolio, self.manifest_parameters)
            )
            entries = validate_entry_signals(
                self.strategy.entry_signals(view, self.manifest_parameters)
            )
            combined = sorted(exits + entries, key=_engine_signal_sort_key)
            seen: set[tuple[date, str, SignalSide, str]] = set()
            for signal in combined:
                identity = (
                    signal.session,
                    signal.security_id,
                    signal.side,
                    signal.rule_id,
                )
                if identity in seen:
                    session_events.append(
                        self._skip_signal(
                            signal,
                            session,
                            SkipReasonCode.DUPLICATE_SIGNAL,
                            "duplicate signal instruction",
                        )
                    )
                    continue
                seen.add(identity)
                skip = self._schedule_signal(signal, session, view, portfolio)
                if skip is not None:
                    session_events.append(skip)
        except StrategyProtocolError as exc:
            raise _fatal(exc.code, session, str(exc)) from exc

    def _schedule_signal(
        self,
        signal: Signal,
        session: date,
        view: MarketViewV1,
        portfolio: PortfolioView,
    ) -> SkippedSignalEventV1 | None:
        if signal.session != session:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.SIGNAL_SESSION_MISMATCH,
                "signal session does not match the invoking session",
            )
        if signal.security_id not in self.planes:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.INELIGIBLE_SECURITY,
                "security is not pinned for this Run",
            )
        if signal.security_id in self.pending:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.POSITION_CONFLICT,
                "an order is already pending for this security",
            )
        held = self.positions.get(signal.security_id)
        if signal.side is SignalSide.BUY and held is not None:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.POSITION_CONFLICT,
                "position already open -- V1 forbids pyramiding a duplicate entry",
            )
        if signal.side is SignalSide.SELL and held is None:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.POSITION_CONFLICT,
                "no open position to sell",
            )
        requested = validate_position_size(
            self.strategy.position_size(
                signal, view, portfolio, self.manifest_parameters
            )
        )
        if requested == 0:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.POSITION_SIZE_ZERO,
                "position size is zero",
            )
        if (
            signal.side is SignalSide.SELL
            and held is not None
            and Decimal(requested) != held.shares
        ):
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.POSITION_CONFLICT,
                "sell quantity does not match the held position -- V1 forbids a partial exit",
            )
        fill_session = self._next_mic_session(signal.security_id, session)
        if fill_session is None:
            return self._skip_signal(
                signal,
                session,
                SkipReasonCode.FILL_BEYOND_END,
                "no fillable session remains before the normalized end",
            )
        self.pending[signal.security_id] = PendingOrderV1(
            security_id=signal.security_id,
            side=signal.side,
            signal_session=session,
            fill_session=fill_session,
            rule_id=signal.rule_id,
            requested_shares=requested,
        )
        return None

    def _skip_signal(
        self, signal: Signal, session: date, reason: SkipReasonCode, detail: str
    ) -> SkippedSignalEventV1:
        return SkippedSignalEventV1(
            security_id=signal.security_id,
            side=signal.side,
            signal_session=session,
            rule_id=signal.rule_id,
            reason=reason,
            detail=detail,
            sequence=self._next_seq(),
        )

    # -- final marks ------------------------------------------------------

    def _mark_final_positions(
        self, session: date
    ) -> tuple[OpenPositionMarkEventV1, ...]:
        marks: list[OpenPositionMarkEventV1] = []
        for security_id, position in sorted(self.positions.items()):
            row = self._latest_row_on_or_before(security_id, session)
            if row is None:
                raise _fatal(
                    SimulationErrorCode.MISSING_REQUIRED_CLOSE,
                    session,
                    f"no as-traded close is available to mark {security_id!r}",
                )
            plane = self.planes[security_id]
            try:
                with deterministic_decimal_context():
                    native_value = row.close * position.shares
                    cost_basis_base = quantize_eight(
                        position.per_share_basis * position.shares
                    )
            except DecimalException as exc:
                raise _fatal(
                    "integrity_error", session, "final mark arithmetic failed"
                ) from exc
            conversion = self._convert(native_value, plane, valuation_session=session)
            market_value_base = conversion.base_amount
            unrealized_pnl_base = quantize_eight(market_value_base - cost_basis_base)
            marks.append(
                OpenPositionMarkEventV1(
                    security_id=security_id,
                    session=session,
                    shares=int(position.shares),
                    mark_price_native=row.close,
                    market_value_base=market_value_base,
                    cost_basis_base=cost_basis_base,
                    unrealized_pnl_base=unrealized_pnl_base,
                    sequence=self._next_seq(),
                )
            )
        return tuple(marks)

    # -- session pipeline ---------------------------------------------------

    def _process_session(
        self, session: date, *, is_final: bool
    ) -> tuple[tuple[TradeLogEvent, ...], EquityCurvePointV1]:
        session_events: list[TradeLogEvent] = []
        self._apply_actions(session, session_events)
        self._execute_fills(session, session_events)
        equity_point = self._value_state(session)
        self._process_signals(session, session_events)
        if is_final:
            final_marks = self._mark_final_positions(session)
            session_events.extend(final_marks)
            self.final_open_positions = final_marks
        return tuple(session_events), equity_point

    def run(self) -> SimulationOutputV1:
        current_month: str | None = None
        last_index = len(self.union_sessions) - 1
        for index, session in enumerate(self.union_sessions):
            month = f"{session.year:04d}-{session.month:02d}"
            if month != current_month:
                current_month = month
                self.month_observer.on_month_boundary(month=month)
            session_events, equity_point = self._process_session(
                session, is_final=(index == last_index)
            )
            self.sink.publish_session(
                session=session, events=session_events, equity_point=equity_point
            )
            self.output_events.extend(session_events)
            self.equity_curve.append(equity_point)

        return SimulationOutputV1(
            manifest_digest=self.manifest.digest(),
            events=tuple(self.output_events),
            equity_curve=tuple(self.equity_curve),
            final_cash_base=self.cash,
            final_open_positions=self.final_open_positions,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_simulation(
    *,
    manifest: RunInputManifestV1,
    strategy: StrategyProtocolV1,
    market_view_factory: MarketViewFactory,
    security_market_data: tuple[SecurityMarketDataV1, ...],
    fx_evidence: StoredHistoricalEvidence | None = None,
    sink: SessionBatchSink | None = None,
    month_boundary_observer: MonthBoundaryObserver | None = None,
) -> SimulationOutputV1:
    """Deterministically replay ``manifest`` and return its complete
    :class:`SimulationOutputV1` (AC 1-7).

    Pure and in-memory: never imports or touches a repository, job, or
    live-portfolio path. ``market_view_factory`` and
    ``security_market_data``/``fx_evidence`` are the caller's
    responsibility to resolve (typically from ``HistoricalPriceRepository``
    for the same revisions ``manifest.securities`` pins) -- this function
    never re-resolves or substitutes evidence itself.

    Raises :class:`SimulationError` on any fatal failure (unsupported
    corporate action, ambiguous/missing/stale FX, missing/tampered pinned
    evidence, arithmetic failure, or an impossible portfolio invariant);
    no :class:`SimulationOutputV1` is ever returned for a Run that raised.
    """
    engine = _Engine(
        manifest=manifest,
        strategy=strategy,
        market_view_factory=market_view_factory,
        security_market_data=security_market_data,
        fx_evidence=fx_evidence,
        sink=sink if sink is not None else InMemorySessionBatchSink(),
        month_observer=(
            month_boundary_observer
            if month_boundary_observer is not None
            else NoOpMonthBoundaryObserver()
        ),
    )
    return engine.run()


__all__ = [
    "DividendAppliedEventV1",
    "EntryFillEventV1",
    "EquityCurvePointV1",
    "ExitFillEventV1",
    "InMemorySessionBatchSink",
    "MarketViewFactory",
    "MonthBoundaryObserver",
    "NoOpMonthBoundaryObserver",
    "OpenPositionMarkEventV1",
    "PendingOrderV1",
    "SecurityMarketDataV1",
    "SessionBatchSink",
    "SimulationError",
    "SimulationErrorCode",
    "SimulationOutputV1",
    "SkipReasonCode",
    "SkippedSignalEventV1",
    "SplitAppliedEventV1",
    "TradeLogEvent",
    "run_simulation",
]
