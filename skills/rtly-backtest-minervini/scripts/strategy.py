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
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-minervini"
STRATEGY_API_VERSION = 1
_ENTRY_RULE = "minervini_vcp_breakout_v1"
_EXIT_RULE = "minervini_risk_exit_v1"
_UPGRADE_EXIT_RULE = "minervini_upgrade_exit_v1"


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


def _current_history(view: MarketViewV1, security_id: str) -> Any | None:
    history = view.price_history(security_id)
    if history.empty or _session_date(history.index[-1]) != view.as_of_session:
        return None
    if not {"close", "volume"}.issubset(history.columns):
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


def _integral_quantity(portfolio: PortfolioView, security_id: str) -> int:
    held = _position(portfolio, security_id)
    if held is None or held.quantity <= 0:
        return 0
    integral = held.quantity.to_integral_value()
    return int(integral) if held.quantity == integral else 0


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
    candidate_score: int,
    held_score: int,
    margin: int,
    session: date,
) -> SignalExplanationV1:
    """Explain rotating out of the weakest holding into a stronger base."""
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="portfolio_upgrade",
                summary=(
                    "A stronger VCP candidate outscores this holding by more "
                    "than the required margin, so capital rotates to it."
                ),
                facts=[
                    # The candidate's identity is a fact *value*, never part
                    # of the label: a long security id must not be able to
                    # overflow the label bound and cost the Sell signal.
                    ExplanationFactV1(label="Upgrade candidate", observed=candidate_id),
                    ExplanationFactV1(
                        label="Candidate VCP score",
                        observed=Decimal(candidate_score),
                        operator=ComparisonOperator.GTE,
                        threshold=Decimal(held_score + margin),
                        unit=EvidenceUnit.SCORE,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Held VCP score",
                        observed=Decimal(held_score),
                        unit=EvidenceUnit.SCORE,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Required upgrade margin",
                        observed=Decimal(margin),
                        unit=EvidenceUnit.SCORE,
                    ),
                ],
            ),
        ]
    )


class MinerviniStrategy:
    """Apply approved VCP entry and risk-exit rules without mutable state."""

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
        signals = [
            self._entry_signal(view, parameters, security_id)
            for security_id in universe
        ]
        return [signal for signal in signals if signal is not None]

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
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        if history is None or scan is None or len(history) < 51:
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
            and getattr(vcp, "valid_vcp", False) is True
            and getattr(vcp, "trend_template_passed", False) is True
            and getattr(vcp, "execution_state", None) == "Breakout"
            and getattr(vcp, "breakout_volume_detected", False) is True
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
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> Signal | None:
        qualification = self._entry_qualification(view, parameters, security_id)
        if qualification is None:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id=_ENTRY_RULE,
            explanation=_entry_explanation(qualification, view.as_of_session),
        )

    def _held_vcp_score(self, view: MarketViewV1, security_id: str) -> int | None:
        """Return a held position's current VCP score for upgrade ranking,
        or ``None`` if no visible scan evidence exists today -- a position
        with no computable score is never treated as the weakest holding."""
        scan = _visible_scan(view, security_id)
        if scan is None:
            return None
        score = getattr(getattr(scan, "vcp", None), "score", None)
        return score if isinstance(score, int) and not isinstance(score, bool) else None

    def _upgrade_exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        already_exiting: frozenset[str],
    ) -> Signal | None:
        """Story: portfolio upgrading (Minervini's "upgrade" discipline).

        When a stronger unheld candidate's VCP score clears the weakest held
        position's own current score by at least ``upgrade_score_margin``,
        sell the weakest holding to free cash for the stronger setup --
        exactly mirroring the mechanical
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
        # The shared allocator owns BUY affordability.  Do not liquidate a
        # holding while a cash slot remains for its next cohort.
        if portfolio.cash > 0:
            return None

        held_ids = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0 and position.security_id not in already_exiting
        }
        if not held_ids:
            return None

        candidates: list[tuple[int, str]] = []
        for security_id in _universe(parameters):
            if security_id in held_ids:
                continue
            qualification = self._entry_qualification(view, parameters, security_id)
            if qualification is not None:
                candidates.append((qualification.score, security_id))
        if not candidates:
            return None
        best_score, best_security_id = max(candidates, key=lambda item: item)

        held_scored = [
            (score, security_id)
            for security_id in held_ids
            if (score := self._held_vcp_score(view, security_id)) is not None
        ]
        if not held_scored:
            return None
        weakest_score, weakest_security_id = min(held_scored, key=lambda item: item)

        if best_score - weakest_score < margin:
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
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        if held is None or held.quantity <= 0 or history is None or len(history) < 50:
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

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.SELL:
            return _integral_quantity(portfolio, signal.security_id)
        # The engine reserves equal capital and determines whole shares.
        return 0
