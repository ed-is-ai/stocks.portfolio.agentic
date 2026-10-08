"""Deterministic long-only Minervini VCP backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

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
    BaseCurrencyCloseHistoryViewV1,
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-minervini"
STRATEGY_API_VERSION = 1
_ENTRY_RULE = "minervini_vcp_breakout_v1"
_EXIT_RULE = "minervini_risk_exit_v1"
_UPGRADE_EXIT_RULE = "minervini_upgrade_exit_v1"
# Monthly scan states whose base is still intact.  A scan's ``Breakout``
# describes only its own snapshot session, so requiring it limited entries to
# breakouts that landed on the month-end snapshot (#31); the daily pivot,
# extension and volume gates trigger the breakout instead.
_ENTRY_SCAN_STATES = frozenset({"Pre-breakout", "Breakout", "Early-post-breakout"})


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


def _current_history(
    view: MarketViewV1,
    security_id: str,
    *,
    limit: int,
    columns: tuple[str, ...],
) -> Any | None:
    history = view.price_history(security_id, limit=limit, columns=columns)
    if history.empty or _session_date(history.index[-1]) != view.as_of_session:
        return None
    if not set(columns).issubset(history.columns):
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


def _integral_quantity(portfolio: PortfolioView, security_id: str) -> Decimal:
    held = _position(portfolio, security_id)
    if held is None or held.quantity <= 0:
        return Decimal(0)
    return held.quantity


class _EntryQualification(NamedTuple):
    """A qualifying VCP entry's score plus the evidence behind it."""

    score: int
    minimum_score: int
    scan_stage: str
    close: Decimal
    pivot: Decimal
    extension_limit: Decimal
    volume: Decimal
    required_volume: Decimal
    volume_multiplier: Decimal
    trend_score: Decimal
    minimum_trend_score: Decimal


class _MomentumReading(NamedTuple):
    value: Decimal | None
    missing_reason: str | None
    numerator_session: date | None
    denominator_session: date | None
    currency: str


def _momentum_reading(view: MarketViewV1, security_id: str) -> _MomentumReading:
    """Read the fixed 21-session return from 253 bounded Run-currency rows."""
    if not isinstance(view, BaseCurrencyCloseHistoryViewV1):
        return _MomentumReading(
            None, "base_currency_history_unavailable", None, None, "unavailable"
        )

    currency = view.base_currency
    history = view.base_currency_close_history(security_id, limit=253)
    if history is None or getattr(history, "empty", True):
        return _MomentumReading(
            None, "current_base_currency_close_unavailable", None, None, currency
        )
    sessions = [_session_date(value) for value in history.index]
    if any(session is None for session in sessions):
        raise ValueError("Base-currency close history has an invalid session index.")
    canonical_sessions = [session for session in sessions if session is not None]
    if canonical_sessions != sorted(set(canonical_sessions)):
        raise ValueError(
            "Base-currency close history sessions are unordered or duplicated."
        )
    if any(session > view.as_of_session for session in canonical_sessions):
        raise ValueError("Base-currency close history contains a future session.")
    if len(canonical_sessions) < 253:
        numerator = canonical_sessions[-22] if len(canonical_sessions) >= 22 else None
        return _MomentumReading(
            None, "insufficient_price_history", numerator, None, currency
        )
    rows = history.iloc[-253:]
    canonical_sessions = canonical_sessions[-253:]
    numerator_session = canonical_sessions[-22]
    denominator_session = canonical_sessions[-253]
    if canonical_sessions[-1] != view.as_of_session:
        return _MomentumReading(
            None,
            "current_base_currency_close_unavailable",
            numerator_session,
            denominator_session,
            currency,
        )
    try:
        numerator_row = rows.iloc[-22]
        denominator_row = rows.iloc[-253]
        numerator = _decimal(numerator_row["close"])
        denominator = _decimal(denominator_row["close"])
    except (KeyError, IndexError, TypeError):
        return _MomentumReading(
            None,
            "close_endpoint_unavailable",
            numerator_session,
            denominator_session,
            currency,
        )
    if numerator is None:
        reason = numerator_row.get("reason")
        return _MomentumReading(
            None,
            reason
            if reason in {"fx_missing", "fx_stale", "fx_outside_coverage"}
            else "invalid_momentum_endpoint",
            numerator_session,
            denominator_session,
            currency,
        )
    if numerator <= 0 or denominator is None or denominator <= 0:
        reason = denominator_row.get("reason") if denominator is None else None
        return _MomentumReading(
            None,
            reason
            if reason in {"fx_missing", "fx_stale", "fx_outside_coverage"}
            else "invalid_momentum_endpoint",
            numerator_session,
            denominator_session,
            currency,
        )
    return _MomentumReading(
        numerator / denominator - Decimal(1),
        None,
        numerator_session,
        denominator_session,
        currency,
    )


def _ranking_reason(
    *,
    policy: str,
    reading: _MomentumReading,
    rank: int,
    count: int,
    priority: Decimal,
    component_facts: tuple[ExplanationFactV1, ...] = (),
) -> SignalReasonV1:
    facts = [
        ExplanationFactV1(label="Ranking policy", observed=policy),
        ExplanationFactV1(
            label="Momentum",
            observed=None if reading.value is None else reading.value * Decimal(100),
            unit=EvidenceUnit.PERCENT,
        ),
        ExplanationFactV1(label="Momentum currency", observed=reading.currency),
        ExplanationFactV1(
            label="Momentum numerator offset",
            observed=Decimal(21),
            unit=EvidenceUnit.SESSIONS,
        ),
        ExplanationFactV1(
            label="Momentum denominator offset",
            observed=Decimal(252),
            unit=EvidenceUnit.SESSIONS,
        ),
        ExplanationFactV1(
            label="Candidate count", observed=Decimal(count), unit=EvidenceUnit.COUNT
        ),
        ExplanationFactV1(
            label="Ordinal rank", observed=Decimal(rank), unit=EvidenceUnit.COUNT
        ),
        ExplanationFactV1(
            label="Encoded priority", observed=priority, unit=EvidenceUnit.SCORE
        ),
        ExplanationFactV1(
            label="Momentum numerator session",
            observed=reading.numerator_session.isoformat()
            if reading.numerator_session
            else None,
            as_of=reading.numerator_session,
        ),
        ExplanationFactV1(
            label="Momentum denominator session",
            observed=reading.denominator_session.isoformat()
            if reading.denominator_session
            else None,
            as_of=reading.denominator_session,
        ),
    ]
    facts.extend(component_facts)
    if reading.missing_reason is not None:
        facts.append(
            ExplanationFactV1(
                label="Momentum unavailable reason", observed=reading.missing_reason
            )
        )
    momentum_text = (
        "unavailable"
        if reading.value is None
        else f"{(reading.value * Decimal(100)).to_eng_string()}%"
    )
    return SignalReasonV1(
        code="entry_ranking",
        summary=f"Ranked {rank} of {count} under {policy}; momentum {momentum_text}.",
        facts=facts,
    )


def _entry_explanation(
    qualification: _EntryQualification, session: date
) -> SignalExplanationV1:
    """Explain one VCP breakout entry in provider-neutral terms."""
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="stage2_confirmed",
                summary="The monthly scan reads a Stage 2 advance.",
                facts=[
                    ExplanationFactV1(
                        label="Scan stage",
                        observed=qualification.scan_stage,
                        operator=ComparisonOperator.IS,
                        threshold="Stage 2",
                    ),
                ],
            ),
            SignalReasonV1(
                code="vcp_breakout",
                summary=(
                    "Close broke out through the VCP pivot without being "
                    "extended beyond the buy range."
                ),
                facts=[
                    ExplanationFactV1(
                        label="Close",
                        observed=qualification.close,
                        operator=ComparisonOperator.GTE,
                        threshold=qualification.pivot,
                        unit=EvidenceUnit.PRICE,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Maximum extended price",
                        observed=qualification.extension_limit,
                        unit=EvidenceUnit.PRICE,
                    ),
                ],
            ),
            SignalReasonV1(
                code="volume_expansion",
                summary="Breakout volume expanded above its 50-session average.",
                facts=[
                    ExplanationFactV1(
                        label="Volume",
                        observed=qualification.volume,
                        operator=ComparisonOperator.GTE,
                        threshold=qualification.required_volume,
                        unit=EvidenceUnit.COUNT,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Required multiple of average volume",
                        observed=qualification.volume_multiplier,
                        unit=EvidenceUnit.RATIO,
                    ),
                ],
            ),
            SignalReasonV1(
                code="trend_template",
                summary="The security passes the trend template.",
                facts=[
                    ExplanationFactV1(
                        label="Trend template score",
                        observed=qualification.trend_score,
                        operator=ComparisonOperator.GTE,
                        threshold=qualification.minimum_trend_score,
                        unit=EvidenceUnit.SCORE,
                    ),
                ],
            ),
            SignalReasonV1(
                code="vcp_score",
                summary="The VCP base scores at or above the required minimum.",
                facts=[
                    ExplanationFactV1(
                        label="VCP score",
                        observed=Decimal(qualification.score),
                        operator=ComparisonOperator.GTE,
                        threshold=Decimal(qualification.minimum_score),
                        unit=EvidenceUnit.SCORE,
                    ),
                ],
            ),
        ]
    )


def _upgrade_explanation(
    *,
    candidate_id: str,
    candidate_score: Decimal,
    held_score: Decimal,
    margin: int,
    session: date,
) -> SignalExplanationV1:
    """Explain rotation from the weakest holding into stronger momentum."""
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="portfolio_upgrade",
                summary=(
                    "A qualifying candidate's 12-to-1 momentum exceeds this "
                    "holding's by the required percentage-point margin."
                ),
                facts=[
                    # The candidate's identity is a fact *value*, never part
                    # of the label: a long security id must not be able to
                    # overflow the label bound and cost the Sell signal.
                    ExplanationFactV1(label="Upgrade candidate", observed=candidate_id),
                    ExplanationFactV1(
                        label="Candidate 12-to-1 momentum",
                        observed=candidate_score * Decimal(100),
                        operator=ComparisonOperator.GTE,
                        threshold=held_score * Decimal(100) + Decimal(margin),
                        unit=EvidenceUnit.PERCENT,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Held 12-to-1 momentum",
                        observed=held_score * Decimal(100),
                        unit=EvidenceUnit.PERCENT,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Required momentum lead",
                        observed=Decimal(margin),
                        unit=EvidenceUnit.PERCENT,
                    ),
                ],
            ),
        ]
    )


class MinerviniStrategy:
    """Apply approved VCP entry and risk-exit rules."""

    def __init__(self) -> None:
        self._qualification_view: MarketViewV1 | None = None
        self._qualification_cache: dict[
            tuple[str, str], _EntryQualification | None
        ] = {}

    def _cached_entry_qualification(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> _EntryQualification | None:
        if self._qualification_view is not view:
            self._qualification_view = view
            self._qualification_cache.clear()
        key = (security_id, repr(sorted(parameters.items(), key=lambda item: item[0])))
        if key not in self._qualification_cache:
            self._qualification_cache[key] = self._entry_qualification(
                view, parameters, security_id
            )
        return self._qualification_cache[key]

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare the volume window plus the stage and VCP evidence.

        The VCP pattern state (pivot, contractions, execution state) and
        the Weinstein stage come from committed detector fragments, not
        from OHLCV, so both are declared for entry *and* exit (#471).
        """
        del parameters
        stage = EvidenceRequirementV1(kind=EvidenceKind.SCAN_STAGE)
        vcp = EvidenceRequirementV1(kind=EvidenceKind.SCAN_VCP)
        return StrategyEvidenceRequirementsV1(
            entry=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=51,
                    columns=("close", "volume"),
                ),
                stage,
                vcp,
            ),
            exit=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=50,
                    columns=("close",),
                ),
                stage,
                vcp,
            ),
        )

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        candidates: list[tuple[Signal, int, _MomentumReading]] = []
        for security_id in universe:
            qualification = self._cached_entry_qualification(
                view, parameters, security_id
            )
            if qualification is None:
                continue
            signal = self._entry_signal(
                view, parameters, security_id, qualification=qualification
            )
            assert signal is not None
            candidates.append(
                (
                    signal,
                    qualification.score,
                    _momentum_reading(view, security_id),
                )
            )
        ordered = sorted(
            candidates,
            key=lambda item: (
                -item[1],
                item[2].value is None,
                -(item[2].value or Decimal(0)),
                item[0].security_id,
            ),
        )
        count = len(ordered)
        ranked: list[Signal] = []
        for rank, (signal, score, reading) in enumerate(ordered, start=1):
            priority = Decimal(count - rank + 1)
            assert signal.explanation is not None
            explanation = SignalExplanationV1(
                reasons=(
                    *signal.explanation.reasons,
                    _ranking_reason(
                        policy="minervini_vcp_then_momentum_v1",
                        reading=reading,
                        rank=rank,
                        count=count,
                        priority=priority,
                        component_facts=(
                            ExplanationFactV1(
                                label="Raw VCP score",
                                observed=Decimal(score),
                                unit=EvidenceUnit.SCORE,
                            ),
                        ),
                    ),
                )
            )
            ranked.append(
                signal.model_copy(
                    update={"priority": priority, "explanation": explanation}
                )
            )
        return ranked

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
    ) -> _EntryQualification | None:
        """Return this security's VCP qualification -- its score plus the
        observations behind it -- if it qualifies for entry today, else
        ``None``. Factored out of :meth:`_entry_signal` so the upgrade-exit
        ranking (below) can score a would-be candidate using the exact same
        qualification rules, without duplicating them, and so the emitted
        Signal can explain itself (#472) from the very same numbers."""
        scan = _visible_scan(view, security_id)
        if scan is None:
            return None
        history = _current_history(
            view, security_id, limit=51, columns=("close", "volume")
        )
        if history is None or len(history) < 51:
            return None

        closes = _decimals(history["close"])
        volumes = _decimals(history["volume"])
        if closes is None or volumes is None:
            return None
        close = closes[-1]
        current_volume = volumes[-1]
        mean_volume = sum(volumes[-51:-1], Decimal(0)) / Decimal(50)
        if mean_volume <= 0 or current_volume < 0:
            return None

        vcp = getattr(scan, "vcp", None)
        stage = getattr(getattr(scan, "stage", None), "value", None)
        pivot = _decimal(getattr(vcp, "pivot_price", None))
        score = getattr(vcp, "score", None)
        trend_score = _decimal(getattr(vcp, "trend_template_score", None))
        minimum_trend = _decimal(parameters["minimum_trend_score"])
        minimum_volume = _decimal(parameters["minimum_relative_volume"])
        maximum_extension = _decimal(parameters["maximum_pivot_extension_pct"])
        minimum_vcp_score = _plain_int(parameters["minimum_vcp_score"])
        if None in (
            pivot,
            trend_score,
            minimum_trend,
            minimum_volume,
            maximum_extension,
            minimum_vcp_score,
        ):
            return None
        assert pivot is not None
        assert trend_score is not None
        assert minimum_trend is not None
        assert minimum_volume is not None
        assert maximum_extension is not None
        assert minimum_vcp_score is not None
        qualifies = (
            stage == "Stage 2"
            and getattr(vcp, "trend_template_passed", False) is True
            and getattr(vcp, "execution_state", None) in _ENTRY_SCAN_STATES
            and isinstance(score, int)
            and not isinstance(score, bool)
            and score >= minimum_vcp_score
            and trend_score >= minimum_trend
            and close >= pivot
            and close <= pivot * (Decimal(1) + maximum_extension / Decimal(100))
            and current_volume >= mean_volume * minimum_volume
        )
        if not qualifies:
            return None
        assert isinstance(score, int)
        return _EntryQualification(
            score=score,
            minimum_score=minimum_vcp_score,
            scan_stage=str(stage),
            close=close,
            pivot=pivot,
            extension_limit=pivot * (Decimal(1) + maximum_extension / Decimal(100)),
            volume=current_volume,
            required_volume=mean_volume * minimum_volume,
            volume_multiplier=minimum_volume,
            trend_score=trend_score,
            minimum_trend_score=minimum_trend,
        )

    def _entry_signal(
        self,
        view: MarketViewV1,
        parameters: StrategyParameters,
        security_id: str,
        *,
        qualification: _EntryQualification | None = None,
    ) -> Signal | None:
        qualification = qualification or self._cached_entry_qualification(
            view, parameters, security_id
        )
        if qualification is None:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id=_ENTRY_RULE,
            explanation=_entry_explanation(qualification, view.as_of_session),
        )

    def _upgrade_exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        already_exiting: frozenset[str],
    ) -> Signal | None:
        """Story: portfolio upgrading (Minervini's "upgrade" discipline).

        When the configured position cap is full and a stronger unheld
        candidate's current 12-to-1 momentum clears the weakest held
        position's momentum by ``upgrade_score_margin`` percentage points,
        sell the weakest holding -- exactly mirroring the mechanical
        stop/SMA/pattern-invalidation exits above, never overriding them.
        The freed cash is picked up by the ordinary ``entry_signals`` path
        on a later qualifying session; this method never buys anything
        itself.
        """
        if parameters.get("enable_position_upgrade") is not True:
            return None
        margin = _plain_int(parameters["upgrade_score_margin"])
        if margin is None:
            return None
        held_ids = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0 and position.security_id not in already_exiting
        }
        if not held_ids:
            return None
        position_cap = _plain_int(parameters.get("max_concurrent_positions"))
        if position_cap is None or position_cap < 1 or len(held_ids) < position_cap:
            return None

        candidates: list[tuple[Decimal, str]] = []
        for security_id in _universe(parameters):
            if security_id in held_ids:
                continue
            qualification = self._cached_entry_qualification(
                view, parameters, security_id
            )
            if qualification is not None:
                momentum = _momentum_reading(view, security_id).value
                if momentum is not None:
                    candidates.append((momentum, security_id))
        if not candidates:
            return None
        best_score, best_security_id = max(candidates, key=lambda item: item)

        held_scored = [
            (momentum, security_id)
            for security_id in held_ids
            if (momentum := _momentum_reading(view, security_id).value) is not None
        ]
        if not held_scored:
            return None
        weakest_score, weakest_security_id = min(held_scored, key=lambda item: item)

        if best_score - weakest_score < Decimal(margin) / Decimal(100):
            return None
        return Signal(
            security_id=weakest_security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_UPGRADE_EXIT_RULE,
            explanation=_upgrade_explanation(
                candidate_id=best_security_id,
                candidate_score=best_score,
                held_score=weakest_score,
                margin=margin,
                session=view.as_of_session,
            ),
        )

    def _exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> Signal | None:
        held = _position(portfolio, security_id)
        if held is None or held.quantity <= 0:
            return None
        scan = _visible_scan(view, security_id)
        history = _current_history(view, security_id, limit=50, columns=("close",))
        if history is None or len(history) < 50:
            return None
        closes = _decimals(history["close"].iloc[-50:])
        maximum_loss = _decimal(parameters["maximum_loss_pct"])
        if closes is None or maximum_loss is None:
            return None
        close = closes[-1]
        sma50 = sum(closes, Decimal(0)) / Decimal(50)
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        stage = getattr(getattr(scan, "stage", None), "value", None)
        state = getattr(getattr(scan, "vcp", None), "execution_state", None)
        # Only *evidenced* stage/pattern values can fail: a view carrying
        # no scan evidence must never be read as a pattern failure, which
        # would manufacture a Sell out of missing evidence (#471).
        # The reason list below *is* the exit decision (#472): each rule
        # appends itself, and the Sell fires exactly when at least one did.
        # Deriving the decision from the reasons rather than restating both
        # makes an explained condition and a firing condition impossible to
        # diverge.
        reasons: list[SignalReasonV1] = []
        if close <= stop:
            reasons.append(
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
                            observed=maximum_loss,
                            unit=EvidenceUnit.PERCENT,
                        ),
                    ],
                )
            )
        if close < sma50:
            reasons.append(
                SignalReasonV1(
                    code="close_below_sma50",
                    summary="Close fell below the 50-session moving average.",
                    facts=[
                        ExplanationFactV1(
                            label="Close",
                            observed=close,
                            operator=ComparisonOperator.LT,
                            threshold=sma50,
                            unit=EvidenceUnit.PRICE,
                            as_of=view.as_of_session,
                        ),
                    ],
                )
            )
        if stage is not None and stage != "Stage 2":
            reasons.append(
                SignalReasonV1(
                    code="stage_exit",
                    summary="The security is no longer in a Stage 2 advance.",
                    facts=[
                        ExplanationFactV1(
                            label="Weinstein stage",
                            observed=stage,
                            operator=ComparisonOperator.IS_NOT,
                            threshold="Stage 2",
                        ),
                    ],
                )
            )
        if isinstance(state, str) and state in {"Invalid", "Damaged"}:
            reasons.append(
                SignalReasonV1(
                    code="vcp_state_invalidated",
                    summary="The VCP base is no longer intact.",
                    facts=[
                        ExplanationFactV1(label="VCP execution state", observed=state),
                    ],
                )
            )
        # Only an *evidenced* stage/pattern value can fail: a view carrying
        # no scan evidence must never be read as a pattern failure, which
        # would manufacture a Sell out of missing evidence (#471).
        if not reasons:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_EXIT_RULE,
            explanation=SignalExplanationV1(reasons=reasons),
        )

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the close at which :meth:`_exit_signal` fires next session.

        The exit sells on ``close <= stop`` or on a close below the next
        session's 50-session SMA, which is exactly a close below the mean of
        today's latest 49 closes; the higher of the two binds.
        ``maximum_loss_pct`` is read as the exit reads it, with no default.
        """
        held = _position(portfolio, security_id)
        if held is None or held.quantity <= 0:
            return None
        maximum_loss = _decimal(parameters.get("maximum_loss_pct"))
        if maximum_loss is None or not 0 <= maximum_loss < 100:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy setting maximum_loss_pct is missing or unusable.",
            )
        history = _current_history(view, security_id, limit=49, columns=("close",))
        closes = (
            None
            if history is None or len(history) < 49
            else _decimals(history["close"])
        )
        if closes is None:
            return StopLevelV1(
                rule_code="insufficient_history",
                summary="Needs 49 sessions of current closes.",
            )
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        sma_break = sum(closes, Decimal(0)) / Decimal(49)
        facts = [
            ExplanationFactV1(
                label="Maximum-loss stop", observed=stop, unit=EvidenceUnit.PRICE
            ),
            ExplanationFactV1(
                label="Maximum loss", observed=maximum_loss, unit=EvidenceUnit.PERCENT
            ),
            ExplanationFactV1(
                label="Close that breaks the 50-session SMA",
                observed=sma_break,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
        ]
        if sma_break > stop:
            return StopLevelV1(
                level=sma_break,
                rule_code="close_below_sma50",
                basis="mixed",
                trigger="close_lt",
                summary="Close below the 50-day average",
                facts=facts,
            )
        if stop <= 0:
            return StopLevelV1(
                rule_code="no_level", summary="No positive stop level.", facts=facts
            )
        return StopLevelV1(
            level=stop,
            rule_code="maximum_loss_stop",
            summary=f"Max loss {maximum_loss.normalize():f}% from average cost",
            facts=facts,
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
        if signal.side == SignalSide.SELL:
            return _integral_quantity(portfolio, signal.security_id)
        # The engine reserves equal capital and determines whole shares.
        return 0
